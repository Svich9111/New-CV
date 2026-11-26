import { SDK_SCRIPT_ID, TelegramApi, findWebApp, waitForTelegramSdk } from "./webapp";
import { fakeTelegram } from "../test/fakeTelegram";

describe("Telegram SDK", () => {
  test("вне Telegram: нет SDK — не внутри", () => {
    expect(findWebApp(window)).toBeNull();
    expect(new TelegramApi(null).inside).toBe(false);
  });

  test("SDK загружен в обычном браузере (initData пустой) — не внутри", () => {
    const fake = fakeTelegram({ initData: "" });
    window.Telegram = { WebApp: fake.webApp };
    expect(findWebApp(window)).toBeNull();
  });

  test("внутри Telegram: initData и start_param", () => {
    const fake = fakeTelegram({ startParam: "abc" });
    window.Telegram = { WebApp: fake.webApp };
    const tg = new TelegramApi(findWebApp(window));
    expect(tg.inside).toBe(true);
    expect(tg.startParam).toBe("abc");
  });

  test("тема Telegram → data-theme; смена темы на лету", () => {
    const fake = fakeTelegram({ colorScheme: "dark" });
    const tg = new TelegramApi(fake.webApp);
    const stop = tg.start();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(fake.readyCalled()).toBe(true);
    fake.webApp.colorScheme = "light";
    fake.emit("themeChanged");
    expect(document.documentElement.dataset.theme).toBe("light");
    stop();
    fake.webApp.colorScheme = "dark";
    fake.emit("themeChanged");
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  test("цвет шапки Telegram берётся из токена --bg", () => {
    document.documentElement.style.setProperty("--bg", "#000000");
    const fake = fakeTelegram({ colorScheme: "dark" });
    new TelegramApi(fake.webApp).applyTheme();
    expect(fake.headerColors).toEqual(["#000000"]);
    document.documentElement.style.removeProperty("--bg");
  });

  test("тактильный отклик и «назад» — только с версии 6.1", () => {
    const old = fakeTelegram({ version: "6.0" });
    const tgOld = new TelegramApi(old.webApp);
    tgOld.impact();
    expect(old.haptics).toEqual([]);
    expect(tgOld.nativeBack).toBe(false);
    const cur = fakeTelegram();
    const tg = new TelegramApi(cur.webApp);
    tg.impact();
    tg.notify("success");
    expect(cur.haptics).toEqual(["impact:light", "notify:success"]);
    expect(tg.nativeBack).toBe(true);
  });

  test("пересылка ссылки — через t.me/share/url с экранированием", () => {
    const fake = fakeTelegram();
    new TelegramApi(fake.webApp).shareLink("https://t.me/b?startapp=X&y", "Привет");
    expect(fake.opened[0]).toBe(
      "https://t.me/share/url?url=https%3A%2F%2Ft.me%2Fb%3Fstartapp%3DX%26y&text=%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82",
    );
    expect(new TelegramApi(null).shareLink("x", "y")).toBe(false);
  });

  describe("цвет шапки по версии Bot API (MAJOR-1, р.1; поведение — по telegram-web-app.js)", () => {
    beforeEach(() => document.documentElement.style.setProperty("--bg", "#ffffff"));
    afterEach(() => document.documentElement.style.removeProperty("--bg"));

    test("подделка бросает на hex в 6.2, как настоящий SDK", () => {
      const fake = fakeTelegram({ version: "6.2" });
      expect(() => fake.webApp.setHeaderColor!("#ffffff")).toThrow("WebAppHeaderColorKeyInvalid");
    });

    for (const version of ["6.1", "6.2", "6.8"]) {
      test(`Bot API ${version}: шапка — ключ bg_color, фон — hex из токена; запуск не падает, ready() вызван`, () => {
        const fake = fakeTelegram({ version });
        const tg = new TelegramApi(fake.webApp);
        expect(() => tg.start()).not.toThrow();
        expect(fake.readyCalled()).toBe(true);
        expect(fake.headerColors).toEqual(["bg_color"]);
        expect(fake.backgroundColors).toEqual(["#ffffff"]);
        fake.emit("themeChanged");
        expect(fake.headerColors).toEqual(["bg_color", "bg_color"]);
      });
    }

    test("Bot API 6.9 и новее: шапка — hex из токена", () => {
      const fake = fakeTelegram({ version: "6.9" });
      new TelegramApi(fake.webApp).start();
      expect(fake.headerColors).toEqual(["#ffffff"]);
    });

    test("Bot API 6.0: цвета не задаются", () => {
      const fake = fakeTelegram({ version: "6.0" });
      new TelegramApi(fake.webApp).start();
      expect(fake.headerColors).toEqual([]);
      expect(fake.backgroundColors).toEqual([]);
      expect(fake.readyCalled()).toBe(true);
    });

    test("любой метод SDK бросает — start() не бросает, ready() вызван, тема применена", () => {
      const fake = fakeTelegram({ colorScheme: "dark", throwOn: ["setHeaderColor", "setBackgroundColor", "onEvent", "expand"] });
      const tg = new TelegramApi(fake.webApp);
      let stop: () => void = () => undefined;
      expect(() => (stop = tg.start())).not.toThrow();
      expect(fake.readyCalled()).toBe(true);
      expect(document.documentElement.dataset.theme).toBe("dark");
      expect(() => stop()).not.toThrow();
    });
  });

  test("подписчики смены темы вызываются после обновления data-theme; отписка работает", () => {
    const fake = fakeTelegram({ colorScheme: "light" });
    const tg = new TelegramApi(fake.webApp);
    tg.start();
    const seen: (string | undefined)[] = [];
    const off = tg.onThemeChange(() => seen.push(document.documentElement.dataset.theme));
    fake.webApp.colorScheme = "dark";
    fake.emit("themeChanged");
    off();
    fake.emit("themeChanged");
    expect(seen).toEqual(["dark"]);
  });

  describe("ожидание SDK (m-9: скрипт грузится асинхронно)", () => {
    let script: HTMLScriptElement;
    beforeEach(() => {
      script = document.createElement("script");
      script.id = SDK_SCRIPT_ID;
      document.head.appendChild(script);
    });
    afterEach(() => script.remove());

    test("SDK уже на месте — сразу", async () => {
      window.Telegram = { WebApp: fakeTelegram().webApp };
      await expect(waitForTelegramSdk(window, 10_000)).resolves.toBeUndefined();
    });

    test("скрипт уже загрузился или упал до запуска (data-state) — сразу", async () => {
      script.dataset.state = "failed";
      await expect(waitForTelegramSdk(window, 10_000)).resolves.toBeUndefined();
    });

    for (const event of ["load", "error"]) {
      test(`событие ${event} у скрипта — продолжаем`, async () => {
        const p = waitForTelegramSdk(window, 10_000);
        script.dispatchEvent(new Event(event));
        await expect(p).resolves.toBeUndefined();
      });
    }

    test("telegram.org не отвечает — продолжаем по сроку", async () => {
      vi.useFakeTimers();
      try {
        let done = false;
        void waitForTelegramSdk(window, 4_000).then(() => (done = true));
        await vi.advanceTimersByTimeAsync(3_999);
        expect(done).toBe(false);
        await vi.advanceTimersByTimeAsync(1);
        expect(done).toBe(true);
      } finally {
        vi.useRealTimers();
      }
    });
  });
});
