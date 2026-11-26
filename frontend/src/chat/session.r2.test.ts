/**
 * A4, задача 2.2, исправления раунда 1, часть 2 — контракт идемпотентности АР-51: ключ реплики
 * `client_message_id`, событие `accepted`, сверка по ключу и `reply_to` вместо текста, 409 «ответ ещё готовится»,
 * 422 под тем же ключом, кризис в истории (`crisis: true`). Ревью р.1: MAJOR-1, MAJOR-2, MINOR-3, MINOR-4.
 * ФТ-05; ТЗ §9.3, §10. Тексты выдуманные.
 */
import { Api, ApiError } from "../api/client";
import { CONTACTS, FakeServer, crisis, delta, done, errorEvent, gate } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";
import { ChatSession, newMessageKey, visibleItems, type ChatItem, type ChatSessionOptions } from "./session";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

async function mk(server = new FakeServer().onboarded(), opts: ChatSessionOptions = {}) {
  const api = new Api({ initData: () => INIT_DATA, fetch: server.fetch });
  await api.login();
  const chat = new ChatSession(api, { replyWaitMs: [5, 5], ...opts });
  chat.open();
  chat.start();
  await vi.waitFor(() => expect(chat.getSnapshot().history).not.toBe("loading"));
  return { chat, server, api };
}

const idle = (chat: ChatSession) => vi.waitFor(() => expect(chat.getSnapshot().phase).toBe("idle"));
const shown = (chat: ChatSession) => visibleItems(chat.getSnapshot().items).map((it) => `${it.author}:${it.text}`);
const posts = (server: FakeServer) => server.calls("POST", "/chat/messages");
const keyOf = (body: unknown) => (body as { client_message_id?: string }).client_message_id;
const owners = (server: FakeServer, text: string) => server.chat.filter((m) => m.author === "owner" && m.text === text);
const lastOwner = (chat: ChatSession, text: string): ChatItem =>
  [...chat.getSnapshot().items].reverse().find((it) => it.author === "owner" && it.text === text)!;

async function say(chat: ChatSession, text: string) {
  chat.setInput(text);
  expect(chat.send()).toBe(true);
  await idle(chat);
}

describe("Ключ реплики (АР-51)", () => {
  test("у каждой новой реплики свой uuid; «Повторить» и «Отправить ещё раз» — с тем же ключом", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Нач")], end: "error" });
    await say(chat, "Раз");
    await chat.retry(lastOwner(chat, "Раз").key);
    await idle(chat);
    server.failNetwork("POST", "/chat/messages");
    await say(chat, "Два");
    await chat.retry(lastOwner(chat, "Два").key);
    await idle(chat);
    const keys = posts(server).map((c) => keyOf(c.body));
    expect(keys).toHaveLength(4);
    for (const k of keys) expect(k).toMatch(UUID);
    expect(keys[1]).toBe(keys[0]);
    expect(keys[3]).toBe(keys[2]);
    expect(keys[2]).not.toBe(keys[0]);
    expect(owners(server, "Раз")).toHaveLength(1);
    expect(owners(server, "Два")).toHaveLength(1);
  });

  test("newMessageKey: uuid v4; без randomUUID — из getRandomValues", () => {
    expect(newMessageKey()).toMatch(UUID);
    Object.defineProperty(globalThis.crypto, "randomUUID", { configurable: true, value: undefined });
    try {
      const a = newMessageKey();
      const b = newMessageKey();
      expect(a).toMatch(UUID);
      expect(a).not.toBe(b);
    } finally {
      delete (globalThis.crypto as { randomUUID?: unknown }).randomUUID;
    }
    expect(typeof globalThis.crypto.randomUUID).toBe("function");
  });

  test("accepted: пузырь привязан к записи сервера (id), ответ — к пузырю (reply_to)", async () => {
    const { chat, server } = await mk();
    await say(chat, "Раз");
    const own = lastOwner(chat, "Раз");
    expect(own.id).toBe(owners(server, "Раз")[0].id);
    const reply = chat.getSnapshot().items.find((it) => it.author === "agent")!;
    expect(reply.replyTo).toBe(own.id);
    expect(server.answerTo(own.id!)?.id).toBe(reply.id);
  });
});

describe("MAJOR-1: повтор пузыря с id и следующий сбой — без дубля", () => {
  test("«X» (обрыв после 200) → «Повторить» → done → «Y» (нет связи): на экране X, ответ, Y", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Нач")], end: "error" });
    await say(chat, "X");
    expect(lastOwner(chat, "X").status?.kind).toBe("unanswered");
    server.reply({ steps: [delta("R"), done()] });
    await chat.retry(lastOwner(chat, "X").key);
    await idle(chat);
    server.failNetwork("POST", "/chat/messages");
    await say(chat, "Y");
    expect(shown(chat)).toEqual(["owner:X", "agent:R", "owner:Y"]);
    expect(lastOwner(chat, "Y").status?.kind).toBe("unsent");
    expect(owners(server, "X")).toHaveLength(1);
  });

  test("после повтора и нового открытия чата — X один раз, ответ под ним", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Нач")], end: "error" });
    await say(chat, "X");
    await chat.retry(lastOwner(chat, "X").key);
    await idle(chat);
    const again = await mk(server);
    expect(shown(again.chat)).toEqual(["owner:X", "agent:Слышу тебя."]);
    expect(shown(chat)).toEqual(["owner:X", "agent:Слышу тебя."]);
  });
});

describe("MAJOR-2: история не загрузилась — сверка не делает вывода по чужим записям", () => {
  test("в истории «Привет» с ответом, первая загрузка упала, новое «Привет» не дошло — «Не отправлено», старый ответ не под ним", async () => {
    const server = new FakeServer().onboarded();
    server.addChat("owner", "Привет");
    server.addChat("agent", "Старый ответ");
    server.failNetwork("GET", "/chat/messages");
    const { chat } = await mk(server);
    expect(chat.getSnapshot().history).toBe("failed");
    server.failNetwork("POST", "/chat/messages");
    await say(chat, "Привет");
    const own = lastOwner(chat, "Привет");
    expect(own.status).toMatchObject({ kind: "unsent", retry: true });
    expect(own.id).toBeNull();
    // Страница, прочитанная сверкой, и есть первая страница: на экране по порядку, уведомление снято.
    expect(shown(chat)).toEqual(["owner:Привет", "agent:Старый ответ", "owner:Привет"]);
    expect(chat.getSnapshot().history).toBe("loaded");
    expect(chat.getSnapshot().historyError).toBeNull();
    await chat.retry(own.key);
    await idle(chat);
    expect(shown(chat)).toEqual(["owner:Привет", "agent:Старый ответ", "owner:Привет", "agent:Слышу тебя."]);
  });

  test("история не загрузилась, реплика сохранена без ответа — «сохранено, ответа нет», а не «ответ есть»", async () => {
    const server = new FakeServer().onboarded();
    server.addChat("owner", "Привет");
    server.addChat("agent", "Старый ответ");
    server.failNetwork("GET", "/chat/messages");
    const { chat } = await mk(server);
    server.reply({ steps: [delta("Нач")], end: "error" });
    await say(chat, "Привет");
    expect(lastOwner(chat, "Привет").status?.kind).toBe("unanswered");
  });
});

describe("Одинаковые реплики подряд теперь различаются", () => {
  test("«да» (лимит 429) и снова «да» с ответом — два пузыря, ответ под вторым; и при новом открытии", async () => {
    const { chat, server } = await mk();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "60" } });
    await say(chat, "да");
    await say(chat, "да");
    expect(shown(chat)).toEqual(["owner:да", "owner:да", "agent:Слышу тебя."]);
    const again = await mk(server);
    expect(shown(again.chat)).toEqual(["owner:да", "owner:да", "agent:Слышу тебя."]);
  });

  test("«да» не дошло, затем новое «да» с обрывом и сохранённым ответом — ответ привязан ко второму, первое «Не отправлено»", async () => {
    const { chat, server } = await mk();
    server.failNetwork("POST", "/chat/messages");
    await say(chat, "да");
    server.reply({ dropResponse: "answered" });
    await say(chat, "да");
    const items = chat.getSnapshot().items.filter((it) => it.author === "owner");
    expect(items.map((it) => it.status?.kind)).toEqual(["unsent", "answered"]);
    expect(shown(chat)).toEqual(["owner:да", "owner:да", "agent:Ответ, который не дошёл."]);
  });

  test("записи без ключа (до миграции 0018) подряд с одним текстом — один пузырь; с ключом — не сворачиваются", async () => {
    const server = new FakeServer().onboarded();
    server.addChat("owner", "Старое");
    server.addChat("owner", "Старое");
    server.addChat("owner", "Новое", undefined, { client_message_id: newMessageKey() });
    server.addChat("owner", "Новое", undefined, { client_message_id: newMessageKey() });
    const { chat } = await mk(server);
    expect(shown(chat)).toEqual(["owner:Старое", "owner:Новое", "owner:Новое"]);
  });
});

describe("MINOR-4: реплика с другого устройства", () => {
  test("тот же текст с другого устройства с ответом, своя реплика не дошла — «Не отправлено», чужая попытка не своя", async () => {
    const { chat, server } = await mk();
    const g = gate();
    server.reply({ steps: [g.step] });
    chat.setInput("Привет");
    chat.send();
    const key = chat.getSnapshot().items.at(-1)!.key;
    const mine = () => chat.getSnapshot().items.find((it) => it.key === key)!;
    await vi.waitFor(() => expect(mine().id).not.toBeNull());
    // Пока ответа нет, с телефона ушло такое же «Привет» (свой ключ) и получило ответ.
    const other = server.addChat("owner", "Привет", undefined, { client_message_id: newMessageKey() });
    server.addChat("agent", "Ответ на телефоне", undefined, { reply_to: other.id });
    chat.stop();
    g.open();
    await idle(chat);
    // Своя реплика сохранена (accepted), ответа на неё нет; чужая — отдельным пузырём со своим ответом.
    expect(mine().status?.kind).toBe("unanswered");
    expect(shown(chat)).toEqual(["owner:Привет", "owner:Привет", "agent:Ответ на телефоне"]);
    expect(visibleItems(chat.getSnapshot().items)[0].key).toBe(key);
  });

  test("до сохранения своей — с другого устройства пришёл тот же текст; своя не дошла — «Не отправлено»", async () => {
    const { chat, server } = await mk();
    const other = server.addChat("owner", "Привет", undefined, { client_message_id: newMessageKey() });
    server.addChat("agent", "Ответ на телефоне", undefined, { reply_to: other.id });
    server.failNetwork("POST", "/chat/messages");
    await say(chat, "Привет");
    expect(lastOwner(chat, "Привет").status?.kind).toBe("unsent");
    expect(shown(chat)).toEqual(["owner:Привет", "agent:Ответ на телефоне", "owner:Привет"]);
  });
});

describe("Ответ под своей репликой (reply_to)", () => {
  test("в истории ответ на A сохранён после B (повтор A по ключу) — на экране A, ответ A, B, ответ B", async () => {
    const server = new FakeServer().onboarded();
    const a = server.addChat("owner", "A", undefined, { client_message_id: newMessageKey() });
    const b = server.addChat("owner", "B", undefined, { client_message_id: newMessageKey() });
    server.addChat("agent", "ответ B", undefined, { reply_to: b.id });
    server.addChat("agent", "ответ A", undefined, { reply_to: a.id });
    const { chat } = await mk(server);
    expect(shown(chat)).toEqual(["owner:A", "agent:ответ A", "owner:B", "agent:ответ B"]);
  });
});

describe("409 conflict_retry с ключом", () => {
  test("ответ ещё готовится (реплика сохранена) — пауза и повтор с тем же ключом, ответ на ту же реплику", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Нач")], end: "error" });
    await say(chat, "Раз");
    const id = lastOwner(chat, "Раз").id!;
    server.leaseFor(id, 3); // немедленный повтор Api и первый автоповтор получат 409
    await chat.retry(lastOwner(chat, "Раз").key);
    await idle(chat);
    expect(lastOwner(chat, "Раз").status).toEqual({ kind: "answered" });
    expect(shown(chat)).toEqual(["owner:Раз", "agent:Слышу тебя."]);
    const keys = posts(server).map((c) => keyOf(c.body));
    expect(keys).toHaveLength(5);
    expect(new Set(keys).size).toBe(1);
    expect(owners(server, "Раз")).toHaveLength(1);
  });

  test("пока ждали, другой запрос сохранил ответ — сверка показывает его без повторной отправки", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Нач")], end: "error" });
    await say(chat, "Раз");
    const id = lastOwner(chat, "Раз").id!;
    server.leaseFor(id, 2);
    server.addChat("agent", "Готовый ответ", undefined, { reply_to: id });
    await chat.retry(lastOwner(chat, "Раз").key);
    await idle(chat);
    expect(posts(server)).toHaveLength(3);
    expect(shown(chat)).toEqual(["owner:Раз", "agent:Готовый ответ"]);
    expect(chat.getSnapshot().live).toBe("Ответ агента: Готовый ответ");
  });

  test("автоповторы кончились — «ещё готовится» и «Повторить»; ручной повтор получает сохранённый ответ (поток из сохранённого)", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Нач")], end: "error" });
    await say(chat, "Раз");
    const own = lastOwner(chat, "Раз");
    server.leaseFor(own.id!, 100);
    await chat.retry(own.key);
    await idle(chat);
    expect(lastOwner(chat, "Раз").status).toMatchObject({ kind: "unanswered", retry: true, note: expect.stringMatching(/ещё готовится/) });
    expect(posts(server)).toHaveLength(1 + 3 * 2); // попытка и (немедленный повтор Api) × (первая + 2 автоповтора)
    server.releaseLease(own.id!);
    server.addChat("agent", "Ответ другого запроса", undefined, { reply_to: own.id });
    const before = server.chat.length;
    await chat.retry(own.key);
    await idle(chat);
    expect(shown(chat)).toEqual(["owner:Раз", "agent:Ответ другого запроса"]);
    expect(server.chat).toHaveLength(before); // сохранённый ответ отдан повтором, ничего нового не сохранено
  });

  test("«Остановить» во время паузы — автоповтора нет, «Повторить» доступен", async () => {
    const { chat, server } = await mk(undefined, { replyWaitMs: [60_000] });
    server.reply({ steps: [delta("Нач")], end: "error" });
    await say(chat, "Раз");
    const own = lastOwner(chat, "Раз");
    server.leaseFor(own.id!, 2);
    void chat.retry(own.key);
    await vi.waitFor(() => expect(posts(server)).toHaveLength(3));
    await vi.waitFor(() => expect(chat.getSnapshot().phase).toBe("waiting"));
    chat.stop();
    await idle(chat);
    expect(posts(server)).toHaveLength(3);
    expect(lastOwner(chat, "Раз").status).toMatchObject({ kind: "unanswered", retry: true });
  });

  test("409 и в истории реплики нет — ничего не сохранено, «Отправить ещё раз» с тем же ключом", async () => {
    const { chat, server } = await mk();
    server.reply({ status: 409, body: { code: "conflict_retry", message: "x" } });
    server.reply({ status: 409, body: { code: "conflict_retry", message: "x" } });
    await say(chat, "Раз");
    expect(lastOwner(chat, "Раз").status).toMatchObject({ kind: "unsent", retry: true });
    await chat.retry(lastOwner(chat, "Раз").key);
    await idle(chat);
    expect(new Set(posts(server).map((c) => keyOf(c.body))).size).toBe(1);
    expect(shown(chat)).toEqual(["owner:Раз", "agent:Слышу тебя."]);
  });
});

describe("422 под тем же ключом", () => {
  test("под ключом другая реплика — понятная ошибка; повтор уходит с новым ключом как новая реплика", async () => {
    const taken = "11111111-2222-4333-8444-555555555555";
    const server = new FakeServer().onboarded();
    server.addChat("owner", "Чужой текст под ключом", undefined, { client_message_id: taken });
    const keys = [taken];
    const { chat } = await mk(server, { newKey: () => keys.shift() ?? newMessageKey() });
    await say(chat, "Раз");
    const own = lastOwner(chat, "Раз");
    expect(own.status).toMatchObject({ kind: "unsent", retry: true, note: "Сервер не принял это сообщение. Отправь его ещё раз — оно уйдёт как новое." });
    await chat.retry(own.key);
    await idle(chat);
    const sent = posts(server).map((c) => keyOf(c.body));
    expect(sent[0]).toBe(taken);
    expect(sent[1]).toMatch(UUID);
    expect(sent[1]).not.toBe(taken);
    expect(shown(chat)).toEqual(["owner:Чужой текст под ключом", "owner:Раз", "agent:Слышу тебя."]);
  });
});

describe("accepted без id (реплику сохранить не удалось)", () => {
  test("затем error — «Не отправлено», строка о помощи; повтор с тем же ключом сохраняет и отвечает", async () => {
    const { chat, server } = await mk();
    server.reply({ ownerNotSaved: true, steps: [errorEvent("internal")] });
    await say(chat, "Раз");
    const own = lastOwner(chat, "Раз");
    expect(own.status).toMatchObject({ kind: "unsent", retry: true, help: true });
    expect(owners(server, "Раз")).toHaveLength(0);
    await chat.retry(own.key);
    await idle(chat);
    expect(owners(server, "Раз")).toHaveLength(1);
    expect(shown(chat)).toEqual(["owner:Раз", "agent:Слышу тебя."]);
  });

  test("затем crisis — контакты на экране", async () => {
    const { chat, server } = await mk();
    server.reply({ ownerNotSaved: true, steps: [crisis("Ответ протокола.", { id: null })] });
    await say(chat, "Мне плохо");
    expect(chat.getSnapshot().items.at(-1)?.crisis?.length).toBeGreaterThan(0);
  });
});

describe("Кризис в истории (MINOR-3, ТЗ §9.3)", () => {
  test("crisis: true в истории — под ответом контакты из /help/contacts", async () => {
    const server = new FakeServer().onboarded();
    const o = server.addChat("owner", "Мне плохо", undefined, { client_message_id: newMessageKey() });
    server.addChat("agent", "Ответ протокола.", undefined, { reply_to: o.id, crisis: true });
    server.addChat("agent", "Обычный ответ");
    const { chat } = await mk(server);
    await vi.waitFor(() => expect(chat.getSnapshot().items.find((it) => it.text === "Ответ протокола.")?.crisis).toEqual(CONTACTS));
    expect(chat.getSnapshot().items.find((it) => it.text === "Обычный ответ")?.crisis).toBeUndefined();
    expect(server.calls("GET", "/help/contacts")).toHaveLength(1);
  });

  test("контакты не загрузились — блок всё равно есть (пустой список: экран добавит 112)", async () => {
    const server = new FakeServer().onboarded();
    server.addChat("agent", "Ответ протокола.", undefined, { crisis: true });
    server.inject("GET", "/help/contacts", 503, { code: "service_unavailable", message: "x" });
    const { chat } = await mk(server);
    await vi.waitFor(() => expect(server.calls("GET", "/help/contacts")).toHaveLength(1));
    expect(chat.getSnapshot().items[0].crisis).toEqual([]);
  });

  test("кризисный ответ без message_id переживает перезагрузку истории после 422 по курсору", async () => {
    const server = new FakeServer().onboarded().seedChat(40);
    const { chat } = await mk(server);
    server.reply({ steps: [crisis("Ответ протокола.", { id: null })] });
    await say(chat, "Мне плохо");
    server.cursorKey = 2;
    await chat.loadOlder();
    await vi.waitFor(() => expect(chat.getSnapshot().restarted).toBe(true));
    const c = chat.getSnapshot().items.find((it) => it.text === "Ответ протокола.");
    expect(c?.crisis?.length).toBeGreaterThan(0);
    expect(shown(chat).slice(-2)).toEqual(["owner:Мне плохо", "agent:Ответ протокола."]);
  });

  test("кризисный ответ без message_id из прежнего обмена (потом была ещё реплика) переживает перезагрузку после 422", async () => {
    const server = new FakeServer().onboarded().seedChat(40);
    const { chat } = await mk(server);
    server.reply({ steps: [crisis("Ответ протокола.", { id: null })] });
    await say(chat, "Мне плохо");
    await say(chat, "Ещё");
    server.cursorKey = 2;
    await chat.loadOlder();
    await vi.waitFor(() => expect(chat.getSnapshot().restarted).toBe(true));
    const c = chat.getSnapshot().items.find((it) => it.text === "Ответ протокола.");
    expect(c?.crisis?.length).toBeGreaterThan(0);
  });

  test("кризисный ответ из потока при новом открытии — снова с контактами", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [crisis("Ответ протокола.")] });
    await say(chat, "Мне плохо");
    const again = await mk(server);
    await vi.waitFor(() => expect(again.chat.getSnapshot().items.at(-1)?.crisis).toEqual(CONTACTS));
    expect(shown(again.chat)).toEqual(["owner:Мне плохо", "agent:Ответ протокола."]);
    expect(shown(chat)).toEqual(["owner:Мне плохо", "agent:Ответ протокола."]);
  });
});

describe("Гонки", () => {
  test("422 по курсору посреди ответа после accepted — реплика из истории и пузырь — один, ответ под ним", async () => {
    const server = new FakeServer().onboarded().seedChat(40);
    const { chat } = await mk(server);
    const g = gate();
    server.reply({ steps: [delta("Слышу "), g.step, delta("тебя."), done()] });
    // Перезагрузка первой страницы посреди ответа (как после 422 по курсору, Б-62) — реплика уже сохранена.
    chat.setInput("Новая");
    expect(chat.send()).toBe(true);
    await vi.waitFor(() => expect(chat.getSnapshot().phase).toBe("streaming"));
    void (chat as unknown as { loadFirst(r: boolean): Promise<void> }).loadFirst(true);
    await vi.waitFor(() => expect(chat.getSnapshot().restarted).toBe(true));
    await vi.waitFor(() => expect(chat.getSnapshot().history).toBe("loaded"));
    g.open();
    await idle(chat);
    const s = shown(chat);
    expect(s.filter((x) => x === "owner:Новая")).toHaveLength(1);
    expect(s.slice(-2)).toEqual(["owner:Новая", "agent:Слышу тебя."]);
    expect(new Set(chat.getSnapshot().items.map((it) => it.id).filter(Boolean)).size).toBe(
      chat.getSnapshot().items.filter((it) => it.id).length,
    );
  });

  test("повтор по ключу отдал сохранённый ответ, который уже пришёл с историей, — не дважды", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Нач"), { saveAgent: "Сохранённый." }], end: "error" });
    server.failNetwork("GET", "/chat/messages");
    await say(chat, "Раз");
    expect(lastOwner(chat, "Раз").status?.kind).toBe("unknown");
    // Первая страница перечитана (как по «Загрузить переписку снова») — сохранённый ответ уже в ленте.
    void (chat as unknown as { loadFirst(r: boolean): Promise<void> }).loadFirst(false);
    await vi.waitFor(() => expect(shown(chat)).toEqual(["owner:Раз", "agent:Сохранённый."]));
    expect(lastOwner(chat, "Раз").status).toEqual({ kind: "answered" });
  });
});

describe("Клиент: accepted строго по контракту", () => {
  const KEY = "5f0c1e8a-3b7d-4c2e-9a1f-0d4b6e8c2a71";
  async function apiWith(body: string) {
    const server = new FakeServer().onboarded();
    const api = new Api({
      initData: () => INIT_DATA,
      fetch: async (input, init) => {
        if (String(input).endsWith("/chat/messages") && init?.method === "POST") {
          return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
        }
        return server.fetch(input, init);
      },
    });
    await api.login();
    return api;
  }
  const ev = (name: string, data: unknown) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
  const ACC = { message_id: "m1", client_message_id: KEY, replayed: false };
  const DONE = { message_id: "a1", suggest_draft: false };
  async function run(body: string) {
    const api = await apiWith(body);
    const acc: unknown[] = [];
    try {
      return { out: await api.chatSend("Раз", { clientMessageId: KEY, onDelta: () => undefined, onAccepted: (a) => acc.push(a) }), acc };
    } catch (e) {
      if (!(e instanceof ApiError)) throw e;
      return { err: e, acc };
    }
  }

  test("тело запроса — текст и ключ", async () => {
    const server = new FakeServer().onboarded();
    const api = new Api({ initData: () => INIT_DATA, fetch: server.fetch });
    await api.login();
    await api.chatSend("Раз", { clientMessageId: KEY, onDelta: () => undefined });
    expect(server.calls("POST", "/chat/messages")[0].body).toEqual({ text: "Раз", client_message_id: KEY });
    await api.chatSend("Два", { onDelta: () => undefined });
    expect(server.calls("POST", "/chat/messages")[1].body).toEqual({ text: "Два" });
  });

  test("accepted первым — передан экрану; replayed: true — тоже", async () => {
    const r = await run(ev("accepted", { ...ACC, replayed: true }) + ev("delta", { text: "x" }) + ev("done", DONE));
    expect(r.out?.kind).toBe("done");
    expect(r.acc).toEqual([{ messageId: "m1", clientMessageId: KEY, replayed: true }]);
  });

  test("delta или done до accepted, второй accepted, чужой ключ, accepted не по схеме — bad_response", async () => {
    for (const body of [
      ev("delta", { text: "x" }) + ev("done", DONE),
      ev("done", DONE),
      ev("accepted", ACC) + ev("accepted", ACC) + ev("done", DONE),
      ev("accepted", { ...ACC, client_message_id: "00000000-0000-4000-8000-000000000000" }) + ev("done", DONE),
      ev("accepted", { message_id: "m1" }) + ev("done", DONE),
    ]) {
      const r = await run(body);
      expect(r.err?.code, body).toBe("bad_response");
    }
  });

  test("кризис без accepted — всё равно кризис (помощь при любом виде потока, §9.3)", async () => {
    const r = await run(ev("crisis", { message_id: null, text: "Ответ протокола.", replace: true, contacts: CONTACTS }));
    expect(r.out?.kind).toBe("crisis");
  });

  test("повтор по ключу на заглушке: сохранённый ответ — поток из него, без новой записи; кризис — crisis", async () => {
    const server = new FakeServer().onboarded();
    const api = new Api({ initData: () => INIT_DATA, fetch: server.fetch });
    await api.login();
    const first = await api.chatSend("Раз", { clientMessageId: KEY, onDelta: () => undefined });
    const n = server.chat.length;
    const parts: string[] = [];
    const acc: unknown[] = [];
    const again = await api.chatSend("Раз", { clientMessageId: KEY, onDelta: (t) => parts.push(t), onAccepted: (a) => acc.push(a) });
    expect(again).toEqual(first);
    expect(parts).toEqual(["Слышу тебя."]);
    expect(acc).toEqual([{ messageId: server.chat[0].id, clientMessageId: KEY, replayed: true }]);
    expect(server.chat).toHaveLength(n);

    const K2 = "6f0c1e8a-3b7d-4c2e-9a1f-0d4b6e8c2a72";
    server.reply({ steps: [crisis("Ответ протокола.", { replace: true })] });
    await api.chatSend("Плохо", { clientMessageId: K2, onDelta: () => undefined });
    const c = await api.chatSend("Плохо", { clientMessageId: K2, onDelta: () => undefined });
    expect(c.kind).toBe("crisis");
    expect(c.kind === "crisis" && c.data.replace).toBe(false);
  });
});
