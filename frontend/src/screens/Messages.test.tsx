/**
 * A4, задача 3.4 — «Сообщения пары» (ФТ-09…ФТ-11; ТЗ §2 п.3–4, п.7, §4.4, §8, §9.3) и главный экран с числом
 * непрочитанных — на заглушке API по контракту. Тексты выдуманные.
 */
import { screen, waitFor, within } from "@testing-library/react";
import { CRISIS_BLOCKS } from "../couple/crisis";
import { FakeServer, type FeedRow } from "../test/fakeApi";
import { heading, renderApp, type Rendered } from "../test/renderApp";
import { unreadText } from "./Paired";
import { FEED_PAGE, OUTGOING_STATUS } from "./Messages";

function pairedServer(): FakeServer {
  const server = new FakeServer().onboarded().pendingCouple();
  server.partnerAccepts();
  return server;
}

async function openFeed(server = pairedServer()): Promise<Rendered> {
  const r = renderApp({ server });
  await heading("Вы пара");
  await r.user.click(screen.getByRole("button", { name: /Сообщения пары/ }));
  await heading("Сообщения пары");
  await waitFor(() => expect(screen.queryByText("Загружаем сообщения…")).toBeNull());
  return r;
}

const item = (id: string) => document.querySelector(`[data-id="${id}"]`) as HTMLElement;
const originals = (r: Rendered) => r.server.log.filter((l) => l.path.endsWith("/original"));
const reads = (r: Rendered) => r.server.log.filter((l) => l.path.endsWith("/read"));

describe("Отправленные — свой оригинал и статус подписью", () => {
  test("все пять статусов словами; текст — оригинал автора", async () => {
    const server = pairedServer();
    const rows: Record<string, FeedRow> = {};
    for (const st of ["processing", "delivered", "read", "answered"] as const) rows[st] = server.addFeed({ author: "me", original: `Моё ${st}`, status: st });
    rows.withheld = server.addFeed({ author: "me", original: "Моё withheld", status: "withheld", withheld_reason: "pipeline_error" });
    await openFeed(server);
    for (const [st, row] of Object.entries(rows)) {
      const li = item(row.id);
      expect(li).toHaveAttribute("data-direction", "outgoing");
      expect(within(li).getByText(`Моё ${st}`)).toBeInTheDocument();
      expect(li).toHaveTextContent(OUTGOING_STATUS[st as FeedRow["status"]]);
    }
    expect(OUTGOING_STATUS).toEqual({
      processing: "Обрабатывается",
      delivered: "Доставлено",
      read: "Прочитано",
      answered: "Отвечено",
      withheld: "Не доставлено",
    });
  });

  test("своё отправленное не отмечается прочитанным и не открывает /original", async () => {
    const server = pairedServer();
    server.addFeed({ author: "me", status: "delivered" });
    const r = await openFeed(server);
    expect(reads(r)).toHaveLength(0);
    expect(originals(r)).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Показать оригинал" })).toBeNull();
  });
});

describe("Не доставлено — причина подписью (АР-44, Б-86)", () => {
  // Консервативный набор по главному типу (ревью 3.4 MAJOR-1): клиент не знает остальных признаков.
  const CRISES = [
    ["threat_outgoing", ["not_sent", "outgoing_threat_brief", "author_safety"]],
    ["violence", ["not_sent", "author_safety"]],
    ["self_harm", ["not_sent", "self_harm", "author_safety"]],
    ["life_threat", ["not_sent", "author_safety_life"]],
    [null, ["not_sent", "author_safety"]],
  ] as const;
  /** Фразы блоков, которые `crisis.respond` запрещает хотя бы при одном наборе признаков с этим главным типом. */
  const FORBIDDEN = [
    "Вина за это не на тебе", // violence — только пострадавшему, нельзя при угрозе от автора
    "Твоя жизнь сейчас в опасности", // life_threat — то же
    "Давай вернёмся к разговору", // outgoing_threat(_mixed) — нельзя при self_harm
    "подумаем, как сказать", // outgoing_threat — нельзя при смешанном наборе
  ];
  for (const [kind, blocks] of CRISES) {
    test(`safety / ${kind ?? "null"}: «Не отправлено», протокол целиком, контакты и 112`, async () => {
      const server = pairedServer();
      const row = server.addFeed({ author: "me", original: "Остановленное", status: "withheld", withheld_reason: "safety", withheld_crisis: kind });
      const r = await openFeed(server);
      const li = item(row.id);
      expect(li).toHaveTextContent("Не отправлено");
      const help = within(li).getByRole("region", { name: "Контакты помощи" });
      for (const b of blocks) expect(within(help).getByText(CRISIS_BLOCKS[b])).toBeInTheDocument();
      expect(help.querySelectorAll(".crisis__text")).toHaveLength(blocks.length);
      for (const phrase of FORBIDDEN) expect(li).not.toHaveTextContent(phrase);
      expect(await within(help).findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
      await waitFor(() => expect(r.server.calls("GET", "/help/contacts")).toHaveLength(1));
      // Ошибка и «не доставлено» — нейтрально: без красной линии карточки партнёра.
      expect(li.querySelector(".partner-card")).toBeNull();
    });
  }

  test("model_refusal — подпись и помощь (112, «Все контакты помощи»)", async () => {
    const server = pairedServer();
    const row = server.addFeed({ author: "me", status: "withheld", withheld_reason: "model_refusal" });
    const r = await openFeed(server);
    const li = item(row.id);
    expect(li).toHaveTextContent("Не доставлено: сервис не смог обработать");
    expect(within(li).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    await r.user.click(within(li).getByRole("button", { name: "Все контакты помощи" }));
    await heading("Помощь");
  });

  for (const reason of ["pipeline_error", "attempts_exhausted"] as const) {
    test(`${reason} — «Не доставлено», текст сохранён, «Подготовить заново» ведёт в чат`, async () => {
      const server = pairedServer();
      const row = server.addFeed({ author: "me", original: "Мой текст", status: "withheld", withheld_reason: reason });
      const r = await openFeed(server);
      const li = item(row.id);
      expect(li).toHaveTextContent("Не доставлено");
      expect(within(li).getByText("Мой текст")).toBeInTheDocument();
      await r.user.click(within(li).getByRole("button", { name: "Подготовить заново" }));
      await heading("Твой агент");
      expect(r.server.calls("POST", "/drafts")).toHaveLength(0);
    });
  }

  test("couple_ended в ленте текущей пары не показывается", async () => {
    const server = pairedServer();
    const row = server.addFeed({ author: "me", status: "withheld", withheld_reason: "couple_ended" });
    await openFeed(server);
    expect(item(row.id)).toBeNull();
  });
});

describe("Полученные — карточка партнёра, «адаптировано», резкий тон, оригинал по запросу", () => {
  test("адаптированное: пометка подписью, линия карточки партнёра; неадаптированное — без пометки и без «Показать оригинал»", async () => {
    const server = pairedServer();
    const a = server.addFeed({ author: "partner", original: "Ты опять всё забыла!", is_adapted: true, status: "read" });
    const b = server.addFeed({ author: "partner", original: "Ок, суббота.", status: "read", harsh_tone_warning: true });
    await openFeed(server);
    const ca = item(a.id);
    expect(ca.querySelector(".partner-card")).not.toBeNull();
    expect(within(ca).getByText("адаптировано")).toBeInTheDocument();
    expect(within(ca).getByText("Адаптировано: Ты опять всё забыла!")).toBeInTheDocument();
    expect(within(ca).queryByText("Ты опять всё забыла!")).toBeNull();
    expect(within(ca).getByRole("button", { name: "Показать оригинал" })).toBeInTheDocument();
    const cb = item(b.id);
    expect(within(cb).queryByText("адаптировано")).toBeNull();
    expect(within(cb).getByText(/В оригинале резкий тон/)).toBeInTheDocument();
    expect(within(cb).queryByRole("button", { name: "Показать оригинал" })).toBeNull();
    expect(within(ca).queryByText(/резкий тон/)).toBeNull();
  });

  test("/original — только по нажатию; оригинал отдельно от адаптированного; «Скрыть»; повтор без запроса", async () => {
    const server = pairedServer();
    const a = server.addFeed({ author: "partner", original: "Оригинал как есть", is_adapted: true, status: "read" });
    const r = await openFeed(server);
    expect(originals(r)).toHaveLength(0);
    const card = item(a.id);
    await r.user.click(within(card).getByRole("button", { name: "Показать оригинал" }));
    const orig = await within(card).findByText("Оригинал как есть");
    expect(orig.closest("[data-original]")).not.toBeNull();
    expect(within(card).getByText("Адаптировано: Оригинал как есть").closest("[data-original]")).toBeNull();
    expect(originals(r)).toHaveLength(1);
    expect(server.feed.find((f) => f.id === a.id)!.originalViewedAt).not.toBeNull();
    await r.user.click(within(card).getByRole("button", { name: "Скрыть оригинал" }));
    expect(within(card).queryByText("Оригинал как есть")).toBeNull();
    await r.user.click(within(card).getByRole("button", { name: "Показать оригинал" }));
    expect(within(card).getByText("Оригинал как есть")).toBeInTheDocument();
    expect(originals(r)).toHaveLength(1);
    // Повторное открытие ленты в той же сессии — без нового запроса.
    await r.pressBack();
    await heading("Вы пара");
    await r.user.click(screen.getByRole("button", { name: /Сообщения пары/ }));
    await heading("Сообщения пары");
    await r.user.click(await within(await waitFor(() => item(a.id))).findByRole("button", { name: "Показать оригинал" }));
    expect(within(item(a.id)).getByText("Оригинал как есть")).toBeInTheDocument();
    expect(originals(r)).toHaveLength(1);
  });

  test("409 conflict_retry — один повтор; нерасшифрованный оригинал — подпись, просмотр не засчитан", async () => {
    const server = pairedServer();
    const a = server.addFeed({ author: "partner", original: "Текст", is_adapted: true, status: "read" });
    const u = server.addFeed({ author: "partner", original: { unavailable: true, reason: "auth" }, is_adapted: true, status: "read" });
    server.inject("GET", `/couple/messages/${a.id}/original`, 409, { code: "conflict_retry", message: "x" });
    const r = await openFeed(server);
    await r.user.click(within(item(a.id)).getByRole("button", { name: "Показать оригинал" }));
    expect(await within(item(a.id)).findByText("Текст")).toBeInTheDocument();
    expect(r.server.calls("GET", `/couple/messages/${a.id}/original`)).toHaveLength(2);
    await r.user.click(within(item(u.id)).getByRole("button", { name: "Показать оригинал" }));
    expect(await within(item(u.id)).findByText(/Текст оригинала недоступен/)).toBeInTheDocument();
    expect(server.feed.find((f) => f.id === u.id)!.originalViewedAt).toBeNull();
    // В ленте нерасшифрованный текст тоже не скрывает сообщение.
    expect(within(item(u.id)).getByText("Текст недоступен: его не удалось расшифровать.")).toBeInTheDocument();
  });

  test("ошибка /original — нейтральное сообщение, кнопка снова доступна", async () => {
    const server = pairedServer();
    const a = server.addFeed({ author: "partner", is_adapted: true, status: "read" });
    server.inject("GET", `/couple/messages/${a.id}/original`, 404, { code: "not_found", message: "x" });
    const r = await openFeed(server);
    await r.user.click(within(item(a.id)).getByRole("button", { name: "Показать оригинал" }));
    expect(await within(item(a.id)).findByRole("alert")).toHaveTextContent("Оригинал недоступен");
    expect(within(item(a.id)).getByRole("button", { name: "Показать оригинал" })).toBeEnabled();
  });

  test("текст сообщения — только текстом React (разметка не исполняется)", async () => {
    const server = pairedServer();
    server.addFeed({ author: "partner", original: '<img src=x onerror="window.__xss=2">привет', status: "read" });
    await openFeed(server);
    expect(screen.getByText('<img src=x onerror="window.__xss=2">привет')).toBeInTheDocument();
    expect(document.querySelector("[data-screen=messages] img")).toBeNull();
    expect((window as { __xss?: number }).__xss).toBeUndefined();
  });
});

describe("Прочитано — одна отметка на показанное полученное delivered", () => {
  test("delivered → один POST read, статус «Прочитано»; read/answered — без отметки; повторный показ — без второго", async () => {
    const server = pairedServer();
    const d = server.addFeed({ author: "partner", status: "delivered" });
    server.addFeed({ author: "partner", status: "read" });
    server.addFeed({ author: "partner", status: "answered" });
    const r = await openFeed(server);
    await waitFor(() => expect(reads(r)).toHaveLength(1));
    expect(reads(r)[0].path).toBe(`/couple/messages/${d.id}/read`);
    await waitFor(() => expect(item(d.id)).toHaveTextContent("Прочитано"));
    await r.pressBack();
    await heading("Вы пара");
    await r.user.click(screen.getByRole("button", { name: /Сообщения пары/ }));
    await heading("Сообщения пары");
    await waitFor(() => expect(item(d.id)).not.toBeNull());
    expect(reads(r)).toHaveLength(1);
  });

  test("ошибка отметки — молча (без alert); при следующем показе — повтор", async () => {
    const server = pairedServer();
    const d = server.addFeed({ author: "partner", status: "delivered" });
    server.inject("POST", `/couple/messages/${d.id}/read`, 500, { code: "internal", message: "x" });
    const r = await openFeed(server);
    await waitFor(() => expect(reads(r)).toHaveLength(1));
    expect(screen.queryByRole("alert")).toBeNull();
    await r.pressBack();
    await heading("Вы пара");
    await r.user.click(screen.getByRole("button", { name: /Сообщения пары/ }));
    await heading("Сообщения пары");
    await waitFor(() => expect(reads(r)).toHaveLength(2));
    expect(server.feed[0].status).toBe("read");
  });
});

describe("Страницы ленты", () => {
  test("«Показать более ранние» по next_cursor; 422 по курсору — с первой страницы без курсора", async () => {
    const server = pairedServer();
    for (let i = 0; i < FEED_PAGE + 5; i++) server.addFeed({ author: i % 2 ? "me" : "partner", original: `M${i}`, status: "read" });
    const r = await openFeed(server);
    expect(screen.queryByText("M0")).toBeNull();
    expect(screen.getByText(`M${FEED_PAGE + 4}`)).toBeInTheDocument();
    await r.user.click(screen.getByRole("button", { name: "Показать более ранние" }));
    expect(await screen.findByText("M0")).toBeInTheDocument();
    const feeds = () => r.server.calls("GET", "/couple/messages").filter((l) => /cursor=/.test(l.query));
    expect(feeds()).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Показать более ранние" })).toBeNull();

    // Смена ключа сервера: прежний курсор — 422.
    await r.pressBack();
    await heading("Вы пара");
    await r.user.click(screen.getByRole("button", { name: /Сообщения пары/ }));
    await screen.findByRole("button", { name: "Показать более ранние" });
    server.feedCursorKey = 2;
    const before = r.server.calls("GET", "/couple/messages").length;
    await r.user.click(screen.getByRole("button", { name: "Показать более ранние" }));
    expect(await screen.findByText(/Ленту пришлось загрузить заново/)).toBeInTheDocument();
    const after = r.server.calls("GET", "/couple/messages").slice(before);
    expect(after).toHaveLength(2);
    expect(after[1].query).not.toMatch(/cursor=/);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("пустая лента — объяснение; ошибка загрузки — нейтрально и «Загрузить снова»", async () => {
    const server = pairedServer();
    server.inject("GET", "/couple/messages", 200, { items: [{ id: "x" }] }, undefined, 2);
    const r = await openFeed(server);
    expect(await screen.findByRole("alert")).toHaveTextContent("Сообщения пришли с ошибкой");
    await r.user.click(screen.getByRole("button", { name: "Загрузить снова" }));
    expect(await screen.findByText("Пока сообщений нет")).toBeInTheDocument();
  });

  test("после распада пары сервер отдаёт пустую ленту — экран объясняет, сообщений прежней пары нет", async () => {
    const server = pairedServer().seedFeed();
    server.couple = { ...server.couple!, status: "dissolved" };
    await openFeed(server);
    expect(screen.getByText(/Пока сообщений нет|Пары сейчас нет/)).toBeInTheDocument();
    expect(document.querySelectorAll("[data-id]")).toHaveLength(0);
  });
});

describe("Принцип 4: личный чат партнёру не виден — в ленте только отправленное «Отправить»", () => {
  test("реплики личного чата в ленте не появляются; лента не читает /chat/messages", async () => {
    const server = pairedServer().seedFeed();
    server.addChat("owner", "Личная исповедь, которую никто не видит");
    const r = await openFeed(server);
    expect(screen.queryByText(/Личная исповедь/)).toBeNull();
    expect(r.server.calls("GET", "/chat/messages")).toHaveLength(0);
    expect(screen.getByText(/Разговоры с агентами сюда не попадают/)).toBeInTheDocument();
  });

  test("отправка черновика передаёт серверу только версию — ни текста чата, ни черновика в теле send", async () => {
    const server = pairedServer();
    server.addChat("owner", "Личная исповедь");
    const r = renderApp({ server });
    await heading("Вы пара");
    await r.pressMain();
    await heading("Твой агент");
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    await screen.findByLabelText("Текст сообщения партнёру");
    await r.pressMain();
    await heading("Сообщения пары");
    const send = r.server.log.find((l) => l.path.endsWith("/send"))!;
    expect(send.body).toEqual({ expected_version: 1 });
    expect(screen.queryByText(/Личная исповедь/)).toBeNull();
  });
});

describe("«Ответить с агентом» (АР-56, Б-30)", () => {
  test("чат с пометкой ответа; «Подготовить ответ» — reply_to_id этого сообщения; на втором — второго", async () => {
    const server = pairedServer();
    server.addChat("owner", "Разбираюсь");
    const m1 = server.addFeed({ author: "partner", original: "Первое", status: "read" });
    const m2 = server.addFeed({ author: "partner", original: "Второе", status: "read" });
    const r = await openFeed(server);

    await r.user.click(within(item(m1.id)).getByRole("button", { name: "Ответить с агентом" }));
    await heading("Твой агент");
    expect(document.querySelector(`[data-reply-to="${m1.id}"]`)).toHaveTextContent("Ответ на сообщение партнёра");
    await r.user.click(screen.getByRole("button", { name: "Подготовить ответ" }));
    await screen.findByLabelText("Текст сообщения партнёру");
    expect(r.server.calls("POST", "/drafts").at(-1)!.body).toEqual({ reply_to_id: m1.id });
    expect(r.server.calls("POST", "/drafts").length).toBe(1);

    // Назад в ленту, ответ на второе сообщение: id второго, а не первого.
    await r.pressBack();
    await heading("Твой агент");
    await r.pressBack();
    await heading("Сообщения пары");
    await r.user.click(within(await waitFor(() => item(m2.id))).getByRole("button", { name: "Ответить с агентом" }));
    await heading("Твой агент");
    expect(document.querySelector(`[data-reply-to="${m2.id}"]`)).not.toBeNull();
    expect(document.querySelector(`[data-reply-to="${m1.id}"]`)).toBeNull();
    await r.user.click(screen.getByRole("button", { name: "Подготовить ответ" }));
    await screen.findByLabelText("Текст сообщения партнёру");
    expect(r.server.calls("POST", "/drafts").at(-1)!.body).toEqual({ reply_to_id: m2.id });

    // Отправка ответа: send ушёл по нажатию; после доставки исходное — «Отвечено».
    await r.pressMain();
    await heading("Сообщения пары");
    const sent = r.server.feed.at(-1)!;
    expect(sent.reply_to_id).toBe(m2.id);
    r.server.deliver(sent.id);
    expect(r.server.feed.find((f) => f.id === m2.id)!.status).toBe("answered");
  });

  test("«Не отвечать на него» — пометка снимается, «Подготовить сообщение» без reply_to_id", async () => {
    const server = pairedServer();
    server.addChat("owner", "Разбираюсь");
    const m1 = server.addFeed({ author: "partner", status: "read" });
    const r = await openFeed(server);
    await r.user.click(within(item(m1.id)).getByRole("button", { name: "Ответить с агентом" }));
    await heading("Твой агент");
    await r.user.click(screen.getByRole("button", { name: "Не отвечать на него" }));
    await r.user.click(screen.getByRole("button", { name: "Подготовить сообщение" }));
    await screen.findByLabelText("Текст сообщения партнёру");
    expect(r.server.calls("POST", "/drafts")[0].body).toEqual({});
  });
});

describe("Главный экран — непрочитанные подписью, вход в ленту", () => {
  test("число непрочитанных полученных (delivered) словами", async () => {
    const server = pairedServer();
    server.addFeed({ author: "partner", status: "delivered" });
    server.addFeed({ author: "partner", status: "delivered" });
    server.addFeed({ author: "partner", status: "read" });
    server.addFeed({ author: "me", status: "delivered" });
    const r = renderApp({ server });
    await heading("Вы пара");
    const card = screen.getByRole("button", { name: /Сообщения пары/ });
    await waitFor(() => expect(card).toHaveTextContent("2 непрочитанных"));
    expect(card).toHaveAttribute("data-unread", "2");
    // Открытие главного экрана сообщения прочитанными не отмечает.
    expect(reads(r)).toHaveLength(0);
    await r.user.click(card);
    await heading("Сообщения пары");
  });

  test("нет новых — «Новых сообщений нет»", async () => {
    const server = pairedServer();
    renderApp({ server });
    await heading("Вы пара");
    await waitFor(() => expect(screen.getByRole("button", { name: /Сообщения пары/ })).toHaveTextContent("Новых сообщений нет"));
  });

  test("ошибка ленты на главном — «Не получилось проверить»", async () => {
    const server = pairedServer().inject("GET", "/couple/messages", 503, { code: "service_unavailable", message: "x" });
    renderApp({ server });
    await heading("Вы пара");
    await waitFor(() => expect(screen.getByRole("button", { name: /Сообщения пары/ })).toHaveTextContent("Не получилось проверить"));
  });

  test("склонение: 1, 2, 5, 11, 21, 22, 0", () => {
    expect(unreadText(1)).toBe("1 непрочитанное");
    expect(unreadText(2)).toBe("2 непрочитанных");
    expect(unreadText(5)).toBe("5 непрочитанных");
    expect(unreadText(11)).toBe("11 непрочитанных");
    expect(unreadText(21)).toBe("21 непрочитанное");
    expect(unreadText(22)).toBe("22 непрочитанных");
    expect(unreadText(0)).toBe("Новых сообщений нет");
  });
});
