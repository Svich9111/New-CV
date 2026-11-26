/**
 * A4, задача 1.5, раунд 4 — n-7 (docs/reviews/1.5-review-r3.md) и замечание A8 р.3 №1 (1.5-test-r3.md).
 * Экран после ожидания SDK выбирается по событию самого скрипта, а не только по отметке sdk-state.js: если тот
 * не выполнился (404, блокировка, выкладка не в корень), ошибка SDK внутри Telegram всё равно даёт
 * «Не получилось связаться…» и «Попробовать снова», а «Загружаем…» не длится бесконечно. Открытая «Помощь»
 * переживает перезапуск при приходе SDK (ТЗ §8, §9.3).
 *
 * Отметки data-state здесь никто не ставит — это и есть случай «sdk-state.js не выполнился».
 */
import { act, screen } from "@testing-library/react";
import { boot } from "./boot";
import { FakeServer } from "./test/fakeApi";
import { fakeTelegram } from "./test/fakeTelegram";
import { SDK_GIVE_UP_MS, SDK_SCRIPT_ID, SDK_WAIT_MS, onSdkScriptSettled } from "./telegram/webapp";

let script: HTMLScriptElement;
let container: HTMLDivElement;
let unmount: (() => void) | null = null;

beforeEach(() => {
  script = document.createElement("script");
  script.id = SDK_SCRIPT_ID;
  document.head.appendChild(script);
  container = document.createElement("div");
  document.body.appendChild(container);
  delete window.Telegram;
  // Параметры запуска, как их кладёт клиент Telegram (строка выдуманная).
  window.history.replaceState(null, "", "#tgWebAppData=query_id%3DTEST&tgWebAppVersion=8.0&tgWebAppPlatform=ios");
});

afterEach(() => {
  act(() => unmount?.());
  unmount = null;
  script.remove();
  container.remove();
  delete window.Telegram;
  window.history.replaceState(null, "", window.location.pathname);
});

async function start(server = new FakeServer(), sdkGiveUpMs = 200) {
  await act(async () => {
    unmount = await boot({ container, fetch: server.fetch, sdkWaitMs: 20, sdkGiveUpMs });
  });
  return server;
}

const wait = (ms: number) => act(async () => new Promise((r) => setTimeout(r, ms)));
const fire = (type: "load" | "error") => act(() => void script.dispatchEvent(new Event(type)));
const helpLink = () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" });
const failed = () => screen.findByRole("heading", { level: 1, name: "Не получилось связаться с Telegram" });

describe("n-7: onSdkScriptSettled сообщает тип события", () => {
  for (const type of ["load", "error"] as const) {
    test(`${type} → cb("${type}") один раз, data-state не нужен`, () => {
      const cb = vi.fn();
      onSdkScriptSettled(window, cb);
      script.dispatchEvent(new Event(type));
      script.dispatchEvent(new Event(type === "load" ? "error" : "load"));
      expect(cb).toHaveBeenCalledTimes(1);
      expect(cb).toHaveBeenCalledWith(type);
      expect(script.dataset.state).toBeUndefined();
    });
  }

  test("отписка до события — cb не вызывается", () => {
    const cb = vi.fn();
    onSdkScriptSettled(window, cb)();
    script.dispatchEvent(new Event("error"));
    expect(cb).not.toHaveBeenCalled();
  });

  test("сроки: ожидание SDK короче срока отказа от ожидания", () => {
    expect(SDK_GIVE_UP_MS).toBeGreaterThan(SDK_WAIT_MS);
  });
});

describe("n-7: sdk-state.js не выполнился (нет data-state)", () => {
  test("error после срока ожидания — «Не получилось связаться…», «Попробовать снова», «Помощь»; входа нет", async () => {
    const server = await start(new FakeServer(), 60_000);
    await screen.findByText(/Загружаем «Чувства AI»…/);
    fire("error");
    expect(await failed()).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/Проверь интернет/);
    expect(screen.getByRole("button", { name: "Попробовать снова" })).toBeInTheDocument();
    expect(helpLink()).toBeInTheDocument();
    expect(server.log).toHaveLength(0);
  });

  test("load без Telegram.WebApp — причина, а не снова «Загружаем…»", async () => {
    await start(new FakeServer(), 60_000);
    await screen.findByText(/Загружаем «Чувства AI»…/);
    fire("load");
    expect(await failed()).toBeInTheDocument();
    expect(screen.queryByText(/Загружаем «Чувства AI»…/)).toBeNull();
  });

  test("SDK упал ещё до запуска (событие пропущено) — после срока причина и «Попробовать снова»; поздний load входит", async () => {
    const server = await start();
    expect(await screen.findByText(/Загружаем «Чувства AI»…/)).toBeInTheDocument();
    await wait(250);
    expect(await failed()).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Попробовать снова" })).toBeInTheDocument();
    expect(server.log).toHaveLength(0);
    // Скрипт всё же догрузился — приложение запускается с SDK.
    window.Telegram = { WebApp: fakeTelegram().webApp };
    fire("load");
    expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
  });

  test("load пришёл до срока — срок снят: экран не сменяется на причину, вход один", async () => {
    const server = await start();
    await screen.findByText(/Загружаем «Чувства AI»…/);
    window.Telegram = { WebApp: fakeTelegram().webApp };
    fire("load");
    expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
    await wait(300);
    expect(screen.getByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Не получилось связаться с Telegram" })).toBeNull();
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
  });

  test("размонтировано до срока — срок снят, ничего не рисуется", async () => {
    await start();
    await screen.findByText(/Загружаем «Чувства AI»…/);
    act(() => unmount?.());
    unmount = null;
    await wait(250);
    expect(container.textContent).toBe("");
  });
});

describe("A8 р.3 №1, n-6: «Помощь», открытая во время ожидания SDK, переживает перезапуск (ТЗ §9.3)", () => {
  test("SDK пришёл — вход, «Помощь» на месте со 112, нативная «Назад» → первый экран", async () => {
    const server = await start(new FakeServer(), 60_000);
    await screen.findByText(/Загружаем «Чувства AI»…/);
    await act(async () => helpLink().click());
    await screen.findByRole("link", { name: "112" });
    const tg = fakeTelegram();
    window.Telegram = { WebApp: tg.webApp };
    fire("load");
    await wait(30);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    expect(tg.back.visible).toBe(true);
    await act(async () => tg.back.click());
    expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
    expect(tg.back.visible).toBe(false);
  });

  test("SDK упал — «Помощь» на месте, «‹ Назад» → «Не получилось связаться…»", async () => {
    await start(new FakeServer(), 60_000);
    await screen.findByText(/Загружаем «Чувства AI»…/);
    await act(async () => helpLink().click());
    await screen.findByRole("link", { name: "112" });
    fire("error");
    await wait(30);
    expect(screen.getByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
    await act(async () => screen.getByRole("button", { name: "‹ Назад" }).click());
    expect(await failed()).toBeInTheDocument();
  });

  test("«Помощь» открыли и закрыли до прихода SDK — после входа первый экран без «Назад»", async () => {
    await start(new FakeServer(), 60_000);
    await screen.findByText(/Загружаем «Чувства AI»…/);
    await act(async () => helpLink().click());
    await screen.findByRole("link", { name: "112" });
    await act(async () => screen.getByRole("button", { name: "‹ Назад" }).click());
    await screen.findByText(/Загружаем «Чувства AI»…/);
    const tg = fakeTelegram();
    window.Telegram = { WebApp: tg.webApp };
    fire("load");
    expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
    expect(tg.back.visible).toBe(false);
  });

  test("«Помощь» открыта на «Не получилось связаться…» после срока — поздний SDK: вход, «Помощь» на месте", async () => {
    const server = await start();
    await failed();
    await act(async () => helpLink().click());
    await screen.findByRole("link", { name: "112" });
    const tg = fakeTelegram();
    window.Telegram = { WebApp: tg.webApp };
    fire("load");
    await wait(30);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
    await act(async () => tg.back.click());
    expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
  });
});
