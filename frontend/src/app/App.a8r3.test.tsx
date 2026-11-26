/**
 * A8, задача 1.5, раунд 3 — независимая проверка исправлений A4 (коммит 526775a) по docs/reviews/1.5-test-r2.md
 * (D6) и 1.5-review-r2.md (n-1): отказ нативной кнопки посреди сессии, повторное падение запасного экрана
 * без подмены модулей, «онбординг сохранён, принять не удалось» при нескольких сбоях и кнопками в странице.
 * ТЗ §8 (нативные кнопки, старые клиенты), §9.3 (помощь доступна всегда), ФТ-02/ФТ-03, §4.1 п.4.
 */
import { act, screen } from "@testing-library/react";
import { FakeServer, PARTNER_TOKEN } from "../test/fakeApi";
import { fakeTelegram } from "../test/fakeTelegram";
import { heading, renderApp, type Rendered } from "../test/renderApp";

const CONSENTS = [
  "Принимаю правила сервиса",
  "Даю согласие на обработку персональных данных",
  "Даю отдельное согласие на обработку сведений о чувствах и отношениях",
  "Мне исполнилось 18 лет",
];
const helpLink = () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" });

describe("A8 р.3 · D6: нативная MainButton отказала посреди сессии (ТЗ §8, §9.3)", () => {
  test("setText бросает с 3-го экрана — дальше кнопка в странице, нативная скрыта, её старый обработчик молчит", async () => {
    const tg = fakeTelegram();
    const orig = tg.webApp.MainButton.setText.bind(tg.webApp.MainButton);
    let screens = 0;
    tg.webApp.MainButton.setText = (t: string) => {
      if (t === "Начать") screens++;
      if (screens > 0) throw new Error("WebAppMainButtonParamInvalid");
      orig(t);
    };
    const r = renderApp({ tg });
    await heading("Твой личный агент");
    await r.pressMain();
    await heading("Что приватно");
    expect(tg.main.visible).toBe(true);
    await r.pressMain(); // → «Чем мы не являемся»: здесь setText("Начать") бросает
    expect(await heading("Чем мы не являемся")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Что-то пошло не так" })).toBeNull();
    expect(tg.main.visible).toBe(false);
    // Нажатие нативной кнопки (если Telegram её всё же показал) не уводит со слайда: обработчик снят.
    await r.pressMain();
    expect(screen.getByRole("heading", { level: 1, name: "Чем мы не являемся" })).toBeInTheDocument();
    await r.user.click(screen.getByRole("button", { name: "Начать" }));
    expect(await heading("Давай познакомимся")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Продолжить" })).toBeDisabled();
    expect(helpLink()).toBeInTheDocument();
  });
});

describe("A8 р.3 · D6: отказ show() после onClick() — обработчик снят (выжившая мутация M10)", () => {
  test("MainButton.show бросает — нажатие нативной кнопки не действует (обработчик не остался), кнопка в странице ведёт", async () => {
    const tg = fakeTelegram();
    tg.webApp.MainButton.show = () => {
      throw new Error("SDK failure");
    };
    const r = renderApp({ tg });
    await heading("Твой личный агент");
    expect(tg.main.visible).toBe(false);
    // Telegram скрытую кнопку не нажимает, но если событие всё же придёт — оно не должно вести дальше:
    // иначе одно касание сработало бы дважды (нативный обработчик + кнопка в странице).
    await r.pressMain();
    expect(screen.getByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
    await r.user.click(screen.getByRole("button", { name: "Далее" }));
    expect(await heading("Что приватно")).toBeInTheDocument();
  });
});

describe("A8 р.3 · D6: сбой getComputedStyle — кнопка в странице, экран не падает (ТЗ §8, §9.3)", () => {
  // Обращён A4 (р.4) по замечанию A8 р.3 №3: прежде MainButton читал токены цвета вне withMain, сбой
  // getComputedStyle ронял экран и запасной экран и уводил в LastResort. Теперь токены читаются под защитой:
  // нативная кнопка скрыта, дальше — кнопка в странице, экраны не падают. Каскад «упал экран и запасной экран →
  // статичный минимум без цикла» по-прежнему проверяется в App.boundary.test.tsx (D6, упал и запасной экран).
  // Запросы по роли — с hidden: true, нажатия — click() элемента: иначе Testing Library и user-event сами зовут
  // getComputedStyle у <html> и падают на подменённом сбое.
  test("getComputedStyle бросает — «Твой личный агент» с кнопкой в странице, нативная скрыта, сбой не повторяется", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const real = globalThis.getComputedStyle.bind(globalThis);
    const broken = { on: false, calls: 0 };
    vi.spyOn(globalThis, "getComputedStyle").mockImplementation((el: Element, p?: string | null) => {
      if (broken.on && el === document.documentElement) {
        broken.calls++;
        throw new Error("getComputedStyle failure");
      }
      return real(el, p);
    });
    const server = new FakeServer();
    // Сбой включён до запуска: тема применяется под защитой (start), а «Входим…» токенов цвета не читает.
    broken.on = true;
    const tg = fakeTelegram();
    renderApp({ server, tg });
    expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент", hidden: true })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Что-то пошло не так", hidden: true })).toBeNull();
    expect(screen.queryByText(/Приложение не может показать экран/)).toBeNull();
    expect(tg.main.visible).toBe(false);
    // После первого отказа нативная кнопка больше не пробуется — и токены цвета не читаются снова.
    const calls = broken.calls;
    expect(calls).toBeGreaterThan(0);
    expect(calls).toBeLessThanOrEqual(10);
    await act(async () => new Promise((res) => setTimeout(res, 100)));
    expect(broken.calls).toBe(calls);

    await act(async () => screen.getByRole("button", { name: "Далее", hidden: true }).click());
    expect(await screen.findByRole("heading", { level: 1, name: "Что приватно", hidden: true })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Что-то пошло не так", hidden: true })).toBeNull();
    expect(tg.main.visible).toBe(false);
    expect(broken.calls).toBe(calls);
    expect(screen.getByRole("button", { name: "Помощь и контакты служб поддержки", hidden: true })).toBeInTheDocument();
    broken.on = false;
  });
});

describe("A8 р.3 · n-1: PUT /me/onboarding — ровно один при любых повторах принятия (ФТ-02, §4.1 п.4)", () => {
  async function partnerToAccept(r: Rendered, inPage: boolean) {
    await heading("Игорь приглашает тебя");
    const next = async (name: string) => (inPage ? r.user.click(screen.getByRole("button", { name })) : r.pressMain());
    await next("Принять приглашение");
    await heading("Твой личный агент");
    await next("Далее");
    await heading("Что приватно");
    await next("Далее");
    await heading("Чем мы не являемся");
    await next("Начать");
    await heading("Давай познакомимся");
    await r.user.type(screen.getByLabelText("Как к тебе обращаться"), "Аня");
    for (const c of CONSENTS) await r.user.click(screen.getByRole("checkbox", { name: c }));
    await next("Продолжить");
  }

  test("503, «Помощь»→«Назад» дважды, снова 503, «Помощь»→«Назад», успех — PUT = 1, accept = 3", async () => {
    const server = new FakeServer()
      .partnerInvite()
      .inject("POST", `/invites/${PARTNER_TOKEN}/accept`, 503, { code: "service_unavailable", message: "x" }, undefined, 2);
    const r = renderApp({ server, tg: fakeTelegram({ startParam: PARTNER_TOKEN, firstName: "Аня" }) });
    await partnerToAccept(r, false);
    await screen.findByRole("alert");
    for (let i = 0; i < 2; i++) {
      await r.user.click(helpLink());
      await heading("Помощь");
      await r.pressBack();
      await heading("Давай познакомимся");
      expect(screen.queryByLabelText("Имя партнёра")).toBeNull();
    }
    expect(r.tg!.main.text).toBe("Принять приглашение снова");
    await r.pressMain();
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервис временно недоступен");
    expect(server.calls("POST", `/invites/${PARTNER_TOKEN}/accept`)).toHaveLength(2);
    await r.user.click(helpLink());
    await heading("Помощь");
    await r.pressBack();
    await heading("Давай познакомимся");
    await r.pressMain();
    expect(await heading("Вы пара")).toBeInTheDocument();
    expect(server.calls("PUT", "/me/onboarding")).toHaveLength(1);
    expect(server.calls("POST", `/invites/${PARTNER_TOKEN}/accept`)).toHaveLength(3);
  });

  test("нативные кнопки отказали — тот же путь кнопками в странице: 503 → «Помощь» → «‹ Назад» → повтор, PUT = 1", async () => {
    const tg = fakeTelegram({ startParam: PARTNER_TOKEN, firstName: "Аня" });
    const boom = () => {
      throw new Error("SDK failure");
    };
    tg.webApp.MainButton.show = boom;
    tg.webApp.BackButton.show = boom;
    const server = new FakeServer()
      .partnerInvite()
      .inject("POST", `/invites/${PARTNER_TOKEN}/accept`, 503, { code: "service_unavailable", message: "x" });
    const r = renderApp({ server, tg });
    await partnerToAccept(r, true);
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервис временно недоступен");
    await r.user.click(helpLink());
    await heading("Помощь");
    await r.user.click(screen.getByRole("button", { name: "‹ Назад" }));
    await heading("Давай познакомимся");
    expect(screen.queryByLabelText("Имя партнёра")).toBeNull();
    await r.user.click(screen.getByRole("button", { name: "Принять приглашение снова" }));
    expect(await heading("Вы пара")).toBeInTheDocument();
    expect(server.calls("PUT", "/me/onboarding")).toHaveLength(1);
    expect(server.calls("POST", `/invites/${PARTNER_TOKEN}/accept`)).toHaveLength(2);
  });

  test("отказ PUT /me/onboarding (503) — режима «принять снова» нет: форма на месте, повтор сохраняет и принимает", async () => {
    const server = new FakeServer()
      .partnerInvite()
      .inject("PUT", "/me/onboarding", 503, { code: "service_unavailable", message: "x" });
    const r = renderApp({ server, tg: fakeTelegram({ startParam: PARTNER_TOKEN, firstName: "Аня" }) });
    await partnerToAccept(r, false);
    await screen.findByRole("alert");
    expect(server.calls("POST", `/invites/${PARTNER_TOKEN}/accept`)).toHaveLength(0);
    await r.user.click(helpLink());
    await heading("Помощь");
    await r.pressBack();
    await heading("Давай познакомимся");
    expect(screen.getByLabelText("Имя партнёра")).toBeInTheDocument();
    expect(r.tg!.main.text).toBe("Продолжить");
    await r.pressMain();
    expect(await heading("Вы пара")).toBeInTheDocument();
    expect(server.calls("PUT", "/me/onboarding")).toHaveLength(2);
    expect(server.calls("POST", `/invites/${PARTNER_TOKEN}/accept`)).toHaveLength(1);
  });
});
