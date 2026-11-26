/**
 * A8, задача 2.2, раунд 3 — негативные сценарии к исправлениям 0c64a7f: пустой текст по правилам сервера (D6) не
 * делает клиент строже нужного; ключ пузыря после нескольких 422 подряд (D7); записи более старых страниц при
 * сверке не встают в ленту (О-4); окно лимита при огромном `Retry-After` (MINOR-N5). ФТ-05; ТЗ §8, §10; АР-51;
 * карточка 2.2, критерии 2, 4, 6. Тексты выдуманные.
 */
import { Api } from "../api/client";
import { FakeServer } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";
import { ChatSession, newMessageKey, visibleItems, type ChatItem, type ChatSessionOptions } from "./session";

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
const keyOf = (body: unknown) => (body as { client_message_id?: string }).client_message_id;
const textOf = (body: unknown) => (body as { text?: string }).text;
const mine = (chat: ChatSession, key: string): ChatItem => chat.getSnapshot().items.find((it) => it.key === key)!;
const shown = (chat: ChatSession) => visibleItems(chat.getSnapshot().items).map((it) => `${it.author}:${it.text}`);

async function say(chat: ChatSession, text: string): Promise<string> {
  chat.setInput(text);
  expect(chat.send()).toBe(true);
  const key = [...chat.getSnapshot().items].reverse().find((it) => it.author === "owner")!.key;
  await idle(chat);
  return key;
}

describe("A8 р.3 — D6: клиент не строже сервера там, где текст есть", () => {
  test("буква в окружении пробелов Python (U+0085, U+001F, U+3000) уходит, текст — ровно как набран", async () => {
    const { chat, server } = await mk();
    const typed = "\u0085 Привет\u001f　";
    await say(chat, typed);
    expect(posts(server)).toHaveLength(1);
    expect(textOf(posts(server)[0].body)).toBe(typed);
  });

  test("невидимые, но не пробельные для сервера символы (ZWSP, U+180E) уходят — сервер их примет", async () => {
    const { chat, server } = await mk();
    await say(chat, "​");
    await say(chat, "᠎");
    expect(posts(server).map((c) => textOf(c.body))).toEqual(["​", "᠎"]);
  });
});

describe("A8 р.3 — D7: несколько 422 подряд", () => {
  test("каждый 422 — новый ключ и у пузыря; ключи не повторяются; пузырь один; затем ответ под ним", async () => {
    const { chat, server } = await mk();
    server.inject("POST", "/chat/messages", 422, { code: "validation_error", message: "x" }, undefined, 3);
    const key = await say(chat, "Раз");
    for (let i = 0; i < 2; i++) {
      expect(mine(chat, key).status).toMatchObject({ kind: "unsent", retry: true });
      await chat.retry(key);
      await idle(chat);
    }
    const cmidAfter422 = mine(chat, key).cmid;
    await chat.retry(key);
    await idle(chat);
    const keys = posts(server).map((c) => keyOf(c.body));
    expect(keys).toHaveLength(4);
    expect(new Set(keys).size).toBe(4);
    expect(keys[3]).toBe(cmidAfter422);
    expect(mine(chat, key).cmid).toBe(keys[3]);
    expect(mine(chat, key).status).toEqual({ kind: "answered" });
    expect(shown(chat)).toEqual(["owner:Раз", "agent:Слышу тебя."]);
  });
});

describe("A8 р.3 — О-4: более старые страницы сверки только для подписи", () => {
  test("реплика на 2-й странице: записи 2-й страницы в ленту не встают, id в ленте не повторяются", async () => {
    const server = new FakeServer().onboarded();
    const hook: { fn: Hook | null } = { fn: null };
    const { chat } = await mk(server, {}, hook);
    server.reply({ dropResponse: true });
    hook.fn = (method) => {
      if (method !== "GET") return;
      hook.fn = null;
      for (let i = 0; i < 35; i++) server.addChat("owner", `С телефона ${i}`, undefined, { client_message_id: newMessageKey() });
    };
    const key = await say(chat, "Своя");
    expect(mine(chat, key).status?.kind).toBe("unanswered");
    const items = chat.getSnapshot().items;
    // 35 реплик: 30 новейших (5…34) — первая страница, 0…4 — вместе со своей на второй.
    for (let i = 0; i < 5; i++) expect(items.some((it) => it.text === `С телефона ${i}`)).toBe(false);
    for (let i = 5; i < 35; i++) expect(items.filter((it) => it.text === `С телефона ${i}`)).toHaveLength(1);
    const ids = items.map((it) => it.id).filter(Boolean);
    expect(new Set(ids).size).toBe(ids.length);
    expect(items.filter((it) => it.text === "Своя")).toHaveLength(1);
  });
});

describe("A8 р.3 — MINOR-N5: окно лимита", () => {
  test("огромный Retry-After — окно в будущем, повтора нет и через год; дата не NaN", async () => {
    let t = 1_000_000;
    const server = new FakeServer().onboarded();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "99999999999999" } });
    const { chat } = await mk(server, { now: () => t });
    const key = await say(chat, "Раз");
    const st = mine(chat, key).status;
    expect(st?.kind).toBe("limited");
    const at = st && st.kind === "limited" ? st.retryAt : undefined;
    expect(at === undefined || (Number.isFinite(at) && at > t + 365 * 86_400_000)).toBe(true);
    t += 365 * 86_400_000;
    await chat.retry(key);
    expect(posts(server)).toHaveLength(1);
  });

  test("часы устройства ушли назад после 429 — повтора нет, пока окно по этим часам не кончилось", async () => {
    let t = 10_000_000;
    const server = new FakeServer().onboarded();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "60" } });
    const { chat } = await mk(server, { now: () => t });
    const key = await say(chat, "Раз");
    t -= 3_600_000;
    await chat.retry(key);
    expect(posts(server)).toHaveLength(1);
  });
});
