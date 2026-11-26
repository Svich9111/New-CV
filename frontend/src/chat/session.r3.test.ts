/**
 * A4, задача 2.2, раунд 3 — исправления по ревью и тесту р.2: паузы 409 по умолчанию (MINOR-N4), «Повторить»
 * после окна лимита (MINOR-N5), пустой текст по правилам сервера (A8-2.2-D6), ключ пузыря после 422 (A8-2.2-D7),
 * своя реплика за пределами первой страницы при сверке (О-4). ФТ-05; ТЗ §10; АР-51. Тексты выдуманные.
 */
import { Api } from "../api/client";
import { FakeServer, delta } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";
import { ChatSession, REPLY_WAIT_MS, SCAN_PAGES, isBlank, newMessageKey, visibleItems, type ChatItem, type ChatSessionOptions } from "./session";

type Hook = (method: string) => void;

async function mk(server = new FakeServer().onboarded(), opts: ChatSessionOptions = {}, hook?: { fn: Hook | null }) {
  const api = new Api({
    initData: () => INIT_DATA,
    fetch: async (input, init) => {
      const u = new URL(String(input), "http://app.test");
      if (hook?.fn && u.pathname.endsWith("/chat/messages")) hook.fn(init?.method ?? "GET");
      return server.fetch(input, init);
    },
  });
  await api.login();
  const chat = new ChatSession(api, { replyWaitMs: [5, 5, 5], ...opts });
  chat.open();
  chat.start();
  await vi.waitFor(() => expect(chat.getSnapshot().history).not.toBe("loading"));
  return { chat, server };
}

const idle = (chat: ChatSession) => vi.waitFor(() => expect(chat.getSnapshot().phase).toBe("idle"));
const posts = (server: FakeServer) => server.calls("POST", "/chat/messages");
const gets = (server: FakeServer) => server.calls("GET", "/chat/messages");
const keyOf = (body: unknown) => (body as { client_message_id?: string }).client_message_id;
const mine = (chat: ChatSession, key: string): ChatItem => chat.getSnapshot().items.find((it) => it.key === key)!;
const shown = (chat: ChatSession) => visibleItems(chat.getSnapshot().items).map((it) => `${it.author}:${it.text}`);

async function say(chat: ChatSession, text: string): Promise<string> {
  chat.setInput(text);
  expect(chat.send()).toBe(true);
  const key = [...chat.getSnapshot().items].reverse().find((it) => it.author === "owner")!.key;
  await idle(chat);
  return key;
}

describe("MINOR-N4 — паузы перед автоповтором после 409 по умолчанию", () => {
  test("REPLY_WAIT_MS = 3, 6 и 10 с", () => {
    expect(REPLY_WAIT_MS).toEqual([3000, 6000, 10000]);
  });

  describe("на поддельных часах", () => {
    afterEach(() => {
      vi.useRealTimers();
    });

    /** Дать доработать запросам к заглушке: таймеры «сейчас», обещания и очередь Node. */
    async function settle() {
      for (let i = 0; i < 30; i++) {
        await vi.advanceTimersByTimeAsync(0);
        await new Promise((r) => setImmediate(r));
      }
    }

    test("сессия без настроек ждёт ровно 3, 6 и 10 с, потом «Повторить» вручную, без новых запросов", async () => {
      const server = new FakeServer().onboarded();
      server.reply({ steps: [delta("Нач")], end: "error" });
      const api = new Api({ initData: () => INIT_DATA, fetch: server.fetch });
      await api.login();
      const chat = new ChatSession(api);
      chat.open();
      chat.start();
      await vi.waitFor(() => expect(chat.getSnapshot().history).toBe("loaded"));
      const key = await say(chat, "Раз");
      server.leaseFor(mine(chat, key).id!, 1000);

      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
      void chat.retry(key);
      await settle();
      // Повтор и немедленный повтор клиента (Api) — оба 409; затем пауза.
      expect(posts(server)).toHaveLength(3);
      expect(chat.getSnapshot().phase).toBe("waiting");
      for (const [i, ms] of [3000, 6000, 10000].entries()) {
        await vi.advanceTimersByTimeAsync(ms - 1);
        await settle();
        expect(posts(server), `пауза ${i + 1}: до ${ms} мс`).toHaveLength(3 + 2 * i);
        await vi.advanceTimersByTimeAsync(1);
        await settle();
        expect(posts(server), `пауза ${i + 1}: в ${ms} мс`).toHaveLength(5 + 2 * i);
      }
      expect(chat.getSnapshot().phase).toBe("idle");
      expect(mine(chat, key).status).toMatchObject({ kind: "unanswered", retry: true });
      await vi.advanceTimersByTimeAsync(60_000);
      await settle();
      expect(posts(server)).toHaveLength(9);
      expect(new Set(posts(server).map((c) => keyOf(c.body))).size).toBe(1);
    });
  });
});

describe("MINOR-N5 — реплика под лимитом: «Повторить» после окна тем же ключом", () => {
  test("до окна повтора нет; после окна (по Retry-After) — тот же ключ, ответ на ту же реплику, без дубля", async () => {
    let t = 1_000_000;
    const server = new FakeServer().onboarded();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "60" } });
    const { chat } = await mk(server, { now: () => t });
    const key = await say(chat, "Раз");
    expect(mine(chat, key).status).toEqual({ kind: "limited", retryAt: 1_060_000 });
    expect(chat.getSnapshot().limit).not.toBeNull();
    t += 59_999;
    await chat.retry(key);
    expect(posts(server)).toHaveLength(1);
    t += 1;
    await chat.retry(key);
    await idle(chat);
    const keys = posts(server).map((c) => keyOf(c.body));
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
    expect(mine(chat, key).status).toEqual({ kind: "answered" });
    expect(chat.getSnapshot().limit).toBeNull();
    expect(server.chat.filter((m) => m.author === "owner")).toHaveLength(1);
    expect(shown(chat)).toEqual(["owner:Раз", "agent:Слышу тебя."]);
  });

  test("без Retry-After срок окна неизвестен — повтора нет («завтра»)", async () => {
    let t = 0;
    const server = new FakeServer().onboarded();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" } });
    const { chat } = await mk(server, { now: () => t });
    const key = await say(chat, "Раз");
    expect(mine(chat, key).status).toEqual({ kind: "limited" });
    t += 7 * 86_400_000;
    await chat.retry(key);
    expect(posts(server)).toHaveLength(1);
  });

  test("повтор после окна снова упёрся в лимит — снова «лимит» с новым сроком, ключ тот же", async () => {
    let t = 0;
    const server = new FakeServer().onboarded();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "10" } });
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "30" } });
    const { chat } = await mk(server, { now: () => t });
    const key = await say(chat, "Раз");
    t = 10_000;
    await chat.retry(key);
    await idle(chat);
    expect(mine(chat, key).status).toEqual({ kind: "limited", retryAt: 40_000 });
    const keys = posts(server).map((c) => keyOf(c.body));
    expect(keys[1]).toBe(keys[0]);
  });
});

describe("A8-2.2-D6 — пустой текст по правилам сервера (Python str.strip)", () => {
  // Все символы, которые Python 3.12 `str.isspace()` считает пробелами (их убирает `str.strip()`).
  const PY_SPACE = [
    ..."\t\n\v\f\r\u001c\u001d\u001e\u001f \u0085  ",
    ...Array.from({ length: 11 }, (_, i) => String.fromCharCode(0x2000 + i)),
    ..."    　",
  ];

  test("каждый пробельный символ Python — пусто; вместе и с BOM — тоже; буква, ZWSP, U+180E — не пусто", () => {
    for (const ch of PY_SPACE) expect(isBlank(ch), ch.codePointAt(0)!.toString(16)).toBe(true);
    expect(isBlank(PY_SPACE.join(""))).toBe(true);
    expect(isBlank("")).toBe(true);
    expect(isBlank("﻿")).toBe(true);
    for (const t of ["а", " а ", "​", "᠎", "\u0000", "\u001b"]) expect(isBlank(t), JSON.stringify(t)).toBe(false);
  });

  test("реплика из любого из них не уходит; поле ввода не очищается", async () => {
    const { chat, server } = await mk();
    for (const ch of PY_SPACE) {
      chat.setInput(`${ch}${ch}`);
      expect(chat.send()).toBe(false);
      expect(chat.getSnapshot().input).toBe(`${ch}${ch}`);
    }
    expect(posts(server)).toHaveLength(0);
  });

  test("422 empty_text — «сообщение пустое», без повтора (а не «сервер не принял, отправь как новое»)", async () => {
    const { chat, server } = await mk();
    server.inject("POST", "/chat/messages", 422, { code: "empty_text", message: "x" });
    const key = await say(chat, "​");
    const st = mine(chat, key).status;
    expect(st).toMatchObject({ kind: "unsent", retry: false });
    expect(st && "note" in st ? st.note : "").toMatch(/^Сообщение пустое/);
    expect(st && "note" in st ? st.note : "").not.toMatch(/не принял/);
  });

  test("422 validation_error на непустой текст — по-прежнему ключ отклонён, повтор с новым ключом", async () => {
    const { chat, server } = await mk();
    server.inject("POST", "/chat/messages", 422, { code: "validation_error", message: "x" });
    const key = await say(chat, "Раз");
    expect(mine(chat, key).status).toMatchObject({ kind: "unsent", retry: true, note: expect.stringMatching(/не принял/) });
  });
});

describe("A8-2.2-D7 — после 422 новый ключ и у пузыря", () => {
  test("ключ пузыря равен ключу следующего POST; сверка после обрыва — «Не отправлено», без id чужой записи", async () => {
    const taken = newMessageKey();
    const server = new FakeServer().onboarded();
    const foreign = server.addChat("owner", "Чужой текст", undefined, { client_message_id: taken });
    const keys = [taken];
    const { chat } = await mk(server, { newKey: () => keys.shift() ?? newMessageKey() });
    const key = await say(chat, "Раз");
    const fresh = mine(chat, key).cmid;
    expect(fresh).not.toBe(taken);
    server.failNetwork("POST", "/chat/messages");
    await chat.retry(key);
    await idle(chat);
    expect(keyOf(posts(server)[1].body)).toBe(fresh);
    expect(mine(chat, key)).toMatchObject({ id: null, cmid: fresh, status: { kind: "unsent" } });
    expect(chat.getSnapshot().items.filter((it) => it.id === foreign.id)).toHaveLength(1);
    await chat.retry(key);
    await idle(chat);
    expect(keyOf(posts(server)[2].body)).toBe(fresh);
    expect(mine(chat, key).status).toEqual({ kind: "answered" });
  });
});

describe("О-4 — своя реплика ушла с первой страницы истории", () => {
  /** Обрыв после сохранения реплики; перед сверкой с «телефона» приходит `n` реплик. */
  async function pushedOut(n: number, opts: { answered?: boolean; seed?: number } = {}) {
    const server = new FakeServer().onboarded();
    if (opts.seed) server.seedChat(opts.seed);
    const hook: { fn: Hook | null } = { fn: null };
    const { chat } = await mk(server, {}, hook);
    server.reply({ dropResponse: opts.answered ? "answered" : true });
    hook.fn = (method) => {
      if (method !== "GET") return;
      hook.fn = null;
      for (let i = 0; i < n; i++) server.addChat("owner", `С телефона ${i}`, undefined, { client_message_id: newMessageKey() });
    };
    const before = gets(server).length;
    const key = await say(chat, "Своя");
    return { chat, server, key, scans: () => gets(server).length - before };
  }

  test("на второй странице — «сохранено, ответа нет», пузырь на месте; повтор тем же ключом — ответ, без дубля", async () => {
    const { chat, server, key, scans } = await pushedOut(31);
    expect(mine(chat, key).status).toMatchObject({ kind: "unanswered", retry: true });
    expect(scans()).toBe(2);
    expect(mine(chat, key).id).toBe(server.chat.find((m) => m.text === "Своя")!.id);
    expect(shown(chat).at(-1)).toBe("owner:Своя");
    await chat.retry(key);
    await idle(chat);
    expect(new Set(posts(server).map((c) => keyOf(c.body))).size).toBe(1);
    expect(server.chat.filter((m) => m.text === "Своя")).toHaveLength(1);
    expect(shown(chat).slice(-2)).toEqual(["owner:Своя", "agent:Слышу тебя."]);
  });

  test("ответ сохранён, реплика на второй странице — «ответ есть», ответ показан под ней", async () => {
    const { chat, key } = await pushedOut(40, { answered: true });
    expect(mine(chat, key).status).toEqual({ kind: "answered" });
    expect(shown(chat).slice(-2)).toEqual(["owner:Своя", "agent:Ответ, который не дошёл."]);
  });

  test("на первой странице есть запись из ленты до отправки — дальше не ищем, «не отправлено» верно", async () => {
    const server = new FakeServer().onboarded().seedChat(4);
    const { chat } = await mk(server);
    server.failNetwork("POST", "/chat/messages");
    const before = gets(server).length;
    const key = await say(chat, "Своя");
    expect(mine(chat, key).status?.kind).toBe("unsent");
    expect(gets(server).length - before).toBe(1);
  });

  test("реплики нет, история длинная, ленты до отправки не было — ищем до конца истории, «не отправлено»", async () => {
    const server = new FakeServer().onboarded();
    const hook: { fn: Hook | null } = { fn: null };
    const { chat } = await mk(server, {}, hook);
    server.failNetwork("POST", "/chat/messages");
    hook.fn = (method) => {
      if (method !== "GET") return;
      hook.fn = null;
      server.seedChat(70);
    };
    const key = await say(chat, "Своя");
    expect(mine(chat, key).status?.kind).toBe("unsent");
    expect(gets(server)).toHaveLength(1 + 3);
  });

  test(`дальше ${SCAN_PAGES} страниц не ищем — «неизвестно», повтор сначала проверит историю`, async () => {
    const { chat, key, scans } = await pushedOut(PAGE_SIZE * (SCAN_PAGES + 1) + 1);
    expect(mine(chat, key).status).toMatchObject({ kind: "unknown", note: expect.stringMatching(/Не удалось проверить/) });
    expect(scans()).toBe(1 + SCAN_PAGES);
  });

  test("более старая страница не загрузилась — «неизвестно», а не «не отправлено»", async () => {
    const server = new FakeServer().onboarded();
    const hook: { fn: Hook | null } = { fn: null };
    const { chat } = await mk(server, {}, hook);
    server.reply({ dropResponse: true });
    let n = 0;
    hook.fn = (method) => {
      if (method !== "GET") return;
      n += 1;
      if (n === 1) for (let i = 0; i < 31; i++) server.addChat("owner", `С телефона ${i}`, undefined, { client_message_id: newMessageKey() });
      if (n === 2) {
        hook.fn = null;
        throw new TypeError("Failed to fetch");
      }
    };
    const key = await say(chat, "Своя");
    expect(n).toBe(2);
    expect(mine(chat, key).status).toMatchObject({ kind: "unknown", note: expect.stringMatching(/Не удалось проверить/) });
  });
});

const PAGE_SIZE = 30;
