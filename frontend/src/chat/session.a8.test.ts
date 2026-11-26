/**
 * A8, задача 2.2, раунд 1 — `ChatSession` без экрана: повтор без дубля реплики (все четыре исхода сверки с
 * историей), одинаковые реплики подряд, гонки с медленной историей, кризис после перезагрузки истории, лимит.
 * ФТ-05; ТЗ §4.2, §9.3, §10; контракт АР-32 (повтор того же текста — новая реплика), Б-62. Тексты выдуманные.
 */
import { Api } from "../api/client";
import { FakeServer, crisis, delta, done, gate } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";
import { ChatSession, visibleItems, type ChatState } from "./session";

type Hold = { open: () => void };

/** Сессия чата на заглушке сервера; `holdGet` — следующий `GET /chat/messages` ждёт `open()`. */
async function mk(server = new FakeServer().onboarded()) {
  const holds: Array<{ match: (q: string) => boolean; p: Promise<void> }> = [];
  const hold = (match: (q: string) => boolean): Hold => {
    let open = () => undefined as void;
    const p = new Promise<void>((r) => (open = r));
    holds.push({ match, p });
    return { open };
  };
  const api = new Api({
    initData: () => INIT_DATA,
    fetch: async (input, init) => {
      const u = new URL(String(input), "http://app.test");
      if (u.pathname.endsWith("/chat/messages")) {
        const i = holds.findIndex((h) => h.match(`${init?.method ?? "GET"} ${u.search}`));
        if (i >= 0) {
          const [h] = holds.splice(i, 1);
          await h.p;
        }
      }
      return server.fetch(input, init);
    },
  });
  await api.login();
  const chat = new ChatSession(api);
  chat.open();
  chat.start();
  await vi.waitFor(() => expect(chat.getSnapshot().history).not.toBe("loading"));
  return { chat, server, hold };
}

const idle = (chat: ChatSession) => vi.waitFor(() => expect(chat.getSnapshot().phase).toBe("idle"));
const shown = (s: ChatState) => visibleItems(s.items).map((it) => `${it.author}:${it.text}`);
const owners = (server: FakeServer, text: string) => server.chat.filter((m) => m.author === "owner" && m.text === text).length;
const posts = (server: FakeServer) => server.calls("POST", "/chat/messages").length;
const gets = (server: FakeServer) => server.calls("GET", "/chat/messages").length;

async function say(chat: ChatSession, text: string) {
  chat.setInput(text);
  expect(chat.send()).toBe(true);
  await idle(chat);
}

const ownerItem = (chat: ChatSession, text: string) =>
  [...chat.getSnapshot().items].reverse().find((it) => it.author === "owner" && it.text === text)!;

describe("A8 2.2 — сверка с историей: четыре исхода (АР-32, карточка «Повтор без дубля»)", () => {
  test("answered: сервер сохранил и ответ, до клиента не дошло ничего — ответ из истории, второй отправки нет", async () => {
    const { chat, server } = await mk();
    server.reply({ dropResponse: "answered" });
    await say(chat, "Мне тревожно");
    expect(shown(chat.getSnapshot())).toEqual(["owner:Мне тревожно", "agent:Ответ, который не дошёл."]);
    expect(ownerItem(chat, "Мне тревожно").status).toEqual({ kind: "answered" });
    expect(posts(server)).toBe(1);
    expect(chat.getSnapshot().live).toBe("Ответ агента: Ответ, который не дошёл.");
  });

  test("unanswered: обрыв после кусков — «сохранено, ответа нет», повтор тем же пузырём; на экране одна реплика", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Слышу")], end: "error" });
    await say(chat, "Мне тревожно");
    const it = ownerItem(chat, "Мне тревожно");
    expect(it.status?.kind).toBe("unanswered");
    expect(shown(chat.getSnapshot())).toEqual(["owner:Мне тревожно", "agent:Слышу"]);
    expect(chat.getSnapshot().items.find((x) => x.key === `${it.key}-reply`)?.partial).toBe("interrupted");
    await chat.retry(it.key);
    await idle(chat);
    expect(posts(server)).toBe(2);
    expect(owners(server, "Мне тревожно")).toBe(2); // сервер хранит обе попытки (АР-32)
    expect(shown(chat.getSnapshot())).toEqual(["owner:Мне тревожно", "agent:Слышу тебя."]);
  });

  test("unsent: сервер реплику не получил — «Не отправлено», повтор отправляет один раз", async () => {
    const { chat, server } = await mk();
    server.failNetwork("POST", "/chat/messages");
    await say(chat, "Мне тревожно");
    const it = ownerItem(chat, "Мне тревожно");
    expect(it.status).toMatchObject({ kind: "unsent", retry: true });
    expect(owners(server, "Мне тревожно")).toBe(0);
    await chat.retry(it.key);
    await idle(chat);
    expect(owners(server, "Мне тревожно")).toBe(1);
    expect(shown(chat.getSnapshot())).toEqual(["owner:Мне тревожно", "agent:Слышу тебя."]);
  });

  test("unknown → повтор: сначала история; сервер успел ответить — второй отправки нет", async () => {
    const { chat, server } = await mk();
    server.reply({ dropResponse: "answered" });
    server.failNetwork("GET", "/chat/messages");
    await say(chat, "Мне тревожно");
    const it = ownerItem(chat, "Мне тревожно");
    expect(it.status?.kind).toBe("unknown");
    const before = gets(server);
    await chat.retry(it.key);
    await idle(chat);
    expect(gets(server)).toBe(before + 1);
    expect(posts(server)).toBe(1);
    expect(shown(chat.getSnapshot())).toEqual(["owner:Мне тревожно", "agent:Ответ, который не дошёл."]);
  });

  test("unknown → повтор: история снова не читается — остаётся unknown, POST не уходит", async () => {
    const { chat, server } = await mk();
    server.failNetwork("POST", "/chat/messages");
    server.failNetwork("GET", "/chat/messages", 2);
    await say(chat, "Мне тревожно");
    const it = ownerItem(chat, "Мне тревожно");
    await chat.retry(it.key);
    await idle(chat);
    expect(posts(server)).toBe(1);
    expect(ownerItem(chat, "Мне тревожно").status?.kind).toBe("unknown");
  });

  test("unknown → повтор: реплики в истории нет — отправка один раз", async () => {
    const { chat, server } = await mk();
    server.failNetwork("POST", "/chat/messages");
    server.failNetwork("GET", "/chat/messages");
    await say(chat, "Мне тревожно");
    await chat.retry(ownerItem(chat, "Мне тревожно").key);
    await idle(chat);
    expect(posts(server)).toBe(2);
    expect(owners(server, "Мне тревожно")).toBe(1);
    expect(shown(chat.getSnapshot())).toEqual(["owner:Мне тревожно", "agent:Слышу тебя."]);
  });

  test("повтор недоступен во время ответа и для чужого ключа", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Слышу")], end: "error" });
    await say(chat, "Раз");
    const it = ownerItem(chat, "Раз");
    const g = gate();
    server.reply({ steps: [g.step, done()] });
    await chat.retry(it.key);
    await chat.retry(it.key); // второй повтор, пока первый идёт
    await chat.retry("local-999");
    g.open();
    await idle(chat);
    expect(posts(server)).toBe(2);
  });
});

describe("A8 2.2 — одинаковые реплики подряд", () => {
  test("«да» с ответом, затем снова «да» с ответом — оба пузыря видны (не свёрнуты)", async () => {
    const { chat } = await mk();
    await say(chat, "да");
    await say(chat, "да");
    expect(shown(chat.getSnapshot())).toEqual(["owner:да", "agent:Слышу тебя.", "owner:да", "agent:Слышу тебя."]);
  });

  test("два «да» с ответом, третье «да» — обрыв, сервер ответил: сверка не путает прежние «да» с новым", async () => {
    const { chat, server } = await mk();
    await say(chat, "да");
    await say(chat, "да");
    server.reply({ dropResponse: "answered" });
    await say(chat, "да");
    expect(shown(chat.getSnapshot())).toEqual([
      "owner:да",
      "agent:Слышу тебя.",
      "owner:да",
      "agent:Слышу тебя.",
      "owner:да",
      "agent:Ответ, который не дошёл.",
    ]);
    expect(posts(server)).toBe(3);
  });

  test("два «да» с ответом, третье «да» не дошло до сервера — «Не отправлено», а не «ответ есть»", async () => {
    const { chat, server } = await mk();
    await say(chat, "да");
    await say(chat, "да");
    server.failNetwork("POST", "/chat/messages");
    await say(chat, "да");
    expect(ownerItem(chat, "да").status?.kind).toBe("unsent");
    expect(shown(chat.getSnapshot())).toHaveLength(5);
  });

  test("«да» из прошлого сеанса в истории (с ответом); новое «да» не дошло — не принимается за сохранённое", async () => {
    const server = new FakeServer().onboarded();
    server.addChat("owner", "да");
    server.addChat("agent", "Ответ в прошлый раз");
    const { chat } = await mk(server);
    server.failNetwork("POST", "/chat/messages");
    await say(chat, "да");
    expect(ownerItem(chat, "да").status?.kind).toBe("unsent");
  });

  test("после 429 (реплика сохранена без id у клиента) то же «да» обрывается без ответа — сохранённое при 429 не выдаётся за новое", async () => {
    const { chat, server } = await mk();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "60" } });
    await say(chat, "да");
    server.failNetwork("POST", "/chat/messages");
    await say(chat, "да");
    expect(ownerItem(chat, "да").status?.kind).toBe("unsent");
    expect(owners(server, "да")).toBe(1);
  });
});

describe("A8 2.2 — гонки с медленной историей", () => {
  test("первая страница истории идёт медленно — отправка не уходит, ввод сохранён; после загрузки — уходит", async () => {
    const server = new FakeServer().onboarded();
    const api = new Api({ initData: () => INIT_DATA, fetch: server.fetch });
    await api.login();
    let release = () => undefined as void;
    const slow = new Promise<void>((r) => (release = r));
    const orig = api.chatHistory.bind(api);
    vi.spyOn(api, "chatHistory").mockImplementation(async (o) => {
      await slow;
      return orig(o);
    });
    const chat = new ChatSession(api);
    chat.open();
    chat.start();
    chat.setInput("Мне тревожно");
    expect(chat.send()).toBe(false);
    expect(chat.getSnapshot().input).toBe("Мне тревожно");
    release();
    await vi.waitFor(() => expect(chat.getSnapshot().history).toBe("loaded"));
    expect(chat.send()).toBe(true);
    await idle(chat);
    expect(posts(server)).toBe(1);
  });

  test("медленная старая страница (успех) приходит посреди ответа — ответ и реплика на месте, без повторов", async () => {
    const server = new FakeServer().onboarded().seedChat(40);
    const { chat, hold } = await mk(server);
    const h = hold((q) => q.startsWith("GET") && q.includes("cursor="));
    void chat.loadOlder();
    const g = gate();
    server.reply({ steps: [delta("Слышу "), g.step, delta("тебя."), done()] });
    chat.setInput("Новая реплика");
    expect(chat.send()).toBe(true);
    await vi.waitFor(() => expect(chat.getSnapshot().phase).toBe("streaming"));
    h.open();
    await vi.waitFor(() => expect(chat.getSnapshot().olderLoading).toBe(false));
    g.open();
    await idle(chat);
    const s = shown(chat.getSnapshot());
    expect(s).toHaveLength(42);
    expect(s.slice(-2)).toEqual(["owner:Новая реплика", "agent:Слышу тебя."]);
    expect(new Set(chat.getSnapshot().items.map((i) => i.key)).size).toBe(chat.getSnapshot().items.length);
  });

  // A8-2.2-D2: «Показать более ранние» и сразу отправка (кнопка «Отправить» не ждёт старую страницу): старая
  // страница отвечает 422 (смена ключа, Б-62) уже посреди ответа — `loadFirst(true)` заменяет ленту первой
  // страницей сервера и сохраняет только локальные `unsent`/`unknown`. Пузырь отправляемой реплики (`sending`)
  // пропадает, а если сервер её ещё не сохранил — реплики на экране нет вовсе. Ожидается: реплика и ответ на
  // экране ровно по разу (ТЗ §10 «реплика не теряется», ФТ-05).
  test("A8-2.2-D2: 422 по курсору посреди ответа — своя реплика не пропадает с экрана", async () => {
    const server = new FakeServer().onboarded().seedChat(40);
    const { chat, hold } = await mk(server);
    const h = hold((q) => q.startsWith("GET") && q.includes("cursor="));
    void chat.loadOlder();
    server.cursorKey = 2;
    const post = hold((q) => q.startsWith("POST"));
    server.reply({ steps: [delta("Слышу тебя."), done()] });
    chat.setInput("Новая реплика");
    expect(chat.send()).toBe(true);
    // Запрос с репликой ещё в пути (сервер её не сохранил) — а курсор уже отвергнут и лента перезагружена.
    h.open();
    await vi.waitFor(() => expect(chat.getSnapshot().restarted).toBe(true));
    await vi.waitFor(() => expect(chat.getSnapshot().history).toBe("loaded"));
    const during = shown(chat.getSnapshot());
    post.open();
    await idle(chat);
    const s = shown(chat.getSnapshot());
    expect(during).toContain("owner:Новая реплика");
    expect(s.filter((x) => x === "owner:Новая реплика")).toHaveLength(1);
    expect(s.at(-1)).toBe("agent:Слышу тебя.");
  });

  test("сверка после обрыва ждёт медленную историю — в это время ни отправки, ни повтора, ни старой страницы", async () => {
    const server = new FakeServer().onboarded().seedChat(40);
    const { chat, hold } = await mk(server);
    server.reply({ steps: [delta("Слышу")], end: "error" });
    const h = hold((q) => q.startsWith("GET") && !q.includes("cursor="));
    chat.setInput("Раз");
    chat.send();
    await vi.waitFor(() => expect(chat.getSnapshot().phase).toBe("checking"));
    chat.setInput("Два");
    expect(chat.send()).toBe(false);
    await chat.loadOlder();
    await chat.retry(ownerItem(chat, "Раз").key);
    h.open();
    await idle(chat);
    expect(posts(server)).toBe(1);
    expect(server.calls("GET", "/chat/messages").filter((c) => c.query.includes("cursor="))).toHaveLength(0);
    expect(chat.getSnapshot().input).toBe("Два");
  });
});

describe("A8 2.2 — кризис (ТЗ §9.3)", () => {
  test("replace: false, но text не начинается с показанного — показан только text, куски не склеены с ним", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Ну, это мелочи"), crisis("Ответ протокола.", { replace: false })] });
    await say(chat, "Мне плохо");
    expect(shown(chat.getSnapshot())).toEqual(["owner:Мне плохо", "agent:Ответ протокола."]);
  });

  test("контакты под кризисным ответом переживают перезагрузку истории после 422 по курсору", async () => {
    const server = new FakeServer().onboarded().seedChat(40);
    const { chat } = await mk(server);
    server.reply({ steps: [crisis("Ответ протокола.")] });
    await say(chat, "Мне плохо");
    server.cursorKey = 2;
    await chat.loadOlder();
    await vi.waitFor(() => expect(chat.getSnapshot().restarted).toBe(true));
    const c = chat.getSnapshot().items.find((it) => it.text === "Ответ протокола.");
    expect(c?.crisis?.length).toBeGreaterThan(0);
  });

  test("кризис после 429: баннер лимита снят, ввод не заблокирован", async () => {
    const { chat, server } = await mk();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "60" } });
    await say(chat, "Раз");
    expect(chat.getSnapshot().limit).toMatch(/через 1 мин/);
    server.reply({ steps: [crisis("Ответ протокола.")] });
    await say(chat, "Мне очень плохо");
    expect(chat.getSnapshot().limit).toBeNull();
    expect(chat.getSnapshot().items.at(-1)?.crisis).toBeDefined();
  });

  test("429 без Retry-After и с Retry-After: 0 — «завтра» (пустая строка), реплика «сохранено»", async () => {
    for (const h of [undefined, "0"]) {
      const { chat, server } = await mk();
      server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: h === undefined ? {} : { "Retry-After": h } });
      await say(chat, "Раз");
      expect(chat.getSnapshot().limit).toBe("");
      expect(ownerItem(chat, "Раз").status).toEqual({ kind: "limited" });
    }
  });
});
