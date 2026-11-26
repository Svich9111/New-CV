/**
 * Задача 3.4, раунд 2 — стражи подмены черновика (ревью 3.4-r1 BLOCKER-1, A8-3.4-2; MINOR-3).
 * Действия экрана «Черновик» («Отправить», «Другой вариант», «Не отправлять», PATCH) работают только с показанным
 * черновиком (его id и версией). Опоздавший ответ `POST /drafts` или «Другого варианта» с прежнего экрана (другой
 * `replyTo`, запрос уже не текущий) не подменяет показанный черновик и черновик в памяти сессии. Тексты выдуманные.
 */
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import { FakeServer } from "../test/fakeApi";
import { heading, renderApp, type Rendered } from "../test/renderApp";

const A = "Черновик А — ответ, которого автор на этом экране не видел";
const B = "Черновик Б — на экране";

function pairedServer(): FakeServer {
  const server = new FakeServer().onboarded().pendingCouple();
  server.partnerAccepts();
  server.addChat("owner", "Мне обидно, что ужин опять отменился");
  server.addChat("agent", "Слышу, как тебе обидно.");
  return server;
}

/** Задержать ответ на первый подходящий запрос (сервер его уже обработал), пока тест не отпустит. */
function holdOnce(server: FakeServer, method: string, re: RegExp) {
  const orig = server.fetch;
  let release!: () => void;
  const open = new Promise<void>((r) => (release = r));
  let on = true;
  server.fetch = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const res = await orig(input, init);
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://app.test");
    const path = url.pathname.replace(/^\/api\/v1/, "");
    if (on && (init.method ?? "GET").toUpperCase() === method && re.test(path)) {
      on = false;
      await open;
    }
    return res;
  };
  return { release: () => release() };
}

const field = () => screen.getByLabelText("Текст сообщения партнёру") as HTMLTextAreaElement;
const flush = () => act(async () => new Promise((r) => setTimeout(r, 20)));
const posts = (r: Rendered, suffix: string) => r.server.log.filter((l) => l.method === "POST" && l.path.endsWith(suffix)).map((l) => l.path);
const draftId = (server: FakeServer, prefix: string) => [...server.draftRows.values()].find((d) => d.text.startsWith(prefix))!.id;

afterEach(cleanup);

/** Лента → «Ответить с агентом» → чат. */
async function toReply(server: FakeServer): Promise<Rendered> {
  const m1 = server.addFeed({ author: "partner", original: "Входящее для ответа", status: "read" });
  const r = renderApp({ server });
  await heading("Вы пара");
  await r.user.click(screen.getByRole("button", { name: /Сообщения пары/ }));
  await heading("Сообщения пары");
  const li = await waitFor(() => document.querySelector(`[data-id="${m1.id}"]`) as HTMLElement);
  await r.user.click(within(li).getByRole("button", { name: "Ответить с агентом" }));
  await heading("Твой агент");
  return r;
}

/**
 * Сценарий ревьювера: «Подготовить ответ» (ответ А задержан) → «Назад» → «Не отвечать на него» → «Подготовить
 * сообщение» (на экране Б) → приходит опоздавший ответ А.
 */
async function lateCreate(): Promise<Rendered> {
  const server = pairedServer();
  const hold = holdOnce(server, "POST", /^\/drafts$/);
  server.draftReply({ kind: "draft", text: A });
  server.draftReply({ kind: "draft", text: B });
  const r = await toReply(server);
  await r.user.click(screen.getByRole("button", { name: "Подготовить ответ" }));
  await heading("Ответ партнёру");
  await r.pressBack();
  await heading("Твой агент");
  await r.user.click(screen.getByRole("button", { name: "Не отвечать на него" }));
  await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
  await waitFor(() => expect(field().value).toBe(B));
  hold.release();
  await flush();
  expect(field().value).toBe(B);
  return r;
}

describe("BLOCKER-1: опоздавший POST /drafts не подменяет показанный черновик", () => {
  test("«Отправить» — ровно показанный Б с его версией; А партнёру не уходит", async () => {
    const r = await lateCreate();
    const b = draftId(r.server, "Черновик Б");
    await r.pressMain();
    await heading("Сообщения пары");
    expect(posts(r, "/send")).toEqual([`/drafts/${b}/send`]);
    expect(r.server.log.find((l) => l.path.endsWith("/send"))!.body).toEqual({ expected_version: 1 });
    expect(r.server.feed.filter((f) => f.author === "me").map((f) => f.original)).toEqual([B]);
  });

  test("«Другой вариант» — над показанным Б", async () => {
    const r = await lateCreate();
    const b = draftId(r.server, "Черновик Б");
    await r.user.click(screen.getByRole("button", { name: "Другой вариант" }));
    await waitFor(() => expect(posts(r, "/regenerate")).toEqual([`/drafts/${b}/regenerate`]));
    expect(posts(r, "/send")).toHaveLength(0);
  });

  test("«Не отправлять» — отклоняет показанный Б, А не трогает", async () => {
    const r = await lateCreate();
    const b = draftId(r.server, "Черновик Б");
    await r.user.click(screen.getByRole("button", { name: "Не отправлять" }));
    await heading("Твой агент");
    expect(posts(r, "/reject")).toEqual([`/drafts/${b}/reject`]);
    expect(posts(r, "/send")).toHaveLength(0);
  });

  test("правка и «Отправить» — PATCH и send только у Б", async () => {
    const r = await lateCreate();
    const b = draftId(r.server, "Черновик Б");
    await r.user.clear(field());
    await r.user.type(field(), "Моя правка Б");
    await r.pressMain();
    await heading("Сообщения пары");
    expect(r.server.log.filter((l) => l.method === "PATCH").map((l) => l.path)).toEqual([`/drafts/${b}`]);
    expect(posts(r, "/send")).toEqual([`/drafts/${b}/send`]);
  });

  test("уход и возврат на экран: в памяти сессии — Б, а не опоздавший А", async () => {
    const r = await lateCreate();
    await r.pressBack();
    await heading("Твой агент");
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    await heading("Черновик");
    expect(field().value).toBe(B);
    expect(screen.getAllByText("Это черновик, который ты начал(а) раньше.").length).toBeGreaterThan(0);
    expect(r.server.calls("POST", "/drafts")).toHaveLength(2);
  });
});

describe("BLOCKER-1: опоздавший «Другой вариант» прежнего экрана не трогает чужой черновик", () => {
  test("вариант А пришёл, когда на экране уже Б — в памяти и на экране Б; отправляется Б", async () => {
    const server = pairedServer();
    server.draftReply({ kind: "draft", text: A });
    const hold = holdOnce(server, "POST", /\/regenerate$/); // до renderApp: Api берёт server.fetch при создании
    const r = await toReply(server);
    await r.user.click(screen.getByRole("button", { name: "Подготовить ответ" }));
    await waitFor(() => expect(field().value).toBe(A));
    const a = draftId(server, "Черновик А");
    server.draftReply({ kind: "draft", text: "Вариант А2" });
    await r.user.click(screen.getByRole("button", { name: "Другой вариант" }));
    await r.pressBack();
    await heading("Твой агент");
    await r.user.click(screen.getByRole("button", { name: "Не отвечать на него" }));
    server.draftReply({ kind: "draft", text: B });
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    await waitFor(() => expect(field().value).toBe(B));
    hold.release();
    await flush();
    expect(field().value).toBe(B);
    const created = r.server.calls("POST", "/drafts").length;
    await r.pressBack();
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    await heading("Черновик");
    // В памяти сессии остался Б: возврат показывает его, а не создаёт новый черновик.
    expect(field().value).toBe(B);
    expect(r.server.calls("POST", "/drafts")).toHaveLength(created);
    await r.pressMain();
    await heading("Сообщения пары");
    const b = draftId(server, "Черновик Б");
    expect(posts(r, "/send")).toEqual([`/drafts/${b}/send`]);
    expect(posts(r, "/send")).not.toContain(`/drafts/${a}/send`);
  });
});

describe("BLOCKER-1 / ревью 3.4-r2 MINOR-B: «Отправить» берёт черновик экрана, а не памяти сессии", () => {
  test("опоздавший «Другой вариант» прежнего экрана → вход заново с тем же replyTo → «Отправить»: уходит только показанное", async () => {
    const server = pairedServer();
    server.draftReply({ kind: "draft", text: A });
    const hold = holdOnce(server, "POST", /\/regenerate$/); // до renderApp: Api берёт server.fetch при создании
    const r = await toReply(server);
    await r.user.click(screen.getByRole("button", { name: "Подготовить ответ" }));
    await waitFor(() => expect(field().value).toBe(A));
    const a = draftId(server, "Черновик А");
    server.draftReply({ kind: "draft", text: "Вариант А2, которого на экране не было" });
    await r.user.click(screen.getByRole("button", { name: "Другой вариант" }));
    await r.pressBack();
    await heading("Твой агент");
    // Тот же replyTo: экран берёт черновик v1 из памяти сессии, нового POST /drafts нет.
    await r.user.click(screen.getByRole("button", { name: "Подготовить ответ" }));
    await waitFor(() => expect(field().value).toBe(A));
    expect(r.server.calls("POST", "/drafts")).toHaveLength(1);
    hold.release();
    await flush();
    // Вариант пришёл: на сервере (и в памяти сессии) v2, на экране по-прежнему v1.
    expect(server.draftRows.get(a)!.version).toBe(2);
    const shown = field().value;
    expect(shown).toBe(A);
    await r.pressMain();
    await flush();
    // Партнёру уходит только показанный текст; при расхождении версий — ничего (409 → перечитать).
    const sent = r.server.log.filter((l) => l.method === "POST" && l.path === `/drafts/${a}/send`);
    for (const l of sent) expect(l.body).toEqual({ expected_version: 1 });
    const delivered = r.server.feed.filter((f) => f.author === "me").map((f) => f.original);
    for (const t of delivered) expect(t).toBe(shown);
    expect(delivered).not.toContain("Вариант А2, которого на экране не было");
  });
});

describe("MINOR-3: после «Отправить» из «лента → чат → черновик» в стеке одна лента", () => {
  test("«Назад» из ленты ведёт на главный экран, а не в чат", async () => {
    const server = pairedServer();
    server.draftReply({ kind: "draft", text: "Ответ партнёру" });
    const r = await toReply(server);
    await r.user.click(screen.getByRole("button", { name: "Подготовить ответ" }));
    await waitFor(() => expect(field().value).toBe("Ответ партнёру"));
    await r.pressMain();
    await heading("Сообщения пары");
    await r.pressBack();
    await heading("Вы пара");
  });
});
