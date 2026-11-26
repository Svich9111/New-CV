/**
 * A8, задача 1.5, раунд 4 — перепроверка n-6/n-7 (docs/reviews/1.5-review-r3.md) и замечаний A8 р.3 №1–3.
 * Срок ожидания SDK по умолчанию (15 с, SDK_GIVE_UP_MS), «Помощь» не закрывается сменой экрана по сроку,
 * поздние повторные события скрипта не дают второго входа (ТЗ §8, §9.3).
 * Отметку data-state здесь никто не ставит: это случай «sdk-state.js не выполнился».
 */
import { act, screen } from "@testing-library/react";
import { boot } from "./boot";
import { FakeServer } from "./test/fakeApi";
import { fakeTelegram } from "./test/fakeTelegram";
import { SDK_GIVE_UP_MS, SDK_SCRIPT_ID } from "./telegram/webapp";

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
  vi.useRealTimers();
  script.remove();
  container.remove();
  delete window.Telegram;
  window.history.replaceState(null, "", window.location.pathname);
});

const wait = (ms: number) => act(async () => new Promise((r) => setTimeout(r, ms)));
const fire = (type: "load" | "error") => act(() => void script.dispatchEvent(new Event(type)));
const helpLink = () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" });
const failedHeading = () => screen.queryByRole("heading", { level: 1, name: "Не получилось связаться с Telegram" });
const loadingText = () => screen.queryByText(/Загружаем «Чувства AI»…/);

describe("A8 р.4 · срок ожидания SDK по умолчанию — 15 с (n-7)", () => {
  // Обращён под Б-93 (A4, задача 2.2): раньше срок шёл после SDK_WAIT_MS (≈ 19 с вместо 15), теперь — от запуска
  // страницы; «Загружаем…» с «Помощью» показывается сразу, без ожидания SDK_WAIT_MS.
  test("SDK_GIVE_UP_MS = 15 000; без sdkGiveUpMs «Загружаем…» с «Помощью» — сразу, причина — ровно через 15 с от запуска", async () => {
    expect(SDK_GIVE_UP_MS).toBe(15_000);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let clock = 0;
    const server = new FakeServer();
    await act(async () => {
      unmount = await boot({ container, fetch: server.fetch, sdkWaitMs: 4_000, now: () => clock });
    });
    expect(loadingText()).not.toBeNull();
    expect(helpLink()).toBeInTheDocument();
    clock = SDK_GIVE_UP_MS - 1;
    await act(async () => vi.advanceTimersByTimeAsync(SDK_GIVE_UP_MS - 1));
    expect(loadingText()).not.toBeNull();
    expect(failedHeading()).toBeNull();
    clock = SDK_GIVE_UP_MS;
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(failedHeading()).not.toBeNull();
    expect(screen.getByRole("button", { name: "Попробовать снова" })).toBeInTheDocument();
    expect(loadingText()).toBeNull();
    expect(server.log).toHaveLength(0);
  });
});

describe("A8 р.4 · «Помощь» не закрывается сменой экрана по сроку или событию (ТЗ §9.3)", () => {
  test("«Помощь» открыта на «Загружаем…», вышел срок — «Помощь» на месте, «‹ Назад» → причина", async () => {
    const server = new FakeServer();
    await act(async () => {
      unmount = await boot({ container, fetch: server.fetch, sdkWaitMs: 20, sdkGiveUpMs: 150 });
    });
    await screen.findByText(/Загружаем «Чувства AI»…/);
    await act(async () => helpLink().click());
    await screen.findByRole("link", { name: "112" });
    await wait(250);
    expect(screen.getByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    await act(async () => screen.getByRole("button", { name: "‹ Назад" }).click());
    expect(await screen.findByRole("heading", { level: 1, name: "Не получилось связаться с Telegram" })).toBeInTheDocument();
  });

  test("«Помощь» открыта на «Загружаем…», вышел срок, затем поздний load — вход один, «Помощь» на месте, «Назад» → первый экран", async () => {
    const server = new FakeServer();
    await act(async () => {
      unmount = await boot({ container, fetch: server.fetch, sdkWaitMs: 20, sdkGiveUpMs: 150 });
    });
    await screen.findByText(/Загружаем «Чувства AI»…/);
    await act(async () => helpLink().click());
    await screen.findByRole("link", { name: "112" });
    await wait(250);
    const tg = fakeTelegram();
    window.Telegram = { WebApp: tg.webApp };
    fire("load");
    await wait(30);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
    expect(tg.back.visible).toBe(true);
    await act(async () => tg.back.click());
    expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
    expect(tg.back.visible).toBe(false);
  });
});

describe("A8 р.4 · нет двойного входа (n-7)", () => {
  test("load, затем повторные load/error и истёкший срок — вход один, экран не сменяется", async () => {
    const server = new FakeServer();
    await act(async () => {
      unmount = await boot({ container, fetch: server.fetch, sdkWaitMs: 20, sdkGiveUpMs: 150 });
    });
    await screen.findByText(/Загружаем «Чувства AI»…/);
    window.Telegram = { WebApp: fakeTelegram().webApp };
    fire("load");
    expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
    fire("load");
    fire("error");
    await wait(250);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
    expect(failedHeading()).toBeNull();
  });

  test("срок вышел, поздний load, затем ещё load — вход один (слушатель снят после первого события)", async () => {
    const server = new FakeServer();
    await act(async () => {
      unmount = await boot({ container, fetch: server.fetch, sdkWaitMs: 20, sdkGiveUpMs: 100 });
    });
    await wait(200);
    expect(failedHeading()).not.toBeNull();
    window.Telegram = { WebApp: fakeTelegram().webApp };
    fire("load");
    expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
    fire("load");
    await wait(50);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
  });
});
