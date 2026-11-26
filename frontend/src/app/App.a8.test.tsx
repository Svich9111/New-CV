/// <reference types="node" />
/**
 * A8, задача 1.5 — негативные сценарии интерфейса Mini App (ФТ-01…ФТ-03, ТЗ §2, §4.1, §8, §9.1 п.5,
 * §9.3). Дефекты — `it.fails` с пометкой `A8-1.5-Dn` (зеленеют, пока дефект есть).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { FakeServer, PARTNER_TOKEN } from "../test/fakeApi";
import { fakeTelegram, INIT_DATA } from "../test/fakeTelegram";
import { heading, renderApp, type Rendered } from "../test/renderApp";

const CONSENTS = [
  "Принимаю правила сервиса",
  "Даю согласие на обработку персональных данных",
  "Даю отдельное согласие на обработку сведений о чувствах и отношениях",
  "Мне исполнилось 18 лет",
];

async function toOnboarding(r: Rendered) {
  await heading("Твой личный агент");
  await r.pressMain();
  await r.pressMain();
  await r.pressMain();
  await heading("Давай познакомимся");
}

async function fillFields(r: Rendered) {
  await r.user.type(screen.getByLabelText("Как к тебе обращаться"), "Аня");
  await r.user.type(screen.getByLabelText("Имя партнёра"), "Игорь");
}

async function check(r: Rendered, skip?: string) {
  for (const c of CONSENTS) if (c !== skip) await r.user.click(screen.getByRole("checkbox", { name: c }));
}

function leaks(): string {
  return [
    JSON.stringify({ ...localStorage }),
    JSON.stringify({ ...sessionStorage }),
    document.cookie,
    window.location.href,
    document.documentElement.outerHTML,
  ].join("\n");
}

/** Задержать ответы на путь до release(); ставится до renderApp (Api запоминает fetch при создании). */
function gate(server: FakeServer, suffix: string) {
  const base = server.fetch;
  let hold = false;
  let release: () => void = () => undefined;
  server.fetch = async (i, init) => {
    if (hold && String(i).endsWith(suffix)) await new Promise<void>((res) => (release = res));
    return base(i, init);
  };
  return { hold: () => (hold = true), release: () => release() };
}

const helpLink = () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" });

describe("A8 · приватность initData и токена в интерфейсе (ТЗ §9.1 п.5)", () => {
  test("полный путь инициатора: initData и токен не попадают в DOM, адрес, хранилища, cookie", async () => {
    const r = renderApp();
    await toOnboarding(r);
    await fillFields(r);
    await check(r);
    await r.pressMain();
    await heading("Пригласи партнёра");
    await r.pressMain(); // «Отправить в Telegram»
    await screen.findByText("Ждём партнёра.");
    const all = leaks();
    expect(r.server.token).toBeTruthy();
    expect(all).not.toContain(r.server.token!);
    expect(all).not.toContain("hash=fake");
    expect(all).not.toContain(encodeURIComponent(INIT_DATA));
    // Ссылка для партнёра уходит в t.me/share/url, но без initData и токена сессии.
    expect(r.tg!.opened.join()).not.toContain("hash=fake");
    expect(r.tg!.opened.join()).not.toContain(r.server.token!);
  });

  test("незнакомый код при входе — показывается message сервера как текст", async () => {
    const server = new FakeServer().inject("POST", "/auth/telegram", 401, { code: "something_new", message: "bad " + INIT_DATA });
    renderApp({ server });
    await heading("Не получилось войти");
    // Незнакомый код — показывается message сервера; если сервер вернёт в нём initData — он попадёт в DOM.
    // Сервер по контракту так не делает; фиксируем поведение клиента (замечание, не дефект).
    expect(screen.getByRole("alert")).toHaveTextContent("bad");
  });
});

describe("A8 · 401 при повторном входе — без цикла (карточка «Вход по контракту»)", () => {
  test("GET /couple всегда 401: экран показывает причину, запросов ограниченное число", async () => {
    const server = new FakeServer().onboarded().inject("GET", "/couple", 401, { code: "unauthorized", message: "x" }, undefined, 1000);
    const r = renderApp({ server });
    await heading("Пригласи партнёра");
    expect(await screen.findByRole("alert")).toHaveTextContent("Сессия закончилась");
    await act(async () => new Promise((res) => setTimeout(res, 50)));
    expect(server.calls("GET", "/couple")).toHaveLength(2);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
    await r.pressMain(); // «Попробовать снова»
    await waitFor(() => expect(server.calls("GET", "/couple")).toHaveLength(4));
    await act(async () => new Promise((res) => setTimeout(res, 50)));
    expect(server.calls("GET", "/couple")).toHaveLength(4);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(3);
  });

  test("повторный вход отвечает 401 invalid_init_data — текст про вход, без цикла", async () => {
    const server = new FakeServer().onboarded();
    const r = renderApp({ server });
    await heading("Пригласи партнёра");
    server.inject("POST", "/couple", 401, { code: "unauthorized", message: "x" });
    server.inject("POST", "/auth/telegram", 401, { code: "invalid_init_data", message: "x" }, undefined, 1000);
    await r.pressMain(); // «Создать ссылку-приглашение»
    expect(await screen.findByRole("alert")).toHaveTextContent("Не получилось подтвердить вход");
    await act(async () => new Promise((res) => setTimeout(res, 50)));
    expect(server.calls("POST", "/couple")).toHaveLength(1);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
  });
});

describe("A8 · 429, 5xx, обрыв при входе", () => {
  test("429 с Retry-After при входе — причина и срок подписью, «Попробовать снова»", async () => {
    const server = new FakeServer().inject("POST", "/auth/telegram", 429, { code: "rate_limited", message: "x" }, { "Retry-After": "20" });
    const r = renderApp({ server });
    await heading("Не получилось войти");
    expect(screen.getByRole("alert")).toHaveTextContent("через 20 с.");
    expect(r.tg!.main).toMatchObject({ visible: true, text: "Попробовать снова" });
    await r.pressMain();
    expect(await heading("Твой личный агент")).toBeInTheDocument();
  });

  test("429 без Retry-After и без тела при входе — понятный текст без числа", async () => {
    const server = new FakeServer().inject("POST", "/auth/telegram", 429);
    renderApp({ server });
    await heading("Не получилось войти");
    expect(screen.getByRole("alert")).toHaveTextContent("Слишком много запросов подряд");
    expect(screen.getByRole("alert")).not.toHaveTextContent(/через|NaN|undefined/);
  });

  test("502 с HTML от прокси при входе — общий текст, HTML не вставлен в страницу", async () => {
    const server = new FakeServer();
    server.fetch = async () => new Response("<h1 id=evil>Bad gateway</h1><script>window.__pwned=1</script>", { status: 502, headers: { "Content-Type": "text/html" } });
    const r = renderApp({ server });
    await heading("Не получилось войти");
    expect(screen.getByRole("alert")).toHaveTextContent("Что-то пошло не так");
    expect(document.getElementById("evil")).toBeNull();
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
    expect(r.tg!.main.text).toBe("Попробовать снова");
  });

  test("403 account_deleted при входе — без «Попробовать снова», «Помощь» доступна", async () => {
    const server = new FakeServer().inject("POST", "/auth/telegram", 403, { code: "account_deleted", message: "x" });
    const r = renderApp({ server });
    await heading("Не получилось войти");
    expect(screen.getByRole("alert")).toHaveTextContent("аккаунт удалён");
    expect(r.tg!.main.visible).toBe(false);
    expect(helpLink()).toBeInTheDocument();
  });

  // A8-1.5-D1 (в интерфейсе): вход повис — «Входим через Telegram…» без конца, без причины и повтора.
  // Р.2 (A8): в р.1 тело стража включало поддельные таймеры уже после начала входа — таймер срока,
  // взведённый настоящим setTimeout при запросе, поддельные часы не двигали, и страж падал при
  // исправленном коде (ошибка стража, не продукта; подтверждено прогоном). Теперь часы подделаны до
  // запуска приложения; смысл тот же: повисший вход → причина подписью и «Попробовать снова» в срок
  // (≤ 60 с), повтор действительно входит.
  it("A8-1.5-D1: повисший вход через 60 с показывает причину и «Попробовать снова»", async () => {
    // shouldAdvanceTime: часы идут и сами (иначе внутренний setTimeout(0) testing-library не сработает),
    // но 60 с срока проходят только через advanceTimersByTimeAsync.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"], shouldAdvanceTime: true });
    try {
      const server = new FakeServer();
      const base = server.fetch;
      let hang = true;
      server.fetch = (i, init) => (hang ? new Promise<Response>(() => undefined) : base(i, init));
      const r = renderApp({ server });
      expect(await screen.findByText("Входим через Telegram…")).toBeInTheDocument();
      // До срока — всё ещё вход, без ложной ошибки.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      expect(screen.getByText("Входим через Telegram…")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(59_000);
      });
      expect(await heading("Не получилось войти")).toBeInTheDocument();
      expect(screen.getByRole("alert")).not.toHaveTextContent(/^\s*$/);
      expect(r.tg!.main).toMatchObject({ visible: true, text: "Попробовать снова" });
      expect(helpLink()).toBeInTheDocument();
      hang = false;
      await r.pressMain();
      expect(await heading("Твой личный агент")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("A8 · HTML и скрипты в полях ответа сервера не исполняются (XSS)", () => {
  const EVIL = `<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>"'&`;

  test("имя пригласившего с HTML — текстом в заголовке, элементов не создаётся", async () => {
    const server = new FakeServer().partnerInvite("active", EVIL);
    renderApp({ server, tg: fakeTelegram({ startParam: PARTNER_TOKEN }) });
    const h = await heading(/приглашает тебя/);
    expect(h.textContent).toBe(`${EVIL} приглашает тебя`);
    expect(document.querySelector("img, script")).toBeNull();
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
  });

  test("контакты помощи с HTML и javascript: в телефоне — текст, href только tel: из цифр", async () => {
    const server = new FakeServer();
    server.inject("GET", "/help/contacts", 200, [
      { name: EVIL, phone: "javascript:alert(1)//112", description: EVIL, round_the_clock: false },
    ]);
    const r = renderApp({ server });
    await heading("Твой личный агент");
    await r.user.click(helpLink());
    await heading("Помощь");
    const items = await screen.findAllByRole("listitem");
    const contact = items.find((li) => li.classList.contains("contact"))!;
    expect(contact.textContent).toContain(EVIL);
    expect(document.querySelector(".contacts img, .contacts script")).toBeNull();
    const a = within(contact).getByRole("link");
    expect(a.getAttribute("href")).toBe("tel:1112");
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
  });

  test("незнакомый код ошибки с HTML в message — текстом в плашке ошибки", async () => {
    const server = new FakeServer().onboarded().inject("GET", "/couple", 500, { code: "brand_new", message: EVIL });
    renderApp({ server });
    await heading("Пригласи партнёра");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(EVIL);
    expect(alert.querySelector("img, script")).toBeNull();
  });

  test("ссылка-приглашение с javascript: — только текст, наружу — через t.me/share/url", async () => {
    const server = new FakeServer().onboarded();
    const r = renderApp({ server });
    await heading("Пригласи партнёра");
    server.inject("POST", "/couple", 201, { couple_id: "c1", link: "javascript:window.__pwned=3", expires_at: "2026-12-03T12:00:00Z" });
    await r.pressMain();
    expect(await screen.findByLabelText("Ссылка-приглашение")).toHaveTextContent("javascript:window.__pwned=3");
    expect(document.querySelector('a[href^="javascript"]')).toBeNull();
    await r.pressMain();
    expect(r.tg!.opened[0]).toMatch(/^https:\/\/t\.me\/share\/url\?url=javascript%3A/);
  });
});

describe("A8 · startapp с мусором (карточка: «токен неверного формата — без запроса»)", () => {
  const BAD = [
    ["кавычки и теги", `"'><script>alert(1)</script>`],
    ["очень длинный (10 000 символов)", "A".repeat(10_000)],
    ["65 символов (длиннее контракта)", "A".repeat(65)],
    ["31 символ (короче)", "A".repeat(31)],
    ["путь", "../../couple/invite/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"],
    ["кириллица", "П".repeat(43)],
    ["пробелы", " ".repeat(43)],
    ["base64 с + и /", "A+/=".repeat(11)],
  ] as const;

  for (const [label, token] of BAD) {
    test(`${label} — «Приглашение не работает», запроса /invites нет`, async () => {
      const r = renderApp({ tg: fakeTelegram({ startParam: token }) });
      expect(await heading("Приглашение не работает")).toBeInTheDocument();
      expect(r.server.log.filter((l) => l.path.startsWith("/invites"))).toHaveLength(0);
      expect(screen.getByRole("button", { name: "Продолжить без приглашения" })).toBeInTheDocument();
      expect(document.querySelector("script")).toBeNull();
      expect(r.tg!.main.visible).toBe(false);
    });
  }

  test("пустой startapp — обычный вход, без экрана приглашения", async () => {
    renderApp({ tg: fakeTelegram({ startParam: "" }) });
    expect(await heading("Твой личный агент")).toBeInTheDocument();
  });

  test("мусорный startapp → «Продолжить без приглашения» → приветствие", async () => {
    const r = renderApp({ tg: fakeTelegram({ startParam: "x'\"" }) });
    await heading("Приглашение не работает");
    await r.user.click(screen.getByRole("button", { name: "Продолжить без приглашения" }));
    expect(await heading("Твой личный агент")).toBeInTheDocument();
  });
});

describe("A8 · согласия и 18+ нельзя обойти (ФТ-02, ТЗ §4.1 п.2, §9.3)", () => {
  test("Enter в поле при незаполненной форме — запроса нет", async () => {
    const r = renderApp();
    await toOnboarding(r);
    await r.user.type(screen.getByLabelText("Твоё имя"), "{Enter}");
    expect(r.server.calls("PUT", "/me/onboarding")).toHaveLength(0);
  });

  for (const skip of CONSENTS) {
    test(`Enter в поле без «${skip}» — запроса нет`, async () => {
      const r = renderApp();
      await toOnboarding(r);
      await fillFields(r);
      await check(r, skip);
      await r.user.type(screen.getByLabelText("Имя партнёра"), "{Enter}");
      fireEvent.submit(screen.getByLabelText("Имя партнёра").closest("form")!);
      expect(r.server.calls("PUT", "/me/onboarding")).toHaveLength(0);
    });
  }

  test("отметил и снял 18+ — снова недоступно; нажатие нативной кнопки — без запроса", async () => {
    const r = renderApp();
    await toOnboarding(r);
    await fillFields(r);
    await check(r);
    expect(r.tg!.main.active).toBe(true);
    await r.user.click(screen.getByRole("checkbox", { name: "Мне исполнилось 18 лет" }));
    expect(r.tg!.main.active).toBe(false);
    await r.pressMain();
    await r.pressMain();
    expect(r.server.calls("PUT", "/me/onboarding")).toHaveLength(0);
    // m-7 (A4, р.1): подсказка про 18+ нейтральная — правило, а не просьба поставить отметку.
    expect(screen.getByRole("status")).toHaveTextContent("Сервис только для взрослых 18+.");
    expect(screen.getByRole("status")).not.toHaveTextContent(/отметь/);
  });

  test("двойной Enter подряд при заполненной форме — один запрос", async () => {
    const server = new FakeServer();
    const g = gate(server, "/me/onboarding");
    const r = renderApp({ server });
    await toOnboarding(r);
    await fillFields(r);
    await check(r);
    g.hold();
    const form = screen.getByLabelText("Имя партнёра").closest("form")!;
    fireEvent.submit(form);
    fireEvent.submit(form);
    await r.pressMain();
    await r.pressMain();
    await act(async () => g.release());
    await heading("Пригласи партнёра");
    expect(r.server.calls("PUT", "/me/onboarding")).toHaveLength(1);
  });

  test("нажатие во время запроса (кнопка в прогрессе, >500 мс) — второго запроса нет", async () => {
    const server = new FakeServer();
    const g = gate(server, "/me/onboarding");
    const r = renderApp({ server });
    await toOnboarding(r);
    await fillFields(r);
    await check(r);
    g.hold();
    await r.pressMain();
    await waitFor(() => expect(r.tg!.main.progress).toBe(true));
    await r.pressMain(); // pressMain сдвигает часы на 1000 мс — защита 500 мс уже не действует
    await r.pressMain();
    await act(async () => g.release());
    await heading("Пригласи партнёра");
    expect(r.server.calls("PUT", "/me/onboarding")).toHaveLength(1);
  });

  test("повторная отправка после 422 — форма сохраняется, второй запрос по контракту", async () => {
    const server = new FakeServer().inject("PUT", "/me/onboarding", 422, { code: "validation_error", message: "Проверьте данные" });
    const r = renderApp({ server });
    await toOnboarding(r);
    await fillFields(r);
    await check(r);
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent("Проверь, что все поля заполнены");
    for (const c of CONSENTS) expect(screen.getByRole("checkbox", { name: c })).toBeChecked();
    await r.pressMain();
    await heading("Пригласи партнёра");
    const puts = server.calls("PUT", "/me/onboarding");
    expect(puts).toHaveLength(2);
    expect(puts[1].body).toMatchObject({ consent_terms: true, consent_data: true, consent_sensitive: true, age_confirmed: true });
  });

  test("после 403 underage назад в онбординг не вернуться (нет «Назад»), «Помощь» есть", async () => {
    const server = new FakeServer().inject("PUT", "/me/onboarding", 403, { code: "underage", message: "x" });
    const r = renderApp({ server });
    await toOnboarding(r);
    await fillFields(r);
    await check(r);
    await r.pressMain();
    expect(await heading("Сервис доступен только с 18 лет")).toBeInTheDocument();
    expect(r.tg!.back.visible).toBe(false);
    expect(r.tg!.main.visible).toBe(false);
    expect(helpLink()).toBeInTheDocument();
    await r.pressBack();
    expect(await heading("Сервис доступен только с 18 лет")).toBeInTheDocument();
  });

  test("Bot API 6.0 / вне Telegram: запасная кнопка disabled, клик по ней — без запроса", async () => {
    const r = renderApp({ tg: fakeTelegram({ version: "6.0" }) });
    await toOnboarding(r);
    await fillFields(r);
    await check(r, "Мне исполнилось 18 лет");
    // В 6.0 MainButton нативная — проверяем нативную; 18+ не отмечен.
    await r.pressMain();
    expect(r.server.calls("PUT", "/me/onboarding")).toHaveLength(0);
  });
});

describe("A8 · «Помощь» на каждом экране и при недоступном /help/contacts (ТЗ §9.3)", () => {
  async function openHelp(r: Rendered) {
    await r.user.click(helpLink());
    expect(await heading("Помощь")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
  }

  test("загрузка входа (вход ещё идёт) — ссылка есть", async () => {
    const server = new FakeServer();
    const base = server.fetch;
    server.fetch = (i, init) => (String(i).endsWith("/auth/telegram") ? new Promise<Response>(() => undefined) : base(i, init));
    const r = renderApp({ server });
    await screen.findByText("Входим через Telegram…");
    await openHelp(r);
  });

  test("вне Telegram", async () => {
    const r = renderApp({ tg: null });
    await heading("Открой в Telegram");
    await openHelp(r);
  });

  test("приветствие (3 слайда), онбординг, приглашение, «Вы пара»", async () => {
    const server = new FakeServer();
    const r = renderApp({ server });
    for (const h of ["Твой личный агент", "Что приватно", "Чем мы не являемся"]) {
      await heading(h);
      expect(helpLink()).toBeInTheDocument();
      await r.pressMain();
    }
    await heading("Давай познакомимся");
    expect(helpLink()).toBeInTheDocument();
    await fillFields(r);
    await check(r);
    await r.pressMain();
    await heading("Пригласи партнёра");
    await openHelp(r);
    await r.pressBack();
    await heading("Пригласи партнёра");
    server.partnerAccepts();
    await r.user.click(screen.getByRole("button", { name: "Проверить, принято ли приглашение" }));
    await heading("Вы пара");
    await openHelp(r);
  });

  test("входящее приглашение и «Приглашение не работает»", async () => {
    const server = new FakeServer().partnerInvite("expired");
    const r = renderApp({ server, tg: fakeTelegram({ startParam: PARTNER_TOKEN }) });
    await heading("Приглашение не работает");
    await openHelp(r);
  });

  for (const [label, make] of [
    ["500", (s: FakeServer) => s.inject("GET", "/help/contacts", 500)],
    ["404", (s: FakeServer) => s.inject("GET", "/help/contacts", 404, { code: "not_found", message: "x" })],
    ["пустой список", (s: FakeServer) => s.inject("GET", "/help/contacts", 200, [])],
    [
      "обрыв связи",
      (s: FakeServer) => {
        const base = s.fetch;
        s.fetch = async (i, init) => {
          if (String(i).endsWith("/help/contacts")) throw new TypeError("Failed to fetch");
          return base(i, init);
        };
      },
    ],
  ] as const) {
    test(`/help/contacts: ${label} — запасной 112`, async () => {
      const server = new FakeServer();
      make(server);
      const r = renderApp({ server });
      await heading("Твой личный агент");
      await openHelp(r);
    });
  }

  // Факт обращён после исправления D1 (A4, р.1): раньше здесь фиксировалось, что 112 во время загрузки нет.
  test("/help/contacts повис — пока загрузка, запасной 112 уже на экране", async () => {
    const server = new FakeServer();
    const base = server.fetch;
    server.fetch = (i, init) => (String(i).endsWith("/help/contacts") ? new Promise<Response>(() => undefined) : base(i, init));
    const r = renderApp({ server });
    await heading("Твой личный агент");
    await r.user.click(helpLink());
    await heading("Помощь");
    expect(screen.getByText("Загружаем контакты…")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
  });

  // A8-1.5-D2 (в интерфейсе) фиксируется на уровне клиента (client.a8.test.ts): 200 text/html на
  // /help/contacts → null → Help.tsx бросает в then, 112 не показывается. Здесь не воспроизводится,
  // потому что необработанное отклонение валит прогон vitest; проверено вручную в Chromium (отчёт).
});

describe("A8 · вне Telegram и старый клиент (< 6.1)", () => {
  test("вне Telegram: ни одного запроса, кроме /help/contacts по запросу человека", async () => {
    const r = renderApp({ tg: null });
    await heading("Открой в Telegram");
    expect(r.server.log).toHaveLength(0);
    await r.user.click(helpLink());
    await screen.findByRole("link", { name: "112" });
    expect(r.server.log.map((l) => l.path)).toEqual(["/help/contacts"]);
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  test("SDK есть, initData пустой, но start_param есть — всё равно «Открой в Telegram», без запросов", async () => {
    const r = renderApp({ tg: fakeTelegram({ initData: "", startParam: PARTNER_TOKEN }) });
    await heading("Открой в Telegram");
    expect(r.server.log).toHaveLength(0);
  });

  test("Bot API 6.0: без тактильного отклика, без цвета шапки, «Назад» в странице на каждом шаге", async () => {
    const tg = fakeTelegram({ version: "6.0", colorScheme: "dark" });
    const r = renderApp({ tg });
    await heading("Твой личный агент");
    expect(tg.headerColors).toEqual([]);
    await r.pressMain();
    await heading("Что приватно");
    expect(tg.haptics).toEqual([]);
    expect(tg.back.visible).toBe(false);
    expect(screen.getByRole("button", { name: /Назад/ })).toBeInTheDocument();
    await r.user.click(helpLink());
    await heading("Помощь");
    await r.user.click(screen.getByRole("button", { name: /Назад/ }));
    expect(await heading("Что приватно")).toBeInTheDocument();
  });

  test("Bot API 6.0 на экране приглашения: без события activated, опрос по таймеру остаётся", async () => {
    const tg = fakeTelegram({ version: "6.0" });
    const server = new FakeServer().onboarded().pendingCouple();
    renderApp({ server, tg });
    await heading("Пригласи партнёра");
    await screen.findByText("Ждём партнёра.");
    const before = server.calls("GET", "/couple").length;
    tg.emit("activated");
    await act(async () => new Promise((res) => setTimeout(res, 20)));
    expect(server.calls("GET", "/couple")).toHaveLength(before);
  });
});

describe("A8 · переключение темы на лету (ТЗ §8 «следует теме Telegram»)", () => {
  const css = readFileSync(join(__dirname, "../theme.css"), "utf8");
  let style: HTMLStyleElement;
  beforeEach(() => {
    style = document.createElement("style");
    style.textContent = css.replace(/@media[^{]+\{[\s\S]*?\}\s*\}/g, "");
    document.head.appendChild(style);
  });
  afterEach(() => style.remove());

  test("themeChanged: data-theme и цвет шапки Telegram меняются без перезапуска", async () => {
    const tg = fakeTelegram({ colorScheme: "light" });
    renderApp({ tg });
    await heading("Твой личный агент");
    expect(tg.headerColors.at(-1)).toBe("#ffffff");
    tg.webApp.colorScheme = "dark";
    act(() => tg.emit("themeChanged"));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(tg.headerColors.at(-1)).toBe("#000000");
  });

  test("недоступная MainButton серая из токенов светлой темы", async () => {
    const r = renderApp({ tg: fakeTelegram({ colorScheme: "light" }) });
    await toOnboarding(r);
    expect(r.tg!.main.params).toMatchObject({ color: "#d9d5d1", text_color: "#5f5a55" });
  });

  // A8-1.5-D3: цвета MainButton задаются только при смене text/disabled/progress; на `themeChanged`
  // они не пересчитываются. Недоступная кнопка остаётся светло-серой (#d9d5d1) в тёмной теме, где
  // токен --disabled-bg = #2c2c2c (карточка: «цвета MainButton берутся из токенов; смена темы на лету»).
  it("A8-1.5-D3: после themeChanged недоступная MainButton берёт --disabled-bg тёмной темы", async () => {
    const tg = fakeTelegram({ colorScheme: "light" });
    const r = renderApp({ tg });
    await toOnboarding(r);
    tg.webApp.colorScheme = "dark";
    act(() => tg.emit("themeChanged"));
    await waitFor(() => expect(r.tg!.main.params).toMatchObject({ color: "#2c2c2c", text_color: "#a8a29d" }));
  });
});

describe("A8 · доступность (роли и имена; axe — в ручной проверке Chromium)", () => {
  test("на экранах пути все кнопки, поля и ссылки имеют доступные имена; один h1", async () => {
    const server = new FakeServer();
    const r = renderApp({ server, tg: fakeTelegram({ version: "6.0" }) });
    const checkScreen = () => {
      expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
      for (const el of [
        ...screen.queryAllByRole("button"),
        ...screen.queryAllByRole("textbox"),
        ...screen.queryAllByRole("checkbox"),
        ...screen.queryAllByRole("link"),
      ])
        expect(el).toHaveAccessibleName();
    };
    await heading("Твой личный агент");
    checkScreen();
    await r.pressMain();
    await heading("Что приватно");
    checkScreen();
    await r.pressMain();
    await r.pressMain();
    await heading("Давай познакомимся");
    checkScreen();
    await fillFields(r);
    await check(r);
    await r.pressMain();
    await heading("Пригласи партнёра");
    await screen.findByText("Ссылка готова.");
    checkScreen();
    await r.user.click(helpLink());
    await screen.findByRole("link", { name: "112" });
    checkScreen();
  });

  test("ошибка — role=alert с «Ошибка:», статус ожидания — role=status", async () => {
    const server = new FakeServer().onboarded().pendingCouple().inject("POST", "/couple/invite", 503, { code: "service_unavailable", message: "x" });
    const r = renderApp({ server });
    await heading("Пригласи партнёра");
    expect(await screen.findByRole("status")).toHaveTextContent("Ждём партнёра.");
    await r.pressMain(); // «Получить новую ссылку»
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/^!?Ошибка: Сервис временно недоступен/);
  });

  // Факт обращён после исправления m-10 (A4, р.1): раньше здесь фиксировался aria-label на <p> без роли.
  test("ссылка-приглашение: видимая подпись и группа с именем (не aria-label на <p>)", async () => {
    const r = renderApp({ server: new FakeServer().onboarded() });
    await heading("Пригласи партнёра");
    await r.pressMain();
    const group = await screen.findByRole("group", { name: "Ссылка-приглашение" });
    expect(within(group).getByText("Ссылка-приглашение")).toBeVisible();
    expect(group.querySelector("[aria-label]")).toBeNull();
    // р.2 (A8): сама ссылка — внутри названной группы, иначе имя группы ни к чему не относится.
    expect(within(group).getByText(/^https:\/\/t\.me\/test_bot\?startapp=/)).toBeVisible();
  });

  test("фокус на заголовке нового экрана после ошибки входа и на «Помощи»", async () => {
    const server = new FakeServer().inject("POST", "/auth/telegram", 503, { code: "service_unavailable", message: "x" });
    const r = renderApp({ server });
    expect(await heading("Не получилось войти")).toHaveFocus();
    await r.user.click(helpLink());
    expect(await heading("Помощь")).toHaveFocus();
  });
});
