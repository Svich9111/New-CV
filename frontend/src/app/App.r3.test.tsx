/**
 * A4, задача 1.5, раунд 3 — исправления по docs/reviews/1.5-review-r2.md (n-1, n-4) и 1.5-test-r2.md (D6, D7, D8)
 * в интерфейсе: отказ нативных кнопок и отклика, «Помощь» с 112 всегда, вход с ответом не по схеме, повтор
 * принятия приглашения после «Помощь» → «Назад».
 */
import { screen } from "@testing-library/react";
import { CONTACTS, FakeServer, PARTNER_TOKEN } from "../test/fakeApi";
import { fakeTelegram } from "../test/fakeTelegram";
import { heading, renderApp, type Rendered } from "../test/renderApp";
import type { TgBackButton, TgMainButton } from "../telegram/webapp";

const CONSENTS = [
  "Принимаю правила сервиса",
  "Даю согласие на обработку персональных данных",
  "Даю отдельное согласие на обработку сведений о чувствах и отношениях",
  "Мне исполнилось 18 лет",
];
const helpLink = () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" });
const boom = () => {
  throw new Error("SDK failure");
};

async function toOnboarding(r: Rendered, viaNative = true) {
  for (const name of ["Твой личный агент", "Что приватно", "Чем мы не являемся"]) {
    await heading(name);
    if (viaNative) await r.pressMain();
    else await r.user.click(screen.getByRole("button", { name: name === "Чем мы не являемся" ? "Начать" : "Далее" }));
  }
  await heading("Давай познакомимся");
}

describe("D6: отказ методов нативных кнопок и отклика — экран не падает, кнопка в странице", () => {
  for (const method of ["setText", "show", "onClick", "enable", "hideProgress"] as const) {
    test(`MainButton.${method} бросает всегда — приветствие и онбординг кнопками в странице, состояние подписью`, async () => {
      const tg = fakeTelegram();
      (tg.webApp.MainButton as unknown as Record<keyof TgMainButton, unknown>)[method] = boom;
      const r = renderApp({ tg });
      expect(await heading("Твой личный агент")).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Что-то пошло не так" })).toBeNull();
      await toOnboarding(r, false);
      // Недоступная кнопка в странице: disabled и подсказка текстом, а не только цветом.
      expect(screen.getByRole("button", { name: "Продолжить" })).toBeDisabled();
      expect(screen.getByRole("status")).toHaveTextContent(/Чтобы продолжить/);
      expect(helpLink()).toBeInTheDocument();
    });
  }

  test("BackButton.show бросает — «‹ Назад» в странице, ведёт назад; на следующих экранах тоже", async () => {
    const tg = fakeTelegram();
    let shows = 0;
    tg.webApp.BackButton.show = () => {
      shows++;
      throw new Error("WebAppBackButtonFailure");
    };
    const r = renderApp({ tg });
    await heading("Твой личный агент");
    await r.pressMain();
    await heading("Что приватно");
    await r.user.click(screen.getByRole("button", { name: "‹ Назад" }));
    expect(await heading("Твой личный агент")).toBeInTheDocument();
    await r.pressMain();
    await r.pressMain();
    await heading("Чем мы не являемся");
    expect(screen.getByRole("button", { name: "‹ Назад" })).toBeInTheDocument();
    expect(shows).toBe(1);
    expect(tg.back.visible).toBe(false);
  });

  for (const method of ["hide", "onClick"] as const) {
    test(`BackButton.${method} бросает — экран на месте, «Назад» доступна`, async () => {
      const tg = fakeTelegram();
      (tg.webApp.BackButton as unknown as Record<keyof TgBackButton, unknown>)[method] = boom;
      const r = renderApp({ tg });
      await heading("Твой личный агент");
      await r.pressMain();
      await heading("Что приватно");
      const inPage = screen.queryByRole("button", { name: "‹ Назад" });
      if (inPage) await r.user.click(inPage);
      else await r.pressBack();
      expect(await heading("Твой личный агент")).toBeInTheDocument();
    });
  }

  test("isVersionAtLeast бросает — приложение рисуется, ready() вызван, «Назад» в странице", async () => {
    const tg = fakeTelegram();
    tg.webApp.isVersionAtLeast = boom;
    const r = renderApp({ tg });
    expect(await heading("Твой личный агент")).toBeInTheDocument();
    expect(tg.readyCalled()).toBe(true);
    await r.pressMain();
    await heading("Что приватно");
    expect(screen.getByRole("button", { name: "‹ Назад" })).toBeInTheDocument();
  });

  test("HapticFeedback бросает — нажатие всё равно ведёт дальше", async () => {
    const tg = fakeTelegram();
    tg.webApp.HapticFeedback = { impactOccurred: boom, notificationOccurred: boom, selectionChanged: boom };
    const r = renderApp({ tg });
    await heading("Твой личный агент");
    await r.pressMain();
    expect(await heading("Что приватно")).toBeInTheDocument();
  });

  test("openTelegramLink бросает — подсказка подписью, ссылка на экране, экран не падает", async () => {
    const server = new FakeServer().onboarded();
    const tg = fakeTelegram();
    tg.webApp.openTelegramLink = boom;
    const r = renderApp({ server, tg });
    await heading("Пригласи партнёра");
    await r.pressMain(); // «Создать ссылку-приглашение»
    await screen.findByText(/Ссылка готова/);
    await r.pressMain(); // «Отправить в Telegram»
    expect(await screen.findByText(/Не получилось открыть выбор чата/)).toBeInTheDocument();
    expect(screen.getByText(/startapp=/)).toBeInTheDocument();
    expect(screen.getByText(/Ссылка готова/)).toBeInTheDocument();
  });
});

describe("D7: «Помощь» — номер 112 ссылкой всегда", () => {
  test("в списке сервера нет 112 — свои записи и 112 в конце; неверные и повторы отброшены", async () => {
    const server = new FakeServer();
    const line = { name: "Линия помощи", phone: "8-800-000-00-00", description: "Описание." };
    server.inject("GET", "/help/contacts", 200, [line, line, { name: "Плохая", phone: "8-800", description: "x", round_the_clock: "да" }]);
    const r = renderApp({ server });
    await heading("Твой личный агент");
    await r.user.click(helpLink());
    await heading("Помощь");
    expect(await screen.findByRole("link", { name: "8-800-000-00-00" })).toHaveAttribute("href", "tel:88000000000");
    const links = screen.getAllByRole("link");
    expect(links.map((l) => l.getAttribute("href"))).toEqual(["tel:88000000000", "tel:112"]);
    expect(screen.queryByText("Плохая")).toBeNull();
    // Запись без round_the_clock (граница контракта) показана без «Круглосуточно».
    expect(screen.getByText("Описание.")).not.toHaveTextContent("Круглосуточно");
  });

  test("в списке сервера есть 112 — запасной не дублируется", async () => {
    const r = renderApp();
    await heading("Твой личный агент");
    await r.user.click(helpLink());
    expect(await screen.findByText(CONTACTS[0].name)).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "112" })).toHaveLength(1);
  });
});

describe("n-4: вход с Me не по схеме — причина и повтор, а не онбординг", () => {
  test("session.me без булевых полей — «Не получилось войти», повтор входит", async () => {
    const server = new FakeServer().onboarded();
    server.inject("POST", "/auth/telegram", 200, { access_token: "t", expires_in: 3600, me: { id: "x" } });
    const r = renderApp({ server });
    expect(await heading("Не получилось войти")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Давай познакомимся" })).toBeNull();
    expect(r.tg!.main.text).toBe("Попробовать снова");
    await r.pressMain();
    expect(await heading("Пригласи партнёра")).toBeInTheDocument();
  });

  test("D8: inviter_name объектом — «Приглашение не работает» с повтором, без «[object Object]»", async () => {
    const server = new FakeServer().partnerInvite();
    server.inject("GET", `/invites/${PARTNER_TOKEN}`, 200, { inviter_name: { x: 1 }, expires_at: "2026-12-03T12:00:00Z" });
    const r = renderApp({ server, tg: fakeTelegram({ startParam: PARTNER_TOKEN }) });
    expect(await heading("Приглашение не работает")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("[object Object]");
    expect(r.tg!.main.text).toBe("Попробовать снова");
    await r.pressMain();
    expect(await heading("Игорь приглашает тебя")).toBeInTheDocument();
  });
});

describe("n-1: «онбординг сохранён, принять не удалось» переживает «Помощь» → «Назад»", () => {
  async function partnerToAccept(server: FakeServer): Promise<Rendered> {
    const r = renderApp({ server, tg: fakeTelegram({ startParam: PARTNER_TOKEN, firstName: "Аня" }) });
    await heading("Игорь приглашает тебя");
    await r.pressMain();
    await toOnboarding(r);
    await r.user.type(screen.getByLabelText("Как к тебе обращаться"), "Аня");
    for (const c of CONSENTS) await r.user.click(screen.getByRole("checkbox", { name: c }));
    await r.pressMain();
    return r;
  }

  test("503 → «Помощь» → «Назад» — только «Принять приглашение снова», PUT /me/onboarding = 1", async () => {
    const server = new FakeServer().partnerInvite().inject("POST", `/invites/${PARTNER_TOKEN}/accept`, 503, {
      code: "service_unavailable",
      message: "x",
    });
    const r = await partnerToAccept(server);
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервис временно недоступен");
    await r.user.click(helpLink());
    await heading("Помощь");
    await r.pressBack();
    expect(await heading("Давай познакомимся")).toBeInTheDocument();
    // Формы нет, причина видна, главное действие — повтор принятия.
    expect(screen.queryByLabelText("Имя партнёра")).toBeNull();
    expect(screen.getByText(/Знакомство сохранено/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Сервис временно недоступен");
    expect(r.tg!.main).toMatchObject({ visible: true, text: "Принять приглашение снова" });
    await r.pressMain();
    expect(await heading("Вы пара")).toBeInTheDocument();
    expect(server.calls("PUT", "/me/onboarding")).toHaveLength(1);
    expect(server.calls("POST", `/invites/${PARTNER_TOKEN}/accept`)).toHaveLength(2);
  });

  test("410 → «Помощь» → «Назад» — одно действие «Продолжить без приглашения», MainButton скрыта", async () => {
    const server = new FakeServer()
      .partnerInvite()
      .inject("POST", `/invites/${PARTNER_TOKEN}/accept`, 410, { code: "invite_used", message: "x" });
    const r = await partnerToAccept(server);
    await screen.findByRole("alert");
    await r.user.click(helpLink());
    await heading("Помощь");
    await r.pressBack();
    await heading("Давай познакомимся");
    expect(screen.queryByLabelText("Имя партнёра")).toBeNull();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(r.tg!.main.visible).toBe(false);
    await r.pressMain();
    expect(server.calls("PUT", "/me/onboarding")).toHaveLength(1);
    expect(server.calls("POST", `/invites/${PARTNER_TOKEN}/accept`)).toHaveLength(1);
    await r.user.click(screen.getByRole("button", { name: "Продолжить без приглашения" }));
    expect(await heading("Пригласи партнёра")).toBeInTheDocument();
  });
});
