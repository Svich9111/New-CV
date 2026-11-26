/**
 * A8, задача 2.2, раунд 1 — негативные сценарии клиента чата (ФТ-05; ТЗ §4.2, §9.3, §10; контракт
 * `POST /chat/messages`, `GET /chat/messages`, АР-32, Б-62). Поток здесь — сырые байты, нарезанные тестом
 * (а не заглушкой сервера): куски по 1 байту, разрыв UTF-8 посреди символа и посреди JSON, `\r\n`/`\r`,
 * пустые и незнакомые события, мусор, тишина, обрыв посреди ответа. Тексты выдуманные.
 */
import { Api, ApiError, type ChatOutcome } from "./client";
import { limitUntilText } from "./messages";
import { FakeServer, json } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";

type Piece = string | Uint8Array | { wait: number } | { fail: true };

const enc = new TextEncoder();
const bytes = (s: string) => enc.encode(s);

/** Поток из заданных кусков; `fail` — соединение рвётся с ошибкой; без `close` поток висит. */
function sse(pieces: Piece[], opts: { close?: boolean; type?: string } = {}) {
  const st = { cancelled: 0 };
  const body = new ReadableStream<Uint8Array>({
    async start(c) {
      for (const p of pieces) {
        if (typeof p === "string") c.enqueue(bytes(p));
        else if (ArrayBuffer.isView(p)) c.enqueue(p);
        else if ("wait" in p) await new Promise((r) => setTimeout(r, p.wait));
        else {
          c.error(new TypeError("network error"));
          return;
        }
        await new Promise((r) => setTimeout(r, 0));
      }
      if (opts.close !== false) c.close();
    },
    cancel() {
      st.cancelled += 1;
    },
  });
  const res = new Response(body, { status: 200, headers: { "Content-Type": opts.type ?? "text/event-stream; charset=utf-8" } });
  return { res, st };
}

/** Api, у которого `POST /chat/messages` отвечает `make()`; остальное — заглушка сервера. */
async function apiWith(make: () => Response | Promise<Response>, timeouts?: ConstructorParameters<typeof Api>[0]["timeouts"]) {
  const server = new FakeServer().onboarded();
  const a = new Api({
    initData: () => INIT_DATA,
    timeouts,
    fetch: async (input, init) => {
      const url = String(input);
      if (url.endsWith("/chat/messages") && init?.method === "POST") {
        server.log.push({ method: "POST", path: "/chat/messages", query: "", auth: null, body: JSON.parse(String(init.body)) });
        return make();
      }
      return server.fetch(input, init);
    },
  });
  await a.login();
  return { a, server };
}

async function run(a: Api, signal?: AbortSignal): Promise<{ out?: ChatOutcome; err?: ApiError; parts: string[] }> {
  const parts: string[] = [];
  try {
    const out = await a.chatSend("Мне тревожно", { signal, onDelta: (t) => parts.push(t) });
    return { out, parts };
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    return { err: e, parts };
  }
}

const ev = (name: string, data: unknown, nl = "\n") =>
  `event: ${name}${nl}data: ${typeof data === "string" ? data : JSON.stringify(data)}${nl}${nl}`;
const DONE = { message_id: "a0000000-0000-4000-8000-000000000001", suggest_draft: false };

/** Нарезать байты потока по `n`. */
function cut(s: string | Uint8Array, n: number): Uint8Array[] {
  const b = typeof s === "string" ? bytes(s) : s;
  const out: Uint8Array[] = [];
  for (let i = 0; i < b.length; i += n) out.push(b.slice(i, i + n));
  return out;
}

describe("A8 2.2 — поток: нарезка, переводы строк, UTF-8 (ФТ-05, контракт SSE)", () => {
  test("весь поток по 1 байту с \\r\\n, комментариями и эмодзи — те же куски, done", async () => {
    const raw = ": ping\r\n\r\n" + ev("delta", { text: "Ёж 🦔, " }, "\r\n") + ": ping\r\n\r\n" + ev("delta", { text: "«тут»\nстрока" }, "\r\n") + ev("done", DONE, "\r\n");
    const { a } = await apiWith(() => sse(cut(raw, 1)).res);
    const { out, parts } = await run(a);
    expect(parts).toEqual(["Ёж 🦔, ", "«тут»\nстрока"]);
    expect(out).toEqual({ kind: "done", data: DONE });
  });

  test("разрыв UTF-8 посреди 4-байтного эмодзи и 2-байтной буквы, на границе куска внутри JSON", async () => {
    const raw = bytes(ev("delta", { text: "я🦔ё" }) + ev("done", DONE));
    const emoji = bytes("🦔");
    // Найти начало эмодзи в байтах и разрезать посреди него, затем посреди «ё».
    const at = raw.findIndex((_, i) => emoji.every((b, j) => raw[i + j] === b));
    const yo = at + 4; // «ё» — сразу после эмодзи, 2 байта
    const parts = [raw.slice(0, at + 2), raw.slice(at + 2, yo + 1), raw.slice(yo + 1)];
    const { a } = await apiWith(() => sse(parts).res);
    const r = await run(a);
    expect(r.parts.join("")).toBe("я🦔ё");
    expect(r.parts.join("")).not.toContain("�");
    expect(r.out?.kind).toBe("done");
  });

  test("одиночный \\r как конец строки, граница куска сразу после \\r, затем \\n — одно событие", async () => {
    const pieces = ["event: delta\r", "\ndata: {\"text\":\"раз\"}\r", "\r", "event: done\rdata: " + JSON.stringify(DONE) + "\r", "\r"];
    const { a } = await apiWith(() => sse(pieces).res);
    const r = await run(a);
    expect(r.parts).toEqual(["раз"]);
    expect(r.out?.kind).toBe("done");
  });

  test("BOM в начале потока не ломает первое событие", async () => {
    const { a } = await apiWith(() => sse(["﻿" + ev("delta", { text: "раз" }) + ev("done", DONE)]).res);
    const r = await run(a);
    expect(r.parts).toEqual(["раз"]);
    expect(r.out?.kind).toBe("done");
  });

  test("пустые события и строки (без data, только event, пустые строки подряд) — пропускаются", async () => {
    const raw = "\n\n\n\nevent: delta\n\n" + "event: done\n\n" + ev("delta", { text: "раз" }) + "\r\n\r\n" + ev("done", DONE);
    const { a } = await apiWith(() => sse([raw]).res);
    const r = await run(a);
    expect(r.parts).toEqual(["раз"]);
    expect(r.out?.kind).toBe("done");
  });

  test("незнакомые события (accepted, ping, message без event) — пропускаются, поток продолжается", async () => {
    const raw =
      ev("accepted", { message_id: "x" }) + ev("ping", "{}") + "data: {\"text\":\"не кусок\"}\n\n" + ev("Delta", { text: "регистр" }) + ev("delta", { text: "раз" }) + ev("done", DONE);
    const { a } = await apiWith(() => sse([raw]).res);
    const r = await run(a);
    expect(r.parts).toEqual(["раз"]);
    expect(r.out?.kind).toBe("done");
  });

  test("мусорные строки без двоеточия и неизвестные поля — пропускаются; событие с ними же — цело", async () => {
    const raw = "привет мир\nfoo: bar\n\nevent: delta\nxyz: 1\ndata: {\"text\":\"раз\"}\nid: 7\nretry: 10\n\n" + ev("done", DONE);
    const { a } = await apiWith(() => sse([raw]).res);
    const r = await run(a);
    expect(r.parts).toEqual(["раз"]);
    expect(r.out?.kind).toBe("done");
  });

  test.each([
    ["delta не JSON", ev("delta", "<html>oops")],
    ["delta пустой text (minLength 1)", ev("delta", { text: "" })],
    ["delta text числом", ev("delta", { text: 5 })],
    ["delta массивом", ev("delta", [{ text: "x" }])],
    ["done без message_id", ev("done", { suggest_draft: false })],
    ["done suggest_draft строкой", ev("done", { message_id: "a1", suggest_draft: "false" })],
    ["done data пустая строка", "event: done\ndata:\n\n"],
  ])("испорченное событие (%s) — bad_response со статусом 200, поток закрыт", async (_n, bad) => {
    let s: ReturnType<typeof sse> | null = null;
    const { a } = await apiWith(() => {
      s = sse([ev("delta", { text: "раз " }), bad, ev("delta", { text: "после" })], { close: false });
      return s.res;
    });
    const r = await run(a);
    expect(r.err?.status).toBe(200);
    expect(r.err?.code).toBe("bad_response");
    expect(r.parts).toEqual(["раз "]);
    await vi.waitFor(() => expect(s!.st.cancelled).toBe(1));
  });

  test("недопустимые байты UTF-8 в куске — заменяются, а не роняют разбор", async () => {
    const raw = new Uint8Array([...bytes('event: delta\ndata: {"text":"а'), 0xff, 0xfe, ...bytes('б"}\n\n'), ...bytes(ev("done", DONE))]);
    const { a } = await apiWith(() => sse([raw]).res);
    const r = await run(a);
    expect(r.out?.kind).toBe("done");
    expect(r.parts.join("")).toMatch(/^а.+б$/u);
  });

  test("два завершающих события подряд — берётся первое, остальное не читается", async () => {
    const { a } = await apiWith(() => sse([ev("done", DONE) + ev("error", { code: "internal", message: "x" })]).res);
    const r = await run(a);
    expect(r.out).toEqual({ kind: "done", data: DONE });
  });

  test("Content-Type text/event-stream без charset и в другом регистре — принимается", async () => {
    const { a } = await apiWith(() => sse([ev("done", DONE)], { type: "Text/Event-Stream" }).res);
    expect((await run(a)).out?.kind).toBe("done");
  });

  test("Content-Type text/plain с телом SSE — bad_response, ни одного куска", async () => {
    const { a } = await apiWith(() => sse([ev("delta", { text: "раз" }) + ev("done", DONE)], { type: "text/plain" }).res);
    const r = await run(a);
    expect(r.err?.code).toBe("bad_response");
    expect(r.parts).toEqual([]);
  });
});

describe("A8 2.2 — тишина, обрыв, отмена (ТЗ §10)", () => {
  afterEach(() => vi.useRealTimers());

  test("тишина ровно chatIdle после куска — stream_timeout; за 1 мс до срока — ещё ждём", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { a } = await apiWith(() => sse([ev("delta", { text: "раз" })], { close: false }).res, { chatIdle: 45_000 });
    const p = run(a);
    await vi.advanceTimersByTimeAsync(10);
    let settled = false;
    void p.then(() => (settled = true));
    await vi.advanceTimersByTimeAsync(44_980);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(50);
    const r = await p;
    expect(r.err?.code).toBe("stream_timeout");
    expect(r.err?.status).toBe(200);
    expect(r.parts).toEqual(["раз"]);
  });

  test("пинги раз в 15 с держат поток 2 минуты; затем done — без срока", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const pings: Piece[] = [];
    for (let i = 0; i < 8; i++) pings.push({ wait: 15_000 }, ": ping\n\n");
    const { a } = await apiWith(() => sse([ev("delta", { text: "раз" }), ...pings, ev("done", DONE)]).res, { chatIdle: 45_000 });
    const p = run(a);
    await vi.advanceTimersByTimeAsync(130_000);
    const r = await p;
    expect(r.out?.kind).toBe("done");
  });

  test("тишина до первого куска (заголовки пришли, событий нет) — тоже stream_timeout", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { a } = await apiWith(() => sse([], { close: false }).res, { chatIdle: 45_000 });
    const p = run(a);
    await vi.advanceTimersByTimeAsync(45_100);
    expect((await p).err?.code).toBe("stream_timeout");
  });

  test("байты без конца строки не считаются событием, но продлевают срок (соединение живо)", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { a } = await apiWith(() => sse([{ wait: 30_000 }, "event: del", { wait: 30_000 }, "ta\ndata: {\"text\":\"раз\"}\n\n", ev("done", DONE)]).res, {
      chatIdle: 45_000,
    });
    const p = run(a);
    await vi.advanceTimersByTimeAsync(61_000);
    const r = await p;
    expect(r.out?.kind).toBe("done");
    expect(r.parts).toEqual(["раз"]);
  });

  test("обрыв с ошибкой посреди ответа (после кусков) — stream_broken 200, показанные куски остались у вызывающего", async () => {
    const { a } = await apiWith(() => sse([ev("delta", { text: "раз " }), ev("delta", { text: "два" }), { fail: true }]).res);
    const r = await run(a);
    expect(r.err?.status).toBe(200);
    expect(r.err?.code).toBe("stream_broken");
    expect(r.parts).toEqual(["раз ", "два"]);
  });

  test("обрыв посреди события (половина строки data) — недописанное не отдаётся, stream_broken", async () => {
    const { a } = await apiWith(() => sse([ev("delta", { text: "раз" }), 'event: delta\ndata: {"text":"по'], { close: true }).res);
    const r = await run(a);
    expect(r.err?.code).toBe("stream_broken");
    expect(r.parts).toEqual(["раз"]);
  });

  test("завершающее событие без пустой строки в конце (соединение закрылось) — по стандарту отброшено, stream_broken", async () => {
    const { a } = await apiWith(() => sse([ev("delta", { text: "раз" }), "event: done\ndata: " + JSON.stringify(DONE) + "\n"]).res);
    expect((await run(a)).err?.code).toBe("stream_broken");
  });

  test("«Остановить» после первого куска — aborted 200, поток отменён один раз, больше кусков нет", async () => {
    let s: ReturnType<typeof sse> | null = null;
    const { a } = await apiWith(() => {
      s = sse([ev("delta", { text: "раз" }), { wait: 50 }, ev("delta", { text: "два" }), ev("done", DONE)]);
      return s.res;
    });
    const ctrl = new AbortController();
    const parts: string[] = [];
    let err: ApiError | null = null;
    await a
      .chatSend("x", {
        signal: ctrl.signal,
        onDelta: (t) => {
          parts.push(t);
          ctrl.abort();
        },
      })
      .catch((e: ApiError) => (err = e));
    expect(err!.code).toBe("aborted");
    expect(err!.status).toBe(200);
    expect(parts).toEqual(["раз"]);
    await vi.waitFor(() => expect(s!.st.cancelled).toBe(1));
  });

  test("«Остановить», когда fetch не слушает сигнал и висит, — сразу aborted 0", async () => {
    const { a } = await apiWith(() => new Promise<Response>(() => undefined));
    const ctrl = new AbortController();
    const p = run(a, ctrl.signal);
    await new Promise((r) => setTimeout(r, 5));
    ctrl.abort();
    const r = await p;
    expect(r.err?.status).toBe(0);
    expect(r.err?.code).toBe("aborted");
  });

  test("уже отменённый сигнал — запрос не уходит", async () => {
    const { a, server } = await apiWith(() => sse([ev("done", DONE)]).res);
    const ctrl = new AbortController();
    ctrl.abort();
    const r = await run(a, ctrl.signal);
    expect(r.err?.code).toBe("aborted");
    expect(server.calls("POST", "/chat/messages")).toHaveLength(0);
  });

  test("отмена после done — ничего не меняет (итог уже получен)", async () => {
    const { a } = await apiWith(() => sse([ev("delta", { text: "раз" }), ev("done", DONE)]).res);
    const ctrl = new AbortController();
    const r = await run(a, ctrl.signal);
    ctrl.abort();
    expect(r.out?.kind).toBe("done");
  });
});

describe("A8 2.2 — кризис в потоке (ТЗ §9.3)", () => {
  test.each([
    ["data не JSON", "event: crisis\ndata: {oops\n\n"],
    ["data пустая", "event: crisis\ndata:\n\n"],
    ["text числом, contacts строкой", ev("crisis", { message_id: 1, text: 5, replace: "yes", contacts: "112" })],
    ["text из пробелов, replace не boolean", ev("crisis", { message_id: null, text: "   ", replace: null, contacts: [] })],
  ])("испорченное событие crisis (%s) — всё равно кризис, text null, replace true", async (_n, raw) => {
    const { a } = await apiWith(() => sse([ev("delta", { text: "раз" }), raw]).res);
    const r = await run(a);
    expect(r.out?.kind).toBe("crisis");
    if (r.out?.kind !== "crisis") return;
    expect(r.out.data.text).toBeNull();
    expect(r.out.data.replace).toBe(true);
    expect(r.out.data.contacts).toEqual([]);
  });

  test("испорченный delta перед crisis — поток испорчен раньше кризиса: bad_response (экран сверяет историю)", async () => {
    const { a } = await apiWith(() => sse([ev("delta", "oops"), ev("crisis", { message_id: null, text: "Протокол", replace: true, contacts: [] })]).res);
    const r = await run(a);
    expect(r.err?.code).toBe("bad_response");
  });
});

describe("A8 2.2 — 429 и Retry-After (ТЗ §10; контракт RateLimited: целое ≥ 1)", () => {
  const r429 = (h?: string) => () =>
    json(429, { code: "rate_limited", message: "Лимит" }, h === undefined ? {} : { "Retry-After": h });

  test.each([
    ["3600", 3600],
    [" 60 ", 60],
    ["0", undefined],
    ["-5", undefined],
    ["1.5", undefined],
    ["Wed, 21 Oct 2026 07:28:00 GMT", undefined],
    ["abc", undefined],
    [undefined, undefined],
  ])("Retry-After %j → retryAfter %j; код rate_limited, статус 429", async (h, want) => {
    const { a } = await apiWith(r429(h));
    const r = await run(a);
    expect(r.err?.status).toBe(429);
    expect(r.err?.code).toBe("rate_limited");
    expect(r.err?.retryAfter).toBe(want);
    expect(r.err?.message).toMatch(/лимит/i);
  });

  test("время по Retry-After через полночь — «завтра после 00:10 (через 20 мин)»", () => {
    const now = new Date(2026, 10, 26, 23, 50, 0).getTime();
    expect(limitUntilText(20 * 60, now)).toBe("завтра после 00:10 (через 20 мин)");
  });

  test("Retry-After на 3 дня — дата, а не «завтра»", () => {
    const now = new Date(2026, 10, 26, 12, 0, 0).getTime();
    expect(limitUntilText(3 * 86_400, now)).toBe("29 ноября после 12:00 (через 72 ч)");
  });

  test("Retry-After 1 с — «после ЧЧ:ММ (через 1 с)», сегодня", () => {
    const now = new Date(2026, 10, 26, 12, 0, 0).getTime();
    expect(limitUntilText(1, now)).toBe("после 12:00 (через 1 с)");
  });

  // A8-2.2-D1: огромный Retry-After (число цифрами, проходит /^\d+$/) — дата за пределом Date: на экране
  // «Invalid Date после Invalid Date (через … ч)». Ожидается: понятный текст без «Invalid Date» (ТЗ §10, §8).
  test("A8-2.2-D1: огромный Retry-After — без «Invalid Date» в тексте лимита", () => {
    const now = new Date(2026, 10, 26, 12, 0, 0).getTime();
    const t = limitUntilText(99_999_999_999_999, now);
    expect(t).not.toMatch(/Invalid|NaN/);
  });
});

describe("A8 2.2 — история: курсор и 422 (контракт, Б-62)", () => {
  test("курсор со спецсимволами кодируется в адресе и возвращается серверу как был", async () => {
    const seen: string[] = [];
    const server = new FakeServer().onboarded();
    const a = new Api({
      initData: () => INIT_DATA,
      fetch: async (input, init) => {
        const u = new URL(String(input), "http://app.test");
        if (u.pathname.endsWith("/chat/messages")) {
          seen.push(u.searchParams.get("cursor") ?? "");
          return json(200, { items: [], next_cursor: null });
        }
        return server.fetch(input, init);
      },
    });
    await a.login();
    await a.chatHistory({ cursor: "a+b/c=&d?e#f", limit: 30 });
    expect(seen).toEqual(["a+b/c=&d?e#f"]);
  });

  test.each([
    ["next_cursor пустая строка", { items: [], next_cursor: "" }],
    ["нет next_cursor", { items: [] }],
    ["items объект", { items: {}, next_cursor: null }],
    ["автор partner", { items: [{ id: "1", author: "partner", text: "чужое", created_at: "2026-11-20T10:00:00Z" }], next_cursor: null }],
    ["text объект не UnavailableText", { items: [{ id: "1", author: "agent", text: { html: "<b>x</b>" }, created_at: "2026-11-20T10:00:00Z" }], next_cursor: null }],
    ["UnavailableText с чужой причиной", { items: [{ id: "1", author: "agent", text: { unavailable: true, reason: "hack" }, created_at: "2026-11-20T10:00:00Z" }], next_cursor: null }],
    ["created_at не дата", { items: [{ id: "1", author: "owner", text: "x", created_at: "вчера" }], next_cursor: null }],
    ["id пустой", { items: [{ id: "", author: "owner", text: "x", created_at: "2026-11-20T10:00:00Z" }], next_cursor: null }],
    ["тело массив", []],
  ])("страница не по схеме (%s) — bad_response «История пришла с ошибкой»", async (_n, body) => {
    const server = new FakeServer().onboarded();
    server.inject("GET", "/chat/messages", 200, body);
    const a = new Api({ initData: () => INIT_DATA, fetch: server.fetch });
    await a.login();
    const e = (await a.chatHistory({ limit: 30 }).catch((x: unknown) => x)) as ApiError;
    expect(e).toBeInstanceOf(ApiError);
    expect(e.code).toBe("bad_response");
    expect(e.message).toMatch(/История пришла с ошибкой/);
  });
});
