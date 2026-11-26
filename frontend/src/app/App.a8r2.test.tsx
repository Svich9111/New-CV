/// <reference types="node" />
/**
 * A8, задача 1.5, раунд 2 — исправления в интерфейсе: SDK по версиям и сбой SDK, сроки ответа, 2xx не по
 * схеме, тема на лету у MainButton, граница ошибок, опрос при неудачном входе, черновик онбординга.
 * Основание: карточка 1.5, ФТ-01…ФТ-03, ТЗ §8, §9.3; находки docs/reviews/1.5-test-r1.md и 1.5-review-r1.md.
 * Дефекты — `it.fails` с пометкой `A8-1.5-Dn` (зеленеют, пока дефект есть).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { act, screen } from "@testing-library/react";
import { TIMEOUTS } from "../api/client";
import { POLL_MS } from "../screens/Invite";
import { FakeServer, PARTNER_TOKEN } from "../test/fakeApi";
import { fakeTelegram } from "../test/fakeTelegram";
import { heading, renderApp, type Rendered } from "../test/renderApp";
import type { TelegramWebApp } from "../telegram/webapp";

const CONSENTS = [
  "Принимаю правила сервиса",
  "Даю согласие на обработку персональных данных",
  "Даю отдельное согласие на обработку сведений о чувствах и отношениях",
  "Мне исполнилось 18 лет",
];
const helpLink = () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" });

async function toOnboarding(r: Rendered) {
  await heading("Твой личный агент");
  await r.pressMain();
  await r.pressMain();
  await r.pressMain();
  await heading("Давай познакомимся");
}

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

// Токены темы (как в App.a8.test.tsx): без theme.css jsdom не знает --bg, --accent и т. д.
const THEME_CSS = readFileSync(join(__dirname, "../theme.css"), "utf8").replace(/@media[^{]+\{[\s\S]*?\}\s*\}/g, "");
let style: HTMLStyleElement;
beforeEach(() => {
  style = document.createElement("style");
  style.textContent = THEME_CSS;
  document.head.appendChild(style);
});
afterEach(() => {
  style.remove();
  vi.useRealTimers();
});

describe("A8 р.2 · SDK по версиям и сбой любого метода SDK — ready() и отрисовка всегда (MAJOR-1, ТЗ §8)", () => {
  for (const version of ["6.0", "6.1", "6.2", "6.8", "6.9", "7.0", "8.0"]) {
    for (const colorScheme of ["light", "dark"] as const) {
      test(`Bot API ${version}, ${colorScheme}: приветствие, ready(), data-theme, шапка допустимая для версии`, async () => {
        const tg = fakeTelegram({ version, colorScheme });
        renderApp({ tg });
        expect(await heading("Твой личный агент")).toBeInTheDocument();
        expect(tg.readyCalled()).toBe(true);
        expect(document.documentElement.dataset.theme).toBe(colorScheme);
        const [major, minor] = version.split(".").map(Number);
        const v = major * 10 + minor;
        if (v < 61) expect(tg.headerColors).toEqual([]);
        else if (v < 69) expect(tg.headerColors).toEqual(["bg_color"]);
        else expect(tg.headerColors).toEqual([colorScheme === "dark" ? "#000000" : "#ffffff"]);
        // смена темы на лету в той же версии тоже не бросает
        tg.webApp.colorScheme = colorScheme === "dark" ? "light" : "dark";
        act(() => tg.emit("themeChanged"));
        expect(document.documentElement.dataset.theme).toBe(tg.webApp.colorScheme);
        expect(screen.getByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
      });
    }
  }

  test("setHeaderColor, setBackgroundColor, onEvent, expand и ready бросают — экран рисуется, ready() вызван", async () => {
    const tg = fakeTelegram({ version: "6.2" });
    const boom = () => {
      throw new Error("SDK failure");
    };
    const wa = tg.webApp as TelegramWebApp & Record<string, unknown>;
    let readyCalls = 0;
    Object.assign(wa, {
      setHeaderColor: boom,
      setBackgroundColor: boom,
      onEvent: boom,
      expand: boom,
      ready: () => {
        readyCalls++;
        throw new Error("ready failure");
      },
    });
    renderApp({ tg });
    expect(await heading("Твой личный агент")).toBeInTheDocument();
    expect(readyCalls).toBe(1);
    expect(helpLink()).toBeInTheDocument();
  });

  test("themeChanged, когда setHeaderColor начал бросать, — тема меняется, экран на месте", async () => {
    const tg = fakeTelegram({ version: "6.9", colorScheme: "light" });
    renderApp({ tg });
    await heading("Твой личный агент");
    (tg.webApp as unknown as Record<string, unknown>).setHeaderColor = () => {
      throw new Error("WebAppHeaderColorInvalid");
    };
    tg.webApp.colorScheme = "dark";
    act(() => tg.emit("themeChanged"));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(screen.getByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
  });

  // Было (р.2): разовый отказ setParams ронял экран в «Что-то пошло не так». По D6 (поручение р.3) отказ метода
  // нативной кнопки экран не роняет — вместо неё кнопка в странице. Ожидание обновлено A4 под это поведение.
  test("MainButton.setParams бросает один раз — экран не падает, кнопка в странице работает, «Помощь» есть", async () => {
    const tg = fakeTelegram();
    const orig = tg.webApp.MainButton.setParams!;
    let n = 0;
    tg.webApp.MainButton.setParams = (p) => {
      if (++n === 1) throw new Error("WebAppMainButtonParamInvalid");
      orig(p);
    };
    const r = renderApp({ tg });
    expect(await heading("Твой личный агент")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Что-то пошло не так" })).toBeNull();
    expect(helpLink()).toBeInTheDocument();
    expect(tg.main.visible).toBe(false);
    await r.user.click(screen.getByRole("button", { name: "Далее" }));
    expect(await heading("Что приватно")).toBeInTheDocument();
  });

  // A8-1.5-D6 (р.2): вызовы MainButton (и BackButton) не защищены так же, как запуск SDK. Если метод нативной
  // кнопки бросает постоянно (SDK отклонил параметры), экран падает; запасной экран границы ошибок сам рисует
  // MainButton («Перезапустить»), падает снова, граница ловит это как новую ошибку — бесконечный цикл
  // отрисовки. В Chromium на сборке dist с настоящим telegram-web-app.js страница зависает (page.evaluate
  // не отвечает 5 с, ~10 000 console.error за 3 с; scratchpad/a8-1.5-r2/probe-d6.cjs). Ожидается (поручение
  // р.2 «сбой любого метода SDK — отрисовка всегда», ТЗ §9.3): конечное число попыток, страница и «Помощь».
  // Чтобы прогон не зависал, подделка перестаёт бросать после 100 вызовов; страж считает попытки.
  function stubbornSetParams(tg: ReturnType<typeof fakeTelegram>) {
    let n = 0;
    tg.webApp.MainButton.setParams = () => {
      n++;
      if (n <= 100) throw new Error("WebAppMainButtonParamInvalid");
    };
    return () => n;
  }

  it("A8-1.5-D6: MainButton.setParams всегда бросает — без цикла отрисовки, «Помощь» есть", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const tg = fakeTelegram();
    const calls = stubbornSetParams(tg);
    renderApp({ tg });
    expect(await screen.findByRole("button", { name: "Помощь и контакты служб поддержки" })).toBeInTheDocument();
    expect(calls()).toBeLessThanOrEqual(10);
  });

  // Факт по D6 обращён A4 (р.3) под исправление: отказ нативной кнопки — запасная кнопка в странице, и на
  // следующих экранах нативная кнопка заново не пробуется (вызовы SDK не растут).
  test("D6 исправлен: setParams всегда бросает — экран на месте, кнопка в странице ведёт дальше, без повторов", async () => {
    const tg = fakeTelegram();
    const calls = stubbornSetParams(tg);
    const r = renderApp({ tg });
    expect(await heading("Твой личный агент")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Что-то пошло не так" })).toBeNull();
    expect(tg.main.visible).toBe(false);
    const n = calls();
    expect(n).toBeGreaterThanOrEqual(1);
    await r.user.click(screen.getByRole("button", { name: "Далее" }));
    expect(await heading("Что приватно")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Далее" })).toBeEnabled();
    expect(calls()).toBe(n);
    expect(helpLink()).toBeInTheDocument();
  });
});

describe("A8 р.2 · D1: сроки ответа в интерфейсе (карточка «причина подписью, повтор»)", () => {
  test("GET /couple повис на экране приглашения — через TIMEOUTS.default причина и «Попробовать снова»", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const server = new FakeServer().onboarded();
    const base = server.fetch;
    let hang = true;
    server.fetch = (i, init) => (hang && String(i).endsWith("/couple") ? new Promise<Response>(() => undefined) : base(i, init));
    const r = renderApp({ server });
    await heading("Пригласи партнёра");
    await tick(TIMEOUTS.default - 100);
    expect(screen.queryByRole("alert")).toBeNull();
    await tick(200);
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервер не ответил вовремя");
    expect(r.tg!.main).toMatchObject({ visible: true, text: "Попробовать снова" });
    hang = false;
    await r.pressMain();
    expect(await screen.findByText(/Ссылки пока нет/)).toBeInTheDocument();
  });

  test("PUT /me/onboarding повис — причина подписью, кнопка снова доступна, повтор отправляет", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const server = new FakeServer();
    const base = server.fetch;
    let hang = true;
    server.fetch = (i, init) =>
      hang && String(i).endsWith("/me/onboarding") ? new Promise<Response>(() => undefined) : base(i, init);
    const r = renderApp({ server });
    await toOnboarding(r);
    await r.user.type(screen.getByLabelText("Как к тебе обращаться"), "Аня");
    await r.user.type(screen.getByLabelText("Имя партнёра"), "Игорь");
    for (const c of CONSENTS) await r.user.click(screen.getByRole("checkbox", { name: c }));
    await r.pressMain();
    expect(r.tg!.main.progress).toBe(true);
    await tick(TIMEOUTS.default + 10);
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервер не ответил вовремя");
    expect(r.tg!.main).toMatchObject({ progress: false, active: true });
    hang = false;
    await r.pressMain();
    expect(await heading("Пригласи партнёра")).toBeInTheDocument();
    // повисший запрос до заглушки не дошёл (её журнал его не видит); дошёл один — повтор
    expect(server.calls("PUT", "/me/onboarding")).toHaveLength(1);
  });

  test("«Помощь» до входа, вход повис: 112 сразу, а после срока входа «Назад» ведёт к причине", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const server = new FakeServer();
    const base = server.fetch;
    server.fetch = (i, init) => (String(i).endsWith("/auth/telegram") ? new Promise<Response>(() => undefined) : base(i, init));
    const r = renderApp({ server });
    await screen.findByText("Входим через Telegram…");
    await r.user.click(helpLink());
    await heading("Помощь");
    expect(screen.getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    await tick(TIMEOUTS.login + 10);
    expect(screen.getByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
    await r.pressBack();
    expect(await heading("Не получилось войти")).toBeInTheDocument();
  });
});

describe("A8 р.2 · D2: 2xx не по схеме в интерфейсе (ТЗ §9.3)", () => {
  // A8-1.5-D7 (р.2): «Помощь» проверяет у записи только name и phone; description другого типа (объект)
  // роняет отрисовку. Граница ошибок показывает «Что-то пошло не так», но её «Помощь» снова грузит тот же
  // список и снова падает — экран «Помощь» с номером-ссылкой 112 недостижим (есть только текст «звони 112»).
  it("A8-1.5-D7: /help/contacts с description-объектом — «Помощь» открывается, 112 ссылкой", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const server = new FakeServer();
    server.inject("GET", "/help/contacts", 200, [{ name: "A", phone: "112", description: { x: 1 }, round_the_clock: true }], undefined, 10);
    const r = renderApp({ server });
    await heading("Твой личный агент");
    await r.user.click(helpLink());
    expect(await heading("Помощь")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
  });

  // Факт по D7 обращён A4 (р.3) под исправление: запись не по схеме отброшена, «Помощь» не падает, 112 ссылкой.
  test("D7 исправлен: description-объект — запись отброшена, запасного экрана нет, ровно одна ссылка tel:112", async () => {
    const server = new FakeServer();
    server.inject("GET", "/help/contacts", 200, [{ name: "A", phone: "112", description: { x: 1 }, round_the_clock: true }], undefined, 10);
    const r = renderApp({ server });
    await heading("Твой личный агент");
    await r.user.click(helpLink());
    expect(await heading("Помощь")).toBeInTheDocument();
    await new Promise((res) => setTimeout(res, 50));
    expect(screen.queryByRole("heading", { name: "Что-то пошло не так" })).toBeNull();
    expect(screen.queryByText("A")).toBeNull();
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "tel:112");
  });

  // A8-1.5-D8 (р.2): у предпросмотра приглашения проверяется только expires_at; inviter_name не строкой
  // проходит как успех: заголовок «[object Object] приглашает тебя», а после «Принять» онбординг падает
  // (`inviterName.slice`) — человек видит «Что-то пошло не так» вместо онбординга.
  it("A8-1.5-D8: inviter_name не строка — «Приглашение не работает» или онбординг без падения", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const server = new FakeServer();
    server.inject("GET", `/invites/${PARTNER_TOKEN}`, 200, { inviter_name: { x: 1 }, expires_at: "2026-12-03T12:00:00Z" });
    const r = renderApp({ server, tg: fakeTelegram({ startParam: PARTNER_TOKEN }) });
    const h = await heading(/приглашает тебя|Тебя приглашают|Приглашение не работает/);
    expect(h.textContent).not.toContain("[object Object]");
    if (h.textContent === "Приглашение не работает") return;
    await r.pressMain();
    await toOnboarding(r);
  });

  test("/help/contacts: name/phone не строки, пустой phone — записи отброшены, остаётся 112", async () => {
    const server = new FakeServer();
    server.inject("GET", "/help/contacts", 200, [
      { name: 1, phone: "8-800", description: "x", round_the_clock: true },
      { name: "B", phone: "", description: "x", round_the_clock: true },
      { name: "C", phone: null, description: "x", round_the_clock: true },
      null,
      "строка",
    ]);
    const r = renderApp({ server });
    await heading("Твой личный агент");
    await r.user.click(helpLink());
    await heading("Помощь");
    expect(await screen.findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    expect(screen.getAllByRole("link")).toHaveLength(1);
  });

  test("вход 200 с HTML — «Не получилось войти», «Попробовать снова» входит", async () => {
    const server = new FakeServer();
    const base = server.fetch;
    let html = true;
    server.fetch = async (i, init) => {
      if (html && String(i).endsWith("/auth/telegram")) {
        html = false;
        return new Response("<html></html>", { status: 200, headers: { "Content-Type": "text/html" } });
      }
      return base(i, init);
    };
    const r = renderApp({ server });
    expect(await heading("Не получилось войти")).toBeInTheDocument();
    expect(r.tg!.main.text).toBe("Попробовать снова");
    await r.pressMain();
    expect(await heading("Твой личный агент")).toBeInTheDocument();
  });
});

describe("A8 р.2 · D3: тема на лету у MainButton (карточка «цвета MainButton из токенов; смена темы на лету»)", () => {
  test("доступная и недоступная кнопка после двух смен темы — цвета текущей темы", async () => {
    const tg = fakeTelegram({ colorScheme: "light" });
    const r = renderApp({ tg });
    await toOnboarding(r);
    expect(tg.main.params).toMatchObject({ color: "#d9d5d1", text_color: "#5f5a55" });
    tg.webApp.colorScheme = "dark";
    act(() => tg.emit("themeChanged"));
    expect(tg.main.params).toMatchObject({ color: "#2c2c2c", text_color: "#a8a29d" });
    tg.webApp.colorScheme = "light";
    act(() => tg.emit("themeChanged"));
    expect(tg.main.params).toMatchObject({ color: "#d9d5d1", text_color: "#5f5a55" });
    // доступная кнопка (приветствие) — акцент текущей темы
    await r.pressBack();
    await r.pressBack();
    await r.pressBack();
    await heading("Твой личный агент");
    const lightAccent = { ...tg.main.params };
    tg.webApp.colorScheme = "dark";
    act(() => tg.emit("themeChanged"));
    const darkAccent = { ...tg.main.params };
    const css = getComputedStyle(document.documentElement);
    expect(darkAccent.color).toBe(css.getPropertyValue("--accent").trim());
    expect(darkAccent.text_color).toBe(css.getPropertyValue("--on-accent").trim());
    expect(lightAccent.color).toMatch(/^#[0-9a-f]{6}$/i);
  });
});

describe("A8 р.2 · D4: граница ошибок (ТЗ §9.3)", () => {
  test("после «Перезапустить» в запасном экране — новый вход (POST /auth/telegram +1) и обычный экран", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    // Было (р.2): сбой вызывал D8 (inviter_name объектом ронял онбординг). D8 исправлен, поэтому A4 (р.3)
    // заменил источник сбоя: данные запуска Telegram, которые бросают при первом чтении после входа (экран
    // сессии падает при отрисовке). Проверки границы — те же.
    const server = new FakeServer();
    const tg = fakeTelegram({ startParam: PARTNER_TOKEN });
    const unsafe = tg.webApp.initDataUnsafe;
    let crash = true;
    Object.defineProperty(tg.webApp, "initDataUnsafe", {
      configurable: true,
      get: () => {
        if (crash) throw new Error("отрисовка упала");
        return unsafe;
      },
    });
    const r = renderApp({ server, tg });
    expect(await heading("Что-то пошло не так")).toBeInTheDocument();
    crash = false;
    expect(helpLink()).toBeInTheDocument();
    await r.user.click(helpLink());
    expect(await heading("Помощь")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "112" })).toBeInTheDocument();
    await r.pressBack();
    await heading("Что-то пошло не так");
    const logins = server.calls("POST", "/auth/telegram").length;
    await r.pressMain(); // «Перезапустить»
    // Перезапуск — тот же startapp: снова запрос приглашения (на этот раз сервер его не знает — 404).
    expect(await heading("Приглашение не работает")).toBeInTheDocument();
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(logins + 1);
  });
});

describe("A8 р.2 · m-3: опрос приглашения при неудачном повторном входе", () => {
  test("вход отклонён на тихом опросе — ни опроса, ни входов дальше; visibilitychange и activated тоже молчат", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const server = new FakeServer().onboarded().pendingCouple();
    const tg = fakeTelegram({ version: "8.0" });
    const r = renderApp({ server, tg });
    await screen.findByText(/Приглашение уже создано/);
    server.expireSession();
    server.inject("POST", "/auth/telegram", 401, { code: "invalid_init_data", message: "x" }, undefined, 1000);
    await tick(POLL_MS);
    expect(await screen.findByRole("alert")).toHaveTextContent("Не получилось подтвердить вход через Telegram");
    const logins = server.calls("POST", "/auth/telegram").length;
    const polls = server.calls("GET", "/couple").length;
    act(() => tg.emit("activated"));
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await tick(POLL_MS * 5);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(logins);
    expect(server.calls("GET", "/couple")).toHaveLength(polls);
    expect(r.tg!.main.text).toBe("Попробовать снова");
  });

  test("истёк токен, тик опроса + activated + visibilitychange разом — один повторный вход", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const server = new FakeServer().onboarded().pendingCouple();
    const tg = fakeTelegram({ version: "8.0" });
    renderApp({ server, tg });
    await screen.findByText(/Приглашение уже создано/);
    const before = server.calls("POST", "/auth/telegram").length;
    server.expireSession();
    await act(async () => {
      tg.emit("activated");
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(before + 1);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("A8 р.2 · m-4: черновик онбординга переживает «Помощь»", () => {
  test("поля, флажки и подсказка — после двух заходов в «Помощь» (ссылка снизу и «Назад»), затем отправка", async () => {
    const r = renderApp();
    await toOnboarding(r);
    const name = screen.getByLabelText("Твоё имя") as HTMLInputElement;
    await r.user.clear(name);
    await r.user.type(name, "Анюта");
    await r.user.type(screen.getByLabelText("Как к тебе обращаться"), "Аня");
    await r.user.type(screen.getByLabelText("Имя партнёра"), "Игорь");
    await r.user.click(screen.getByRole("checkbox", { name: CONSENTS[0] }));
    await r.user.click(screen.getByRole("checkbox", { name: CONSENTS[3] }));
    for (let i = 0; i < 2; i++) {
      await r.user.click(helpLink());
      await heading("Помощь");
      await r.pressBack();
      await heading("Давай познакомимся");
    }
    expect(screen.getByLabelText("Твоё имя")).toHaveValue("Анюта");
    expect(screen.getByLabelText("Как к тебе обращаться")).toHaveValue("Аня");
    expect(screen.getByLabelText("Имя партнёра")).toHaveValue("Игорь");
    expect(screen.getByRole("checkbox", { name: CONSENTS[0] })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: CONSENTS[1] })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: CONSENTS[3] })).toBeChecked();
    expect(r.tg!.main.active).toBe(false);
    await r.user.click(screen.getByRole("checkbox", { name: CONSENTS[1] }));
    await r.user.click(screen.getByRole("checkbox", { name: CONSENTS[2] }));
    await r.pressMain();
    await heading("Пригласи партнёра");
    expect(r.server.calls("PUT", "/me/onboarding")[0].body).toMatchObject({ name: "Анюта", address_form: "Аня", partner_name: "Игорь" });
  });
});
