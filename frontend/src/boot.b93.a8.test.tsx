/// <reference types="node" />
/**
 * A8, задача 2.2, раунд 1 — Б-93 (ТЗ §9.3 «помощь всегда», §8): внутри Telegram без SDK экран ожидания с
 * «Помощью» и 112 — сразу, ни один таймер не сдвинут; срок отказа — от запуска (в том числе уже истёкший к
 * моменту отрисовки); заглушка `#root` — 112 ссылкой и в исходном, и в собранном index.html.
 */
import { act, screen } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
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

test("внутри Telegram без SDK: «Помощь» сразу открывается, 112 ссылкой виден до ответа сервера и без таймеров", async () => {
  window.history.replaceState(null, "", "#tgWebAppData=query_id%3DTEST&tgWebAppVersion=8.0");
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  // Сервер не отвечает вовсе: контакты помощи не придут.
  const hang: typeof fetch = () => new Promise<Response>(() => undefined);
  await act(async () => {
    unmount = await boot({ container, fetch: hang, sdkWaitMs: 60_000 });
  });
  expect(screen.getByText(/Загружаем «Чувства AI»…/)).toBeInTheDocument();
  await act(async () => helpLink()!.click());
  expect(screen.getByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
});

test("только tgWebAppVersion в строке запроса (без hash) — тоже без ожидания SDK", async () => {
  window.history.replaceState(null, "", "?tgWebAppVersion=8.0");
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  await act(async () => {
    unmount = await boot({ container, fetch: new FakeServer().fetch, sdkWaitMs: 60_000 });
  });
  expect(helpLink()).toBeInTheDocument();
});

test("страница «застыла» дольше срока до запуска — причина сразу (срок не начинается заново), «Помощь» на месте", async () => {
  window.history.replaceState(null, "", "#tgWebAppData=query_id%3DTEST&tgWebAppVersion=8.0");
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const reads = [0, 20_000];
  await act(async () => {
    unmount = await boot({ container, fetch: new FakeServer().fetch, sdkGiveUpMs: 15_000, now: () => reads.shift() ?? 20_000 });
  });
  await act(async () => vi.advanceTimersByTimeAsync(0));
  expect(screen.getByRole("heading", { name: "Не получилось связаться с Telegram" })).toBeInTheDocument();
  expect(helpLink()).toBeInTheDocument();
});

test("SDK уже упал до запуска (data-state=failed) внутри Telegram — причина сразу, без ожидания", async () => {
  window.history.replaceState(null, "", "#tgWebAppData=query_id%3DTEST&tgWebAppVersion=8.0");
  script.dataset.state = "failed";
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  await act(async () => {
    unmount = await boot({ container, fetch: new FakeServer().fetch, sdkWaitMs: 60_000 });
  });
  expect(screen.getByRole("heading", { name: "Не получилось связаться с Telegram" })).toBeInTheDocument();
  expect(helpLink()).toBeInTheDocument();
});

test("заглушка #root: 112 ссылкой и в собранном dist/index.html (если сборка есть)", () => {
  const dist = join(__dirname, "../dist/index.html");
  const files = [join(__dirname, "../index.html"), ...(existsSync(dist) ? [dist] : [])];
  for (const f of files) {
    const html = readFileSync(f, "utf8");
    const root = /<div id="root">([\s\S]*?)<\/div>/.exec(html)![1];
    expect(root, f).toContain('href="tel:112"');
  }
});
