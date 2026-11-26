import { TelegramApi, findWebApp } from "./webapp";
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
});
