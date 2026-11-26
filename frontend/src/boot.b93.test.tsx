/// <reference types="node" />
/**
 * A4, задача 2.2 попутно — Б-93 (тест 1.5 р.4, замечание 1): внутри Telegram «Загружаем…» с «Помощью» видно
 * сразу, без SDK_WAIT_MS с голой заглушкой; срок SDK_GIVE_UP_MS — от запуска страницы (см. также обращённый
 * страж в boot.a8r4.test.tsx); в заглушке index.html — номер 112 ссылкой (помощь до запуска JS, ТЗ §9.3).
 */
import { act, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { boot } from "./boot";
import { FakeServer } from "./test/fakeApi";
import { SDK_SCRIPT_ID } from "./telegram/webapp";

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
});

afterEach(() => {
  act(() => unmount?.());
  unmount = null;
  vi.useRealTimers();
  script.remove();
  container.remove();
  window.history.replaceState(null, "", window.location.pathname);
});

const helpLink = () => screen.queryByRole("button", { name: "Помощь и контакты служб поддержки" });

test("внутри Telegram без SDK — экран ожидания с «Помощью» сразу, а не через SDK_WAIT_MS", async () => {
  window.history.replaceState(null, "", "#tgWebAppData=query_id%3DTEST&tgWebAppVersion=8.0");
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const server = new FakeServer();
  await act(async () => {
    unmount = await boot({ container, fetch: server.fetch, sdkWaitMs: 60_000 });
  });
  // Ни один таймер не сдвинут: ожидание SDK не держит экран.
  expect(screen.getByText(/Загружаем «Чувства AI»…/)).toBeInTheDocument();
  expect(helpLink()).toBeInTheDocument();
  expect(server.log).toHaveLength(0);
});

test("срок «Загружаем…» — от запуска: часы уже ушли вперёд к моменту отрисовки — остаток срока, не полный срок", async () => {
  window.history.replaceState(null, "", "#tgWebAppData=query_id%3DTEST&tgWebAppVersion=8.0");
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  // Первое чтение часов — запуск (0 мс), второе — постановка срока (прошло 10 000 мс: долгая отрисовка).
  const reads = [0, 10_000];
  await act(async () => {
    unmount = await boot({ container, fetch: new FakeServer().fetch, sdkGiveUpMs: 15_000, now: () => reads.shift() ?? 10_000 });
  });
  await act(async () => vi.advanceTimersByTimeAsync(4_999));
  expect(screen.queryByRole("heading", { name: "Не получилось связаться с Telegram" })).toBeNull();
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(screen.getByRole("heading", { name: "Не получилось связаться с Telegram" })).toBeInTheDocument();
});

test("обычный браузер — по-прежнему ждём SDK до SDK_WAIT_MS, затем «Открой в Telegram»", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  let p: Promise<() => void> = Promise.resolve(() => undefined);
  await act(async () => {
    p = boot({ container, fetch: new FakeServer().fetch, sdkWaitMs: 1_000 });
  });
  expect(screen.queryByRole("heading", { name: "Открой в Telegram" })).toBeNull();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_000);
    unmount = await p;
  });
  expect(screen.getByRole("heading", { name: "Открой в Telegram" })).toBeInTheDocument();
});

test("заглушка index.html — 112 ссылкой до запуска приложения", () => {
  const html = readFileSync(join(__dirname, "../index.html"), "utf8");
  const root = /<div id="root">([\s\S]*?)<\/div>/.exec(html)![1];
  expect(root).toContain('<a href="tel:112">112</a>');
  expect(root).toMatch(/Загружаем «Чувства AI»…/);
});
