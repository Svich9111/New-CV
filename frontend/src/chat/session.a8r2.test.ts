/**
 * A8, задача 2.2, раунд 2 — `ChatSession` на контракте идемпотентности АР-51 (миграция 0018): сверка по ключу
 * и `reply_to`, 409 «ответ ещё готовится» с ожиданием и «Остановить», 422 под ключом, кризис из истории,
 * одинаковые реплики подряд, реплика с другого устройства. ФТ-05; ТЗ §9.3, §10; карточка 2.2, критерии 4–6.
 * Тексты выдуманные. Дефекты — `test.fails` с пометкой A8-2.2-Dn (docs/reviews/2.2-test-r2.md).
 */
import { Api } from "../api/client";
import { CONTACTS, FakeServer, crisis, delta, done, gate } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";
import { ChatSession, newMessageKey, visibleItems, type ChatItem, type ChatSessionOptions } from "./session";

type Hook = (method: string, path: string) => void | Promise<void>;

/** Сессия на заглушке сервера; `hook` вызывается перед каждым запросом к `/chat/messages` (вмешательство теста). */
async function mk(server = new FakeServer().onboarded(), opts: ChatSessionOptions = {}, hook?: { fn: Hook | null }) {
  const api = new Api({
    initData: () => INIT_DATA,
    fetch: async (input, init) => {
      const u = new URL(String(input), "http://app.test");
      if (hook?.fn && u.pathname.endsWith("/chat/messages")) await hook.fn(init?.method ?? "GET", u.pathname + u.search);
      return server.fetch(input, init);
    },
  });
  await api.login();
  const chat = new ChatSession(api, { replyWaitMs: [5, 5, 5], ...opts });
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
const mineByKey = (chat: ChatSession, key: string): ChatItem => chat.getSnapshot().items.find((it) => it.key === key)!;
const lastKey = (chat: ChatSession) => [...chat.getSnapshot().items].reverse().find((it) => it.author === "owner")!.key;

async function say(chat: ChatSession, text: string): Promise<string> {
  chat.setInput(text);
  expect(chat.send()).toBe(true);
  const key = lastKey(chat);
  await idle(chat);
  return key;
}

describe("A8 р.2 — сверка по ключу (АР-51; карточка, критерий 4)", () => {
  test("реплика сохранена, но вытеснена с первой страницы (31 реплика с другого устройства) — «Не отправлено», повтор тем же ключом даёт ответ без дубля", async () => {
    const server = new FakeServer().onboarded();
    const hook: { fn: Hook | null } = { fn: null };
    const { chat } = await mk(server, {}, hook);
    server.reply({ dropResponse: true });
    // Пока ответ «не дошёл», на телефоне написали 31 реплику — своя ушла со страницы сверки.
    hook.fn = (method) => {
      if (method !== "GET") return;
      hook.fn = null;
      for (let i = 0; i < 31; i++) server.addChat("owner", `С телефона ${i}`, undefined, { client_message_id: newMessageKey() });
    };
    const key = await say(chat, "Своя");
    expect(owners(server, "Своя")).toHaveLength(1);
    expect(mineByKey(chat, key).status?.kind).toBe("unsent");
    await chat.retry(key);
    await idle(chat);
    const sent = posts(server).map((c) => keyOf(c.body));
    expect(sent[1]).toBe(sent[0]);
    expect(owners(server, "Своя")).toHaveLength(1);
    expect(shown(chat).filter((x) => x === "owner:Своя")).toHaveLength(1);
    expect(shown(chat).slice(-2)).toEqual(["owner:Своя", "agent:Слышу тебя."]);
    expect(mineByKey(chat, key).status).toEqual({ kind: "answered" });
    expect(shown(chat).filter((x) => x.startsWith("owner:С телефона"))).toHaveLength(30);
    // Повторное открытие (первая страница и более ранняя): своя реплика и ответ — по разу.
    const again = await mk(server);
    await again.chat.loadOlder();
    expect(shown(again.chat).filter((x) => x === "owner:Своя")).toHaveLength(1);
    expect(shown(again.chat).filter((x) => x === "agent:Слышу тебя.")).toHaveLength(1);
  });

  test("обрыв до accepted, сервер сохранил реплику с ответом на неё — ответ по reply_to, а не по соседству; лишних POST нет", async () => {
    const { chat, server } = await mk();
    server.reply({ dropResponse: "answered" });
    // Чужие соседи на странице: перед своей — чужая реплика с ответом, после — ещё одна чужая без ответа.
    const before = server.addChat("owner", "Чужая до", undefined, { client_message_id: newMessageKey() });
    server.addChat("agent", "Ответ на чужую", undefined, { reply_to: before.id });
    const key = await say(chat, "Своя");
    expect(mineByKey(chat, key).status).toEqual({ kind: "answered" });
    expect(posts(server)).toHaveLength(1);
    const items = visibleItems(chat.getSnapshot().items);
    const at = items.findIndex((it) => it.key === key);
    expect(items[at + 1]).toMatchObject({ author: "agent", text: "Ответ, который не дошёл." });
    expect(items[at + 1].replyTo).toBe(mineByKey(chat, key).id);
    expect(shown(chat)).toEqual(["owner:Чужая до", "agent:Ответ на чужую", "owner:Своя", "agent:Ответ, который не дошёл."]);
  });

  test("ключ один на пузырь во всех путях: обрыв → «Повторить» → нет связи → «Проверить и повторить» → 409 → автоповтор", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Нач")], end: "error" });
    const key = await say(chat, "Раз");
    expect(mineByKey(chat, key).status?.kind).toBe("unanswered");
    server.failNetwork("POST", "/chat/messages");
    server.failNetwork("GET", "/chat/messages");
    await chat.retry(key);
    await idle(chat);
    expect(mineByKey(chat, key).status?.kind).toBe("unknown");
    server.leaseFor(mineByKey(chat, key).id!, 2);
    await chat.retry(key);
    await idle(chat);
    expect(mineByKey(chat, key).status).toEqual({ kind: "answered" });
    const keys = posts(server).map((c) => keyOf(c.body));
    expect(keys.length).toBeGreaterThanOrEqual(4);
    expect(new Set(keys).size).toBe(1);
    expect(owners(server, "Раз")).toHaveLength(1);
    expect(shown(chat)).toEqual(["owner:Раз", "agent:Слышу тебя."]);
  });
});

describe("A8 р.2 — 409 с ожиданием и «Остановить» (АР-51 (в); карточка, критерий 6)", () => {
  test("409 и история не читается — «неизвестно» с текстом о готовящемся ответе; «Проверить и повторить» — тот же ключ", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Нач")], end: "error" });
    const key = await say(chat, "Раз");
    server.leaseFor(mineByKey(chat, key).id!, 2);
    server.failNetwork("GET", "/chat/messages");
    await chat.retry(key);
    await idle(chat);
    const st = mineByKey(chat, key).status;
    expect(st?.kind).toBe("unknown");
    expect(st && "note" in st ? st.note : "").toMatch(/ещё готовится.*Не удалось проверить/);
    await chat.retry(key);
    await idle(chat);
    expect(mineByKey(chat, key).status).toEqual({ kind: "answered" });
    expect(new Set(posts(server).map((c) => keyOf(c.body))).size).toBe(1);
  });

  test("во время паузы ожидания новая реплика не уходит, поле ввода не теряется; конец сессии (close) прерывает паузу без автоповтора", async () => {
    const { chat, server } = await mk(undefined, { replyWaitMs: [60_000] });
    server.reply({ steps: [delta("Нач")], end: "error" });
    const key = await say(chat, "Раз");
    server.leaseFor(mineByKey(chat, key).id!, 10);
    void chat.retry(key);
    await vi.waitFor(() => expect(chat.getSnapshot().phase).toBe("waiting"));
    await vi.waitFor(() => expect(posts(server)).toHaveLength(3));
    chat.setInput("Вторая");
    expect(chat.send()).toBe(false);
    expect(chat.getSnapshot().input).toBe("Вторая");
    chat.close();
    await idle(chat);
    await new Promise((r) => setTimeout(r, 30));
    expect(posts(server)).toHaveLength(3);
    expect(mineByKey(chat, key).status).toMatchObject({ kind: "unanswered", retry: true });
    expect(chat.getSnapshot().items.filter((it) => it.author === "owner")).toHaveLength(1);
  });

  test("«Остановить» во время автоповтора (поток уже идёт) — ответ не сохранён, сверка; повтор — снова тот же ключ", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Нач")], end: "error" });
    const key = await say(chat, "Раз");
    server.leaseFor(mineByKey(chat, key).id!, 2);
    const g = gate();
    server.reply({ steps: [delta("Почти"), g.step, delta(" всё."), done()] });
    void chat.retry(key);
    await vi.waitFor(() => expect(chat.getSnapshot().phase).toBe("streaming"));
    chat.stop();
    g.open();
    await idle(chat);
    expect(mineByKey(chat, key).status?.kind).toBe("unanswered");
    expect(server.answerTo(mineByKey(chat, key).id!)).toBeUndefined();
    await chat.retry(key);
    await idle(chat);
    expect(shown(chat)).toEqual(["owner:Раз", "agent:Слышу тебя."]);
    expect(new Set(posts(server).map((c) => keyOf(c.body))).size).toBe(1);
  });

  test("автоповтор после 409 получил кризис — ответ протокола с контактами под своей репликой, итог «кризис»", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Нач")], end: "error" });
    const key = await say(chat, "Мне плохо");
    server.leaseFor(mineByKey(chat, key).id!, 2);
    server.reply({ steps: [crisis("Ответ протокола.", { replace: true })] });
    await chat.retry(key);
    await idle(chat);
    const s = chat.getSnapshot();
    expect(s.outcome?.kind).toBe("crisis");
    const reply = s.items.find((it) => it.text === "Ответ протокола.")!;
    expect(reply.crisis).toEqual(CONTACTS);
    expect(reply.replyTo).toBe(mineByKey(chat, key).id);
    expect(shown(chat)).toEqual(["owner:Мне плохо", "agent:Ответ протокола."]);
    expect(server.answerTo(mineByKey(chat, key).id!)?.crisis).toBe(true);
  });
});

describe("A8 р.2 — 422 под ключом", () => {
  test("после 422 новый ключ; повтор с ним — обрыв связи до сервера, затем «Отправить ещё раз» — снова новый (не третий) ключ", async () => {
    const taken = "11111111-2222-4333-8444-555555555555";
    const server = new FakeServer().onboarded();
    server.addChat("owner", "Чужой текст", undefined, { client_message_id: taken });
    const keys = [taken];
    const { chat } = await mk(server, { newKey: () => keys.shift() ?? newMessageKey() });
    const key = await say(chat, "Раз");
    expect(mineByKey(chat, key).status?.kind).toBe("unsent");
    server.failNetwork("POST", "/chat/messages");
    await chat.retry(key);
    await idle(chat);
    await chat.retry(key);
    await idle(chat);
    const sent = posts(server).map((c) => keyOf(c.body));
    expect(sent).toHaveLength(3);
    expect(sent[0]).toBe(taken);
    expect(sent[1]).not.toBe(taken);
    expect(sent[2]).toBe(sent[1]);
    expect(shown(chat)).toEqual(["owner:Чужой текст", "owner:Раз", "agent:Слышу тебя."]);
    expect(owners(server, "Раз")).toHaveLength(1);
  });

  // A8-2.2-D7: после 422 `failed` меняет ключ обмена (`turn.cmid`), а ключ пузыря (`item.cmid`) остаётся
  // прежним. Следующая сверка (`reconcile`, затем `mergeFirst`) ищет пузырь по **старому** ключу и находит под
  // ним ту самую чужую запись, из-за которой был 422: пузырь «Раз» получает её id, подпись «Сообщение
  // сохранено, ответа нет» (а сервер «Раз» не получал), на экране у двух пузырей один id. Ожидается: сверка
  // по новому ключу — «Не отправлено», id нет (ТЗ §10 «понятное состояние», карточка, критерий 4: сверка по ключу).
  test.fails("A8-2.2-D7: после 422 и обрыва связи сверка не привязывает пузырь к чужой записи под старым ключом", async () => {
    const taken = "11111111-2222-4333-8444-555555555555";
    const server = new FakeServer().onboarded();
    const foreign = server.addChat("owner", "Чужой текст", undefined, { client_message_id: taken });
    const keys = [taken];
    const { chat } = await mk(server, { newKey: () => keys.shift() ?? newMessageKey() });
    const key = await say(chat, "Раз");
    server.failNetwork("POST", "/chat/messages");
    await chat.retry(key);
    await idle(chat);
    const own = mineByKey(chat, key);
    expect(own.id).not.toBe(foreign.id);
    expect(own.status?.kind).toBe("unsent");
  });

  test("422 на повтор после «сохранено, ответа нет» — пузырь не дублируется, повтор с новым ключом — новая реплика сервера", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Нач")], end: "error" });
    const key = await say(chat, "Раз");
    const firstId = mineByKey(chat, key).id;
    server.inject("POST", "/chat/messages", 422, { code: "validation_error", message: "x" });
    await chat.retry(key);
    await idle(chat);
    expect(mineByKey(chat, key).status?.kind).toBe("unsent");
    await chat.retry(key);
    await idle(chat);
    expect(chat.getSnapshot().items.filter((it) => it.author === "owner" && it.text === "Раз")).toHaveLength(1);
    expect(mineByKey(chat, key).id).not.toBe(firstId);
    expect(owners(server, "Раз")).toHaveLength(2);
  });
});

describe("A8 р.2 — кризис из истории (ТЗ §9.3 «всегда»; карточка, критерий 5)", () => {
  test("кризисный ответ — в сверке после сбоя (а не в истории при открытии): контакты подгружаются, ответ под своей репликой", async () => {
    const server = new FakeServer().onboarded();
    const hook: { fn: Hook | null } = { fn: null };
    const { chat } = await mk(server, {}, hook);
    server.reply({ dropResponse: true });
    // Ответ протокола сохранил сервер до того, как клиент перечитал историю (обрыв после сохранения).
    hook.fn = (method) => {
      if (method !== "GET") return;
      hook.fn = null;
      const own = server.chat.find((m) => m.author === "owner" && m.text === "Мне плохо")!;
      server.addChat("agent", "Ответ протокола.", undefined, { reply_to: own.id, crisis: true });
    };
    const key = await say(chat, "Мне плохо");
    await vi.waitFor(() => expect(chat.getSnapshot().items.find((it) => it.text === "Ответ протокола.")?.crisis).toEqual(CONTACTS));
    expect(mineByKey(chat, key).status).toEqual({ kind: "answered" });
    expect(chat.getSnapshot().outcome?.kind).not.toBe("failed");
    expect(shown(chat)).toEqual(["owner:Мне плохо", "agent:Ответ протокола."]);
    expect(server.calls("GET", "/help/contacts")).toHaveLength(1);
  });

  test("кризис на более старой странице; контакты сначала не загрузились — при следующей сверке запрашиваются снова", async () => {
    const server = new FakeServer().onboarded();
    server.addChat("agent", "Старый ответ протокола.", undefined, { crisis: true });
    server.seedChat(30);
    server.inject("GET", "/help/contacts", 503, { code: "service_unavailable", message: "x" });
    const { chat } = await mk(server);
    expect(chat.getSnapshot().items.some((it) => it.text === "Старый ответ протокола.")).toBe(false);
    await chat.loadOlder();
    const old = () => chat.getSnapshot().items.find((it) => it.text === "Старый ответ протокола.");
    await vi.waitFor(() => expect(server.calls("GET", "/help/contacts")).toHaveLength(1));
    expect(old()?.crisis).toEqual([]);
    // Следующая сверка (реплика не дошла) — контакты запрашиваются ещё раз и встают под старым кризисом.
    server.failNetwork("POST", "/chat/messages");
    await say(chat, "Ещё");
    await vi.waitFor(() => expect(old()?.crisis).toEqual(CONTACTS));
    expect(server.calls("GET", "/help/contacts")).toHaveLength(2);
  });
});

describe("A8 р.2 — одинаковые реплики подряд", () => {
  test("«да» ×3: ответ, не дошло до сервера, обрыв с сохранённым ответом — три пузыря, ответ под третьим, три разных ключа", async () => {
    const { chat, server } = await mk();
    const k1 = await say(chat, "да");
    server.failNetwork("POST", "/chat/messages");
    const k2 = await say(chat, "да");
    server.reply({ dropResponse: "answered" });
    const k3 = await say(chat, "да");
    expect([mineByKey(chat, k1).status, mineByKey(chat, k2).status?.kind, mineByKey(chat, k3).status]).toEqual([
      { kind: "answered" },
      "unsent",
      { kind: "answered" },
    ]);
    expect(shown(chat)).toEqual(["owner:да", "agent:Слышу тебя.", "owner:да", "owner:да", "agent:Ответ, который не дошёл."]);
    const keys = posts(server).map((c) => keyOf(c.body));
    expect(new Set(keys).size).toBe(3);
    expect(owners(server, "да")).toHaveLength(2);
    // Второе «да» после третьего уже не повторяется (повтор — только последней реплики).
    await chat.retry(k2);
    expect(posts(server)).toHaveLength(3);
  });

  test("«да» сохранено без ответа, затем ещё «да» — обрыв без ответа: сверка не отдаёт второму пузырю первую запись", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Н")], end: "error" });
    const k1 = await say(chat, "да");
    server.reply({ dropResponse: true });
    const k2 = await say(chat, "да");
    const a = mineByKey(chat, k1);
    const b = mineByKey(chat, k2);
    expect(a.status?.kind).toBe("unanswered");
    expect(b.status?.kind).toBe("unanswered");
    expect(a.id).not.toBe(b.id);
    expect(b.id).toBe(server.ownerByKey(b.cmid!)!.id);
    expect(shown(chat).filter((x) => x === "owner:да")).toHaveLength(2);
  });
});

describe("A8 р.2 — реплика с другого устройства (MINOR-4)", () => {
  test("пока своя ждёт после 409, с телефона ушла такая же и получила ответ — свой ответ под своей, чужой под чужой", async () => {
    const { chat, server } = await mk();
    server.reply({ steps: [delta("Н")], end: "error" });
    const key = await say(chat, "Привет");
    const myId = mineByKey(chat, key).id!;
    server.leaseFor(myId, 2);
    const other = server.addChat("owner", "Привет", undefined, { client_message_id: newMessageKey() });
    server.addChat("agent", "Ответ телефону", undefined, { reply_to: other.id });
    server.reply({ steps: [delta("Ответ "), delta("мне."), done()] });
    await chat.retry(key);
    await idle(chat);
    expect(mineByKey(chat, key).status).toEqual({ kind: "answered" });
    const items = visibleItems(chat.getSnapshot().items);
    const mine = items.findIndex((it) => it.key === key);
    expect(items[mine + 1]).toMatchObject({ author: "agent", text: "Ответ мне." });
    const theirs = items.findIndex((it) => it.id === other.id);
    expect(theirs).toBeGreaterThanOrEqual(0);
    expect(items[theirs + 1]).toMatchObject({ author: "agent", text: "Ответ телефону" });
    expect(shown(chat).filter((x) => x === "owner:Привет")).toHaveLength(2);
  });

  test("со старого приложения на телефоне (без ключа) пришло то же «Привет» — свой пузырь не сворачивается с чужой записью", async () => {
    const server = new FakeServer().onboarded();
    server.addChat("owner", "Привет");
    const { chat } = await mk(server);
    server.failNetwork("POST", "/chat/messages");
    const key = await say(chat, "Привет");
    expect(mineByKey(chat, key).status?.kind).toBe("unsent");
    expect(shown(chat)).toEqual(["owner:Привет", "owner:Привет"]);
    await chat.retry(key);
    await idle(chat);
    expect(shown(chat)).toEqual(["owner:Привет", "owner:Привет", "agent:Слышу тебя."]);
  });
});

describe("A8 р.2 — реплика, которую сервер считает пустой", () => {
  // A8-2.2-D6: клиент проверяет «не из одних пробелов» через `String.prototype.trim`, сервер — через Python
  // `str.strip` (`_not_blank`, backend/app/schemas.py). Разделители U+001C…U+001F и U+0085 Python считает
  // пробелами, JS — нет: реплика из них уходит, сервер отвечает 422 `validation_error` (тест backend
  // `test_control_whitespace_text_is_422` в test_chat_idempotency_a8.py), а экран под ключом показывает «Сервер не
  // принял это сообщение. Отправь его ещё раз — оно уйдёт как новое.» — повтор снова 422. Ожидается: такая
  // реплика не уходит, как пустая (карточка, критерий 2; ТЗ §10 «понятное сообщение»).
  test.fails("A8-2.2-D6: реплика из U+001F/U+0085 не уходит (сервер её считает пустой)", async () => {
    const { chat, server } = await mk();
    for (const text of ["\u001f", "\u001c\u001d", " \u0085 "]) {
      chat.setInput(text);
      expect(chat.send(), JSON.stringify(text)).toBe(false);
    }
    expect(posts(server)).toHaveLength(0);
  });

  test("обычные пробелы, NBSP, переводы строк — не уходят, как на сервере; BOM — тоже (клиент строже сервера)", async () => {
    const { chat, server } = await mk();
    for (const text of ["   ", "\n\t", " ", "  ", "﻿"]) {
      chat.setInput(text);
      expect(chat.send(), JSON.stringify(text)).toBe(false);
    }
    expect(posts(server)).toHaveLength(0);
  });
});
