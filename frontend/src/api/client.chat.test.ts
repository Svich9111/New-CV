/**
 * A4, задача 2.2 — клиент чата: `GET /chat/messages` (курсор, строгая проверка) и `POST /chat/messages`
 * (поток SSE по частям и целиком, обрыв, отмена, срок тишины, ответы до потока). Заглушка сервера — по контракту.
 */
import { Api, ApiError, type ChatOutcome } from "./client";
import { FakeServer, crisis, delta, done, errorEvent, gate, json } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";

async function api(server = new FakeServer().onboarded(), timeouts?: ConstructorParameters<typeof Api>[0]["timeouts"]) {
  const a = new Api({ initData: () => INIT_DATA, fetch: server.fetch, timeouts });
  await a.login();
  return { a, server };
}

async function send(a: Api, text = "Мне тревожно", signal?: AbortSignal) {
  const parts: string[] = [];
  const out: ChatOutcome = await a.chatSend(text, { signal, onDelta: (t) => parts.push(t) });
  return { out, parts };
}

async function sendErr(a: Api, text = "Мне тревожно", signal?: AbortSignal): Promise<ApiError> {
  try {
    await a.chatSend(text, { signal, onDelta: () => undefined });
  } catch (e) {
    expect(e).toBeInstanceOf(ApiError);
    return e as ApiError;
  }
  throw new Error("ожидалась ошибка");
}

describe("POST /chat/messages — поток ответа", () => {
  test("по частям: куски по порядку, done; тело запроса — только text; заголовки", async () => {
    const { a, server } = await api();
    server.reply({ steps: [delta("Слышу, "), { comment: "ping" }, delta("как тебе.\nРасскажи."), done("a1")] });
    const { out, parts } = await send(a, "Мне тревожно");
    expect(parts).toEqual(["Слышу, ", "как тебе.\nРасскажи."]);
    expect(out).toEqual({ kind: "done", data: { message_id: "a1", suggest_draft: false } });
    const call = server.calls("POST", "/chat/messages")[0];
    expect(call.body).toEqual({ text: "Мне тревожно" });
    expect(call.auth).toMatch(/^Bearer /);
  });

  test("целиком одним куском — тот же результат", async () => {
    const { a, server } = await api();
    server.reply({ chunk: "whole", steps: [delta("Раз "), delta("два"), done("a2")] });
    const { out, parts } = await send(a);
    expect(parts.join("")).toBe("Раз два");
    expect(out.kind).toBe("done");
  });

  test("по 1 байту: многобайтные символы и эмодзи не ломаются", async () => {
    const { a, server } = await api();
    server.reply({ chunk: 1, steps: [delta("Ёжик 🦔 "), delta("и «кавычки»"), done("a3")] });
    const { parts } = await send(a);
    expect(parts.join("")).toBe("Ёжик 🦔 и «кавычки»");
  });

  test("тело без потока (реализация fetch без body) — разбирается целиком", async () => {
    const server = new FakeServer().onboarded();
    const base = server.fetch;
    const a = new Api({
      initData: () => INIT_DATA,
      fetch: async (input, init) => {
        const res = await base(input, init);
        if (!res.headers.get("Content-Type")?.startsWith("text/event-stream")) return res;
        const text = await res.text();
        return { ok: true, status: 200, headers: res.headers, body: null, text: async () => text } as unknown as Response;
      },
    });
    await a.login();
    server.reply({ steps: [delta("Целиком"), done("a4")] });
    const { out, parts } = await send(a);
    expect(parts).toEqual(["Целиком"]);
    expect(out.kind).toBe("done");
  });

  test("кризис: событие с контактами; испорченные записи отброшены", async () => {
    const { a, server } = await api();
    const contacts = [{ name: "112", phone: "112", description: "Экстренные службы", round_the_clock: true }, { phone: 5 }];
    server.reply({ steps: [crisis("Ответ протокола целиком.", { contacts, id: "c1" })] });
    const { out, parts } = await send(a);
    expect(parts).toEqual([]);
    expect(out).toEqual({
      kind: "crisis",
      data: { messageId: "c1", text: "Ответ протокола целиком.", replace: false, contacts: [contacts[0]] },
    });
  });

  test("error: код по контракту; незнакомый — internal", async () => {
    const { a, server } = await api();
    server.reply({ steps: [delta("Нача"), errorEvent("unavailable")] });
    expect((await send(a)).out).toEqual({ kind: "error", code: "unavailable" });
    server.reply({ steps: [errorEvent("brand_new")] });
    expect((await send(a)).out).toEqual({ kind: "error", code: "internal" });
  });

  test("после завершающего события остальное игнорируется", async () => {
    const { a, server } = await api();
    server.reply({ steps: [delta("а"), done("a5"), delta("лишнее"), errorEvent("internal")] });
    const { out, parts } = await send(a);
    expect(parts).toEqual(["а"]);
    expect(out.kind).toBe("done");
  });

  test("соединение закрылось без завершающего события — stream_broken (200: реплика сохранена)", async () => {
    const { a, server } = await api();
    server.reply({ steps: [delta("Нача")] });
    const e = await sendErr(a);
    expect([e.status, e.code]).toEqual([200, "stream_broken"]);
    server.reply({ steps: [delta("Нача")], end: "error" });
    expect((await sendErr(a)).code).toBe("stream_broken");
  });

  test("событие не по контракту — bad_response", async () => {
    const { a, server } = await api();
    server.reply({ steps: [{ event: "delta", data: { text: "" } }] });
    expect((await sendErr(a)).code).toBe("bad_response");
    server.reply({ steps: [{ event: "done", data: { message_id: 7 } }] });
    expect((await sendErr(a)).code).toBe("bad_response");
  });

  test("ответ 200 не потоком (application/json) — bad_response", async () => {
    const { a, server } = await api();
    server.inject("POST", "/chat/messages", 200, { text: "не поток" });
    const e = await sendErr(a);
    expect(e.code).toBe("bad_response");
  });

  test("тишина дольше chatIdle — stream_timeout; комментарии-пинги продлевают срок", async () => {
    const { a, server } = await api(undefined, { chatIdle: 60 });
    server.reply({ steps: [delta("а"), { wait: 40 }, { comment: "ping" }, { wait: 40 }, delta("б"), done("a6")] });
    expect((await send(a)).out.kind).toBe("done");
    server.reply({ steps: [delta("а")], end: "hang" });
    const e = await sendErr(a);
    expect([e.status, e.code]).toEqual([200, "stream_timeout"]);
    await vi.waitFor(() => expect(server.streamsAborted).toBe(1));
  });

  test("отмена посреди потока — aborted (200), поток закрыт", async () => {
    const { a, server } = await api();
    const g = gate();
    server.reply({ steps: [delta("а"), g.step, delta("б"), done()] });
    const ctrl = new AbortController();
    const parts: string[] = [];
    const p = a.chatSend("x", { signal: ctrl.signal, onDelta: (t) => parts.push(t) }).catch((e: unknown) => e);
    await vi.waitFor(() => expect(parts).toEqual(["а"]));
    ctrl.abort();
    const e = (await p) as ApiError;
    expect([e.status, e.code]).toEqual([200, "aborted"]);
    expect(server.streamsAborted).toBe(1);
    g.open();
  });

  test("отмена до ответа сервера — aborted со статусом 0", async () => {
    const server = new FakeServer().onboarded();
    let release: (r: Response) => void = () => undefined;
    const a = new Api({
      initData: () => INIT_DATA,
      fetch: (input, init) =>
        String(input).endsWith("/chat/messages") && init?.method === "POST"
          ? new Promise<Response>((res) => (release = res))
          : server.fetch(input, init),
    });
    await a.login();
    const ctrl = new AbortController();
    const p = a.chatSend("x", { signal: ctrl.signal, onDelta: () => undefined }).catch((e: unknown) => e);
    ctrl.abort();
    const e = (await p) as ApiError;
    expect([e.status, e.code]).toEqual([0, "aborted"]);
    release(json(500, { code: "internal", message: "x" }));
  });

  test("сервер не ответил за chatStart — timeout (0)", async () => {
    const server = new FakeServer().onboarded();
    const a = new Api({
      initData: () => INIT_DATA,
      timeouts: { chatStart: 30 },
      fetch: (input, init) =>
        String(input).endsWith("/chat/messages") && init?.method === "POST" ? new Promise<Response>(() => undefined) : server.fetch(input, init),
    });
    await a.login();
    const e = await sendErr(a);
    expect([e.status, e.code]).toEqual([0, "timeout"]);
  });
});

describe("POST /chat/messages — ответы до потока", () => {
  test("429: код, Retry-After, реплика сохранена сервером", async () => {
    const { a, server } = await api();
    server.reply({ status: 429, body: { code: "rate_limited", message: "лимит" }, headers: { "Retry-After": "3600" } });
    const e = await sendErr(a, "Сохрани меня");
    expect([e.status, e.code, e.retryAfter]).toEqual([429, "rate_limited", 3600]);
    expect(server.chat.map((m) => m.text)).toEqual(["Сохрани меня"]);
  });

  test("409 conflict_retry — один повтор; второй 409 — ошибка, ничего не сохранено", async () => {
    const { a, server } = await api();
    server.reply({ status: 409, body: { code: "conflict_retry", message: "x" } });
    expect((await send(a)).out.kind).toBe("done");
    expect(server.calls("POST", "/chat/messages")).toHaveLength(2);
    server.reply({ status: 409, body: { code: "conflict_retry", message: "x" } });
    server.reply({ status: 409, body: { code: "conflict_retry", message: "x" } });
    const e = await sendErr(a);
    expect(e.code).toBe("conflict_retry");
    expect(server.calls("POST", "/chat/messages")).toHaveLength(4);
  });

  test("401 — один повторный вход и один повтор", async () => {
    const { a, server } = await api();
    server.expireSession();
    expect((await send(a)).out.kind).toBe("done");
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
    expect(server.calls("POST", "/chat/messages")).toHaveLength(2);
  });

  test("422 и 403 — текст на «ты» по коду; 5xx без JSON — internal", async () => {
    const { a, server } = await api();
    server.reply({ status: 422, body: { code: "validation_error", message: "Проверьте данные" } });
    expect((await sendErr(a)).message).toMatch(/длиннее 4000/);
    server.reply({ status: 403, body: { code: "onboarding_required", message: "x" }, saveOwner: false });
    expect((await sendErr(a)).message).toMatch(/Сначала нужно познакомиться/);
    server.inject("POST", "/chat/messages", 502);
    expect((await sendErr(a)).code).toBe("internal");
  });
});

describe("GET /chat/messages — история", () => {
  test("limit и cursor в запросе; страницы от новых к старым; next_cursor null в конце", async () => {
    const server = new FakeServer().onboarded().seedChat(5);
    const { a } = await api(server);
    const p1 = await a.chatHistory({ limit: 3 });
    expect(p1.items.map((m) => m.text)).toEqual(["Реплика 5", "Реплика 4", "Реплика 3"]);
    expect(p1.next_cursor).not.toBeNull();
    const p2 = await a.chatHistory({ limit: 3, cursor: p1.next_cursor });
    expect(p2.items.map((m) => m.text)).toEqual(["Реплика 2", "Реплика 1"]);
    expect(p2.next_cursor).toBeNull();
    expect(server.calls("GET", "/chat/messages").map((c) => c.query)).toEqual([
      "?limit=3",
      `?limit=3&cursor=${encodeURIComponent(p1.next_cursor!)}`,
    ]);
  });

  test("ответ не по схеме — bad_response; 422 — ApiError 422", async () => {
    const { a, server } = await api();
    server.inject("GET", "/chat/messages", 200, { items: [{ id: "x", author: "partner", text: "чужое", created_at: "2026-11-20T10:00:00Z" }], next_cursor: null });
    expect(await a.chatHistory().catch((e: ApiError) => e.code)).toBe("bad_response");
    server.inject("GET", "/chat/messages", 422, { code: "validation_error", message: "x" });
    expect(await a.chatHistory({ cursor: "c.0.0" }).catch((e: ApiError) => e.status)).toBe(422);
  });
});
