import { screen, waitFor } from "@testing-library/react";
import { FakeServer } from "../test/fakeApi";
import { fakeTelegram, INIT_DATA } from "../test/fakeTelegram";
import { heading, renderApp } from "../test/renderApp";

describe("Вне Telegram", () => {
  test("экран «Открой в Telegram», вход не запрашивается", async () => {
    const { server } = renderApp({ tg: null });
    expect(await heading("Открой в Telegram")).toBeInTheDocument();
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(0);
  });

  test("SDK есть, но initData пустой (обычный браузер) — тоже «Открой в Telegram»", async () => {
    const { server } = renderApp({ tg: fakeTelegram({ initData: "" }) });
    expect(await heading("Открой в Telegram")).toBeInTheDocument();
    expect(server.log).toHaveLength(0);
  });

  test("«Помощь» доступна и вне Telegram, с кнопкой «Назад» в странице", async () => {
    const { user } = renderApp({ tg: null });
    await user.click(await screen.findByRole("button", { name: /Помощь/ }));
    expect(await heading("Помощь")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    await user.click(screen.getByRole("button", { name: /Назад/ }));
    expect(await heading("Открой в Telegram")).toBeInTheDocument();
  });
});

describe("Вход по initData (ФТ-01)", () => {
  test("initData уходит в POST /auth/telegram; новый пользователь видит приветствие", async () => {
    const { server, tg } = renderApp();
    expect(await heading("Твой личный агент")).toBeInTheDocument();
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
    expect(server.calls("POST", "/auth/telegram")[0].body).toEqual({ init_data: INIT_DATA });
    expect(tg!.readyCalled()).toBe(true);
  });

  test("неверная подпись — «Не получилось войти», причина подписью, без повтора", async () => {
    const server = new FakeServer().inject("POST", "/auth/telegram", 401, {
      code: "invalid_init_data",
      message: "Не удалось подтвердить вход через Telegram",
    });
    const { tg } = renderApp({ server });
    expect(await heading("Не получилось войти")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/Ошибка: Не получилось подтвердить вход через Telegram/);
    expect(tg!.main.visible).toBe(false);
  });

  test("сервер недоступен (5xx) — «Попробовать снова» на MainButton, повтор входит", async () => {
    const server = new FakeServer().inject("POST", "/auth/telegram", 503, {
      code: "service_unavailable",
      message: "Вход временно недоступен",
    });
    const { tg, pressMain } = renderApp({ server });
    expect(await heading("Не получилось войти")).toBeInTheDocument();
    expect(tg!.main).toMatchObject({ visible: true, text: "Попробовать снова" });
    await pressMain();
    expect(await heading("Твой личный агент")).toBeInTheDocument();
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
  });

  test("обрыв связи при входе — понятный текст", async () => {
    const server = new FakeServer();
    server.fetch = async () => {
      throw new TypeError("Failed to fetch");
    };
    renderApp({ server });
    expect(await screen.findByRole("alert")).toHaveTextContent("Нет связи с сервером");
  });

  test("онбординг пройден, пары нет — экран приглашения", async () => {
    renderApp({ server: new FakeServer().onboarded() });
    expect(await heading("Пригласи партнёра")).toBeInTheDocument();
  });

  test("пара уже активна — «Вы пара»", async () => {
    const server = new FakeServer().onboarded().pendingCouple();
    server.partnerAccepts();
    renderApp({ server });
    expect(await heading("Вы пара")).toBeInTheDocument();
    expect(await screen.findByText(/Ты и Игорь теперь на связи/)).toBeInTheDocument();
  });

  test("ни initData, ни токен не попадают в хранилища браузера и в адрес", async () => {
    const { server } = renderApp();
    await heading("Твой личный агент");
    expect(server.token).toBeTruthy();
    expect(localStorage.length + sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
    expect(window.location.href).not.toContain(server.token!);
    expect(document.body.innerHTML).not.toContain(server.token!);
    expect(document.body.innerHTML).not.toContain("hash=fake");
  });
});

describe("Тема Telegram (ТЗ §8)", () => {
  test("тёмная тема Telegram → data-theme=dark", async () => {
    renderApp({ tg: fakeTelegram({ colorScheme: "dark" }) });
    await heading("Твой личный агент");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  test("смена темы в Telegram применяется без перезапуска", async () => {
    const tg = fakeTelegram({ colorScheme: "light" });
    renderApp({ tg });
    await heading("Твой личный агент");
    expect(document.documentElement.dataset.theme).toBe("light");
    tg.webApp.colorScheme = "dark";
    tg.emit("themeChanged");
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("dark"));
  });
});

describe("Приветствие (ТЗ §8: три слайда)", () => {
  test("три слайда с MainButton «Далее» → «Начать», «Назад» Telegram возвращает слайд", async () => {
    const { tg, pressMain, pressBack } = renderApp();
    expect(await heading("Твой личный агент")).toBeInTheDocument();
    expect(screen.getByText("Слайд 1 из 3")).toBeInTheDocument();
    expect(tg!.main).toMatchObject({ visible: true, text: "Далее" });
    expect(tg!.back.visible).toBe(false);

    await pressMain();
    expect(await heading("Что приватно")).toBeInTheDocument();
    expect(screen.getByText(/Разговор с агентом видишь только ты/)).toBeInTheDocument();
    expect(tg!.back.visible).toBe(true);

    await pressMain();
    expect(await heading("Чем мы не являемся")).toBeInTheDocument();
    expect(screen.getByText(/не заменяет психолога/)).toBeInTheDocument();
    expect(tg!.main.text).toBe("Начать");

    await pressBack();
    expect(await heading("Что приватно")).toBeInTheDocument();
    await pressMain();
    await pressMain();
    expect(await heading("Давай познакомимся")).toBeInTheDocument();
    expect(tg!.haptics).toContain("impact:light");
  });

  test("ссылка на «Помощь» есть на каждом экране, «Назад» возвращает", async () => {
    const { user, pressBack, tg } = renderApp();
    await heading("Твой личный агент");
    await user.click(screen.getByRole("button", { name: /Помощь/ }));
    expect(await heading("Помощь")).toBeInTheDocument();
    expect(tg!.main.visible).toBe(false);
    expect(screen.queryByRole("button", { name: /Помощь и контакты/ })).not.toBeInTheDocument();
    await pressBack();
    expect(await heading("Твой личный агент")).toBeInTheDocument();
  });
});

describe("Старый клиент Telegram и доступность", () => {
  test("Bot API 6.0 без нативной «Назад» — кнопка «Назад» в странице", async () => {
    const { user, pressMain, tg } = renderApp({ tg: fakeTelegram({ version: "6.0" }) });
    await heading("Твой личный агент");
    await pressMain();
    await heading("Что приватно");
    expect(tg!.back.visible).toBe(false);
    await user.click(screen.getByRole("button", { name: /Назад/ }));
    expect(await heading("Твой личный агент")).toBeInTheDocument();
  });

  test("при смене экрана фокус на заголовке нового экрана", async () => {
    const { pressMain } = renderApp();
    await heading("Твой личный агент");
    await pressMain();
    const h = await heading("Что приватно");
    expect(h).toHaveFocus();
  });

  test("у каждого поля и кнопки онбординга есть доступное имя", async () => {
    const { pressMain } = renderApp();
    await heading("Твой личный агент");
    await pressMain();
    await pressMain();
    await pressMain();
    await heading("Давай познакомимся");
    for (const el of [...screen.getAllByRole("textbox"), ...screen.getAllByRole("checkbox"), ...screen.getAllByRole("button")])
      expect(el).toHaveAccessibleName();
  });
});
