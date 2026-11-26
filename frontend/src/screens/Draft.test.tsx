/**
 * A4, задача 3.4 — экран «Черновик» (ФТ-07, ФТ-08; ТЗ §2 п.2, §4.3, §8, §9.3, §10) на заглушке API по контракту.
 * Главный страж: `POST /drafts/{id}/send` уходит только после нажатия «Отправить». Тексты выдуманные.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { CONTACTS, FakeServer } from "../test/fakeApi";
import { fakeTelegram } from "../test/fakeTelegram";
import { heading, renderApp, type Rendered } from "../test/renderApp";
import { SEND_TEXT, privacyNote } from "./Draft";

const TURN = "Мне обидно, что ужин опять отменился";

function pairedServer(): FakeServer {
  const server = new FakeServer().onboarded().pendingCouple();
  server.partnerAccepts();
  server.addChat("owner", TURN);
  server.addChat("agent", "Слышу, как тебе обидно.");
  return server;
}

const sends = (r: Rendered) => r.server.log.filter((l) => l.method === "POST" && /^\/drafts\/[^/]+\/send$/.test(l.path));
const creates = (r: Rendered) => r.server.calls("POST", "/drafts");
const field = () => screen.getByLabelText("Текст сообщения партнёру") as HTMLTextAreaElement;
const live = () => document.querySelector("[data-screen=draft] [data-live]") as HTMLElement;

async function toChat(server = pairedServer(), tg = fakeTelegram()): Promise<Rendered> {
  const r = renderApp({ server, tg });
  await heading("Вы пара");
  await r.pressMain();
  await heading("Твой агент");
  return r;
}

async function openDraft(server = pairedServer(), tg = fakeTelegram()): Promise<Rendered> {
  const r = await toChat(server, tg);
  await r.user.click(await screen.findByRole("button", { name: "Подготовить сообщение" }));
  await heading("Черновик");
  await screen.findByLabelText("Текст сообщения партнёру");
  return r;
}

describe("Открытие: черновик агента, пометка о приватности, MainButton «Отправить»", () => {
  test("«Агент готовит черновик…», затем текст в поле правки; POST /drafts без reply_to_id; send не вызван", async () => {
    const server = pairedServer().draftReply({ kind: "draft", text: "Мне грустно, когда планы меняются в последний момент." });
    const r = await toChat(server);
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    expect(await screen.findByDisplayValue("Мне грустно, когда планы меняются в последний момент.")).toBeInTheDocument();
    expect(creates(r)).toHaveLength(1);
    expect(creates(r)[0].body).toEqual({});
    expect(screen.getByText(privacyNote("Игорь"))).toBeInTheDocument();
    expect(r.tg!.main).toMatchObject({ visible: true, text: SEND_TEXT, active: true });
    expect(r.tg!.back.visible).toBe(true);
    expect(sends(r)).toHaveLength(0);
    expect(live()).toHaveTextContent("Черновик готов");
  });

  test("пометка дословно по ТЗ §2 п.2", () => {
    expect(privacyNote("Игорь")).toBe("Черновик видишь только ты. Пока ты не нажмёшь «Отправить», Игорь ничего не получит.");
    expect(privacyNote(null)).toContain("партнёр ничего не получит");
  });

  test("текст черновика — только текстом (разметка не исполняется)", async () => {
    const xss = '<img src=x onerror="window.__xss=1"><b>жирный</b>';
    const r = await openDraft(pairedServer().draftReply({ kind: "draft", text: xss }));
    expect(field().value).toBe(xss);
    expect(document.querySelector("[data-screen=draft] img")).toBeNull();
    expect((window as { __xss?: number }).__xss).toBeUndefined();
    expect(sends(r)).toHaveLength(0);
  });
});

describe("ФТ-08, §2 п.2: send — только по нажатию «Отправить»", () => {
  test("генерация, правка, «Другой вариант», уход «Назад» и возврат — ни одного send", async () => {
    const r = await openDraft();
    await r.user.type(field(), " Правка.");
    await r.user.clear(screen.getByLabelText(/Пожелание/));
    await r.user.click(screen.getByRole("button", { name: "Другой вариант" }));
    await r.user.click(await screen.findByRole("button", { name: "Заменить" }));
    await waitFor(() => expect(field().value).toMatch(/^Другой вариант 2/));
    await r.pressBack();
    await heading("Твой агент");
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    await heading("Черновик");
    // Возврат — тот же черновик из памяти сессии, без нового POST /drafts.
    expect(field().value).toMatch(/^Другой вариант 2/);
    expect(creates(r)).toHaveLength(1);
    expect(sends(r)).toHaveLength(0);
    expect(r.server.log.filter((l) => l.method === "PATCH")).toHaveLength(0);
  });

  test("«Не отправлять» — reject, возврат в чат, send не вызван", async () => {
    const r = await openDraft();
    const id = [...r.server.draftRows.keys()][0];
    await r.user.click(screen.getByRole("button", { name: "Не отправлять" }));
    await heading("Твой агент");
    expect(r.server.calls("POST", `/drafts/${id}/reject`)).toHaveLength(1);
    expect(r.server.draftRows.get(id)!.status).toBe("rejected");
    expect(sends(r)).toHaveLength(0);
    // Следующее «Подготовить сообщение» — новый черновик, а не отклонённый.
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    await screen.findByLabelText("Текст сообщения партнёру");
    expect(creates(r)).toHaveLength(2);
    expect(sends(r)).toHaveLength(0);
  });

  test("«Помощь» и «Назад» с экрана черновика — правка на месте, send не вызван", async () => {
    const r = await openDraft();
    await r.user.type(field(), " Моя правка");
    await r.user.click(screen.getByRole("button", { name: "Помощь и контакты служб поддержки" }));
    await heading("Помощь");
    await r.pressBack();
    await heading("Черновик");
    expect(field().value).toMatch(/ Моя правка$/);
    expect(screen.getByText(/Изменено тобой/)).toBeInTheDocument();
    expect(sends(r)).toHaveLength(0);
  });

  test("перезапуск сессии (новый запуск приложения) — черновика в памяти нет, send не вызван", async () => {
    const server = pairedServer();
    const r1 = await openDraft(server);
    await r1.user.type(field(), " правка");
    cleanup();
    const r2 = renderApp({ server });
    await heading("Вы пара");
    expect(r2.tg!.main.text).toBe("Поговорить с агентом");
    expect(sends(r2)).toHaveLength(0);
    expect([...server.draftRows.values()].every((d) => d.status === "draft")).toBe(true);
  });

  test("нажатие MainButton — один send с expected_version показанной версии; 202 — лента, «Обрабатывается»", async () => {
    const r = await openDraft();
    const d = [...r.server.draftRows.values()][0];
    await r.pressMain();
    await heading("Сообщения пары");
    expect(sends(r)).toHaveLength(1);
    expect(sends(r)[0]).toMatchObject({ path: `/drafts/${d.id}/send`, body: { expected_version: 1 } });
    expect(r.server.log.filter((l) => l.method === "PATCH")).toHaveLength(0);
    const out = await screen.findByText(d.text);
    expect(out.closest("li")).toHaveTextContent("Обрабатывается");
    expect(r.tg!.haptics).toContain("notify:success");
    expect(r.tg!.main.visible).toBe(false);
  });

  test("двойное нажатие MainButton — один send", async () => {
    const r = await openDraft();
    const { act } = await import("@testing-library/react");
    await act(async () => {
      r.tg!.main.click();
      r.tg!.main.click();
    });
    await heading("Сообщения пары");
    expect(sends(r)).toHaveLength(1);
  });

  test("двойное нажатие запасной кнопки (вне нативной MainButton) — один send", async () => {
    const tg = fakeTelegram();
    // Нативная MainButton отказала (метод SDK бросает) — экран рисует запасную кнопку в странице.
    tg.webApp.MainButton.show = () => {
      throw new Error("SDK failure");
    };
    const r = renderApp({ server: pairedServer(), tg });
    await heading("Вы пара");
    await r.user.click(screen.getByRole("button", { name: "Поговорить с агентом" }));
    await heading("Твой агент");
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    await screen.findByLabelText("Текст сообщения партнёру");
    const btn = screen.getByRole("button", { name: SEND_TEXT });
    await r.user.dblClick(btn);
    await heading("Сообщения пары");
    expect(sends(r)).toHaveLength(1);
  });

  test("правка автора — PATCH с версией 1, затем send версии 2 (ровно показанный текст)", async () => {
    const r = await openDraft();
    const id = [...r.server.draftRows.keys()][0];
    await r.user.clear(field());
    await r.user.type(field(), "Мой текст целиком");
    expect(sends(r)).toHaveLength(0);
    await r.pressMain();
    await heading("Сообщения пары");
    const patch = r.server.log.filter((l) => l.method === "PATCH");
    expect(patch).toHaveLength(1);
    expect(patch[0].body).toEqual({ text: "Мой текст целиком", expected_version: 1 });
    expect(sends(r)[0].body).toEqual({ expected_version: 2 });
    expect(r.server.feed.at(-1)!.original).toBe("Мой текст целиком");
    expect(r.server.draftRows.get(id)!.status).toBe("sent");
  });

  test("пустой текст — «Отправить» недоступна, send не вызван", async () => {
    const r = await openDraft();
    await r.user.clear(field());
    await r.user.type(field(), "   ");
    expect(r.tg!.main.active).toBe(false);
    expect(screen.getByText("Текст пустой — отправлять нечего.")).toBeInTheDocument();
    await r.pressMain();
    expect(sends(r)).toHaveLength(0);
  });

  test("поле ограничено 4000 символами Unicode, как на сервере; счётчик с 3500 (ревью 3.4 MINOR-2)", async () => {
    const r = await openDraft(pairedServer().draftReply({ kind: "draft", text: "а".repeat(3600) }));
    expect(screen.getByText("3600 из 4000 символов")).toBeInTheDocument();
    // Вставка сверх предела обрезается до 4000 символов; эмодзи — один символ и не режется пополам.
    fireEvent.change(field(), { target: { value: "😀".repeat(3999) + "аб" } });
    expect([...field().value]).toHaveLength(4000);
    expect(field().value).toBe("😀".repeat(3999) + "а");
    expect(screen.getByText("4000 из 4000 символов")).toBeInTheDocument();
    expect(r.tg!.main.active).toBe(true);
    fireEvent.change(field(), { target: { value: "а".repeat(3999) + "😀😀" } });
    expect(field().value).toBe("а".repeat(3999) + "😀");
  });
});

describe("§10: правка и текст не теряются", () => {
  test("409 version_conflict при отправке — GET, актуальный текст, своя правка возвращается; ничего не отправлено", async () => {
    const r = await openDraft();
    const d = [...r.server.draftRows.values()][0];
    await r.user.clear(field());
    await r.user.type(field(), "Моя правка");
    // «Вторая вкладка» поменяла черновик: версия 2.
    r.server.draftRows.set(d.id, { ...d, text: "Текст из второй вкладки", version: 2 });
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent("Черновик изменился");
    expect(r.server.calls("GET", `/drafts/${d.id}`)).toHaveLength(1);
    expect(field().value).toBe("Текст из второй вкладки");
    expect(sends(r)).toHaveLength(0);
    expect(r.server.feed).toHaveLength(0);
    expect(r.tg!.haptics).toContain("notify:error");
    await r.user.click(screen.getByRole("button", { name: "Вернуть мою правку" }));
    expect(field().value).toBe("Моя правка");
  });

  test("409 version_conflict на send (без правки) — перечитан, ничего не ушло; следующее нажатие — версия с экрана", async () => {
    const r = await openDraft();
    const d = [...r.server.draftRows.values()][0];
    r.server.draftRows.set(d.id, { ...d, text: "Новая версия", version: 3 });
    await r.pressMain();
    await screen.findByRole("alert");
    expect(field().value).toBe("Новая версия");
    expect(r.server.feed).toHaveLength(0);
    await r.pressMain();
    await heading("Сообщения пары");
    const s = sends(r);
    expect(s.map((l) => l.body)).toEqual([{ expected_version: 1 }, { expected_version: 3 }]);
  });

  test("503 при генерации — понятный текст, «Повторить», разговор цел", async () => {
    const server = pairedServer().draftReply({
      kind: "status",
      status: 503,
      body: { code: "llm_unavailable", message: "Агент сейчас недоступен" },
    });
    const r = await toChat(server);
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Разговор сохранён");
    expect(r.tg!.main.visible).toBe(false);
    await r.user.click(screen.getByRole("button", { name: "Повторить" }));
    await screen.findByLabelText("Текст сообщения партнёру");
    expect(creates(r)).toHaveLength(2);
    await r.user.click(screen.getByRole("button", { name: "Не отправлять" }));
    await heading("Твой агент");
    expect(screen.getByText(TURN)).toBeInTheDocument();
    expect(sends(r)).toHaveLength(0);
  });

  test("503 отказ модели (llm_refusal) — строка о помощи со 112", async () => {
    const server = pairedServer().draftReply({ kind: "status", status: 503, body: { code: "llm_refusal", message: "x" } });
    const r = await toChat(server);
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    expect(sends(r)).toHaveLength(0);
  });

  test("503 при «Другом варианте» — прежний текст и правка на месте", async () => {
    const r = await openDraft();
    await r.user.type(field(), " моё");
    const before = field().value;
    r.server.draftReply({ kind: "status", status: 503, body: { code: "llm_unavailable", message: "x" } });
    await r.user.click(screen.getByRole("button", { name: "Другой вариант" }));
    await r.user.click(screen.getByRole("button", { name: "Заменить" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Прежний текст на месте");
    expect(field().value).toBe(before);
    expect(sends(r)).toHaveLength(0);
  });

  test("«Другой вариант» после правки — подтверждение; «Оставить мой текст» — запроса нет", async () => {
    const r = await openDraft();
    const id = [...r.server.draftRows.keys()][0];
    await r.user.type(field(), " моё");
    await r.user.click(screen.getByRole("button", { name: "Другой вариант" }));
    const group = screen.getByRole("group", { name: "Заменить твой текст?" });
    expect(group).toHaveTextContent("Твоя правка пропадёт");
    await r.user.click(within(group).getByRole("button", { name: "Оставить мой текст" }));
    expect(r.server.calls("POST", `/drafts/${id}/regenerate`)).toHaveLength(0);
    expect(field().value).toMatch(/ моё$/);
  });

  test("«Другой вариант» без правки — сразу; пожелание уходит в wish; 422 same_variant — свой текст", async () => {
    const r = await openDraft();
    const id = [...r.server.draftRows.keys()][0];
    await r.user.type(screen.getByLabelText(/Пожелание/), "мягче");
    await r.user.click(screen.getByRole("button", { name: "Другой вариант" }));
    await waitFor(() => expect(field().value).toMatch(/^Другой вариант 2/));
    expect(r.server.calls("POST", `/drafts/${id}/regenerate`)[0].body).toEqual({ wish: "мягче" });
    r.server.draftReply({ kind: "draft", text: field().value });
    await r.user.click(screen.getByRole("button", { name: "Другой вариант" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Другой вариант получился таким же");
    expect(sends(r)).toHaveLength(0);
  });
});

describe("§9.3: кризис — протокол целиком, контакты, черновика нет", () => {
  const PROTOCOL = "То, что ты описываешь, — это насилие и контроль, а не обычная ссора. Вина за это не на тебе.";

  test("POST /drafts → 200 DraftCrisis: текст протокола целиком, 112 ссылкой, нет поля и MainButton", async () => {
    const server = pairedServer().draftReply({ kind: "crisis", text: PROTOCOL });
    const r = await toChat(server);
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    const crisis = await screen.findByRole("region", { name: "Контакты помощи" });
    expect(within(crisis).getByText(PROTOCOL)).toBeInTheDocument();
    expect(within(crisis).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    expect(screen.queryByLabelText("Текст сообщения партнёру")).toBeNull();
    expect(r.tg!.main.visible).toBe(false);
    expect(sends(r)).toHaveLength(0);
    expect(r.server.draftRows.size).toBe(0);
  });

  test("кризис без контактов и без текста — запасной текст и 112 всё равно", async () => {
    const server = pairedServer().draftReply({ kind: "crisis", text: "", contacts: [] });
    const r = await toChat(server);
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    const crisis = await screen.findByRole("region", { name: "Контакты помощи" });
    expect(within(crisis).getByRole("link", { name: "112" })).toBeInTheDocument();
    expect(crisis).toHaveTextContent("Ты не один и не одна");
    expect(sends(r)).toHaveLength(0);
  });

  test("кризис на «Другом варианте» — протокол, отправить прежний черновик с экрана нельзя", async () => {
    const r = await openDraft();
    const id = [...r.server.draftRows.keys()][0];
    r.server.draftReply({ kind: "crisis", text: PROTOCOL, contacts: CONTACTS });
    await r.user.click(screen.getByRole("button", { name: "Другой вариант" }));
    expect(await screen.findByText(PROTOCOL)).toBeInTheDocument();
    expect(r.server.draftRows.get(id)!.version).toBe(1);
    expect(screen.queryByLabelText("Текст сообщения партнёру")).toBeNull();
    expect(r.tg!.main.visible).toBe(false);
    await r.pressMain();
    expect(sends(r)).toHaveLength(0);
  });
});

describe("Ошибки создания и отправки — свои тексты, ничего не отправлено", () => {
  const CASES = [
    [409, "couple_not_active", "Пара сейчас не активна"],
    [422, "no_turns", "Сначала расскажи агенту"],
    [422, "bad_reply_target", "больше недоступно"],
    [422, "draft_quote", "своими словами"],
    [422, "blocked_by_safety", "остановлен проверкой безопасности"],
  ] as const;
  for (const [status, code, text] of CASES) {
    test(`POST /drafts ${status} ${code}`, async () => {
      const server = pairedServer().draftReply({ kind: "status", status, body: { code, message: "x" } });
      const r = await toChat(server);
      await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(text);
      expect(r.tg!.main.visible).toBe(false);
      expect(sends(r)).toHaveLength(0);
      await r.user.click(screen.getByRole("button", { name: "Вернуться в чат" }));
      await heading("Твой агент");
    });
  }

  test("send 409 not_editable — черновик уже отправлен/отклонён: «Отправить» исчезает", async () => {
    const r = await openDraft();
    const d = [...r.server.draftRows.values()][0];
    r.server.draftRows.set(d.id, { ...d, status: "rejected" });
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent("уже отправлен или отклонён");
    expect(r.tg!.main.visible).toBe(false);
    expect(r.server.feed).toHaveLength(0);
  });

  test("send 409 couple_not_active — ничего не отправлено, текст на месте", async () => {
    const r = await openDraft();
    r.server.couple = { ...r.server.couple!, status: "dissolved" };
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent("сообщение не отправлено");
    expect(r.server.feed).toHaveLength(0);
    expect(field().value).not.toBe("");
  });

  test("обрыв связи на send — «могло не уйти, проверь ленту», ссылка на ленту", async () => {
    const r = await openDraft();
    const id = [...r.server.draftRows.keys()][0];
    r.server.failNetwork("POST", `/drafts/${id}/send`);
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent("Проверь ленту сообщений пары");
    expect(screen.getByRole("button", { name: "Проверить ленту сообщений пары" })).toBeInTheDocument();
  });

  test("402 (до этапа 4 не отдаётся) — как незнакомый код: ничего не ушло, экран цел", async () => {
    const r = await openDraft();
    const id = [...r.server.draftRows.keys()][0];
    r.server.inject("POST", `/drafts/${id}/send`, 402, { code: "subscription_required", message: "Нужна подписка" });
    await r.pressMain();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(r.server.feed).toHaveLength(0);
    expect(field()).toBeInTheDocument();
  });

  test("ответ send не 202/processing — bad_response, на ленту не переходим", async () => {
    const r = await openDraft();
    const id = [...r.server.draftRows.keys()][0];
    r.server.inject("POST", `/drafts/${id}/send`, 200, { id, direction: "outgoing" });
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent("сообщение могло уйти");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Черновик");
  });
});

describe("Приватность клиента: текст черновика не попадает в хранилища, cookie, адрес", () => {
  test("после правки и отправки localStorage/sessionStorage/cookie/адрес пусты от текста", async () => {
    const r = await openDraft(pairedServer().draftReply({ kind: "draft", text: "Секретный выдуманный текст" }));
    await r.user.type(field(), " и правка");
    const all = () => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }) + document.cookie + location.href;
    expect(all()).not.toContain("Секретный");
    await r.pressMain();
    await heading("Сообщения пары");
    expect(all()).not.toContain("Секретный");
  });
});
