/**
 * n-2 (A6, р.2): внутри Telegram медленный telegram.org не даёт ложного «Открой в Telegram». Признак запуска из
 * Telegram — параметры в адресе (`#tgWebAppData=…`); пока SDK грузится — «Загружаем…» и «Помощь», а когда
 * скрипт всё же загрузился — приложение запускается заново уже с SDK.
 */
import { act, screen } from "@testing-library/react";
import { boot } from "./boot";
import { FakeServer } from "./test/fakeApi";
import { fakeTelegram } from "./test/fakeTelegram";
import { SDK_SCRIPT_ID, launchedFromTelegram } from "./telegram/webapp";

let script: HTMLScriptElement;
let container: HTMLDivElement;
let unmount: (() => void) | null = null;

beforeEach(() => {
  script = document.createElement("script");
  script.id = SDK_SCRIPT_ID;
  document.head.appendChild(script);
  container = document.createElement("div");
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => unmount?.());
  unmount = null;
  script.remove();
  container.remove();
  window.history.replaceState(null, "", window.location.pathname);
});

function openedFromTelegram() {
  // Параметры запуска, как их кладёт клиент Telegram (строка выдуманная).
  window.history.replaceState(null, "", "#tgWebAppData=query_id%3DTEST&tgWebAppVersion=8.0&tgWebAppPlatform=ios");
}

async function start(server = new FakeServer()) {
  await act(async () => {
    unmount = await boot({ container, fetch: server.fetch, sdkWaitMs: 20 });
  });
  return server;
}

const helpLink = () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" });

test("признак запуска из Telegram — по параметрам адреса", () => {
  expect(launchedFromTelegram(window)).toBe(false);
  openedFromTelegram();
  expect(launchedFromTelegram(window)).toBe(true);
  window.history.replaceState(null, "", "#other=1");
  expect(launchedFromTelegram(window)).toBe(false);
});

test("внутри Telegram SDK опоздал — «Загружаем…» и «Помощь», а не «Открой в Telegram»; пришёл — вход и приветствие", async () => {
  openedFromTelegram();
  const server = await start();
  expect(await screen.findByText(/Загружаем «Чувства AI»…/)).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Открой в Telegram" })).toBeNull();
  expect(helpLink()).toBeInTheDocument();
  expect(server.calls("POST", "/auth/telegram")).toHaveLength(0);

  const tg = fakeTelegram();
  window.Telegram = { WebApp: tg.webApp };
  act(() => {
    script.dataset.state = "loaded";
    script.dispatchEvent(new Event("load"));
  });
  expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
  expect(tg.readyCalled()).toBe(true);
  expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
});

test("внутри Telegram SDK опоздал, «Помощь» открыта — 112 ссылкой", async () => {
  openedFromTelegram();
  await start();
  await screen.findByText(/Загружаем «Чувства AI»…/);
  await act(async () => helpLink().click());
  expect(await screen.findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
});

test("внутри Telegram скрипт SDK упал после срока — причина подписью и «Помощь», не «Открой в Telegram»", async () => {
  openedFromTelegram();
  await start();
  await screen.findByText(/Загружаем «Чувства AI»…/);
  act(() => {
    script.dataset.state = "failed";
    script.dispatchEvent(new Event("error"));
  });
  expect(await screen.findByRole("heading", { level: 1, name: "Не получилось связаться с Telegram" })).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent(/Проверь интернет/);
  expect(screen.getByRole("button", { name: "Попробовать снова" })).toBeInTheDocument();
  expect(helpLink()).toBeInTheDocument();
});

test("внутри Telegram скрипт SDK упал ещё до запуска — сразу причина, без ожидания", async () => {
  openedFromTelegram();
  script.dataset.state = "failed";
  await start();
  expect(await screen.findByRole("heading", { level: 1, name: "Не получилось связаться с Telegram" })).toBeInTheDocument();
});

test("обычный браузер (нет параметров запуска), SDK не пришёл — «Открой в Telegram», как раньше", async () => {
  await start();
  expect(await screen.findByRole("heading", { level: 1, name: "Открой в Telegram" })).toBeInTheDocument();
});

test("SDK уже загружен до запуска — сразу вход, без экрана ожидания", async () => {
  openedFromTelegram();
  const tg = fakeTelegram();
  window.Telegram = { WebApp: tg.webApp };
  await start();
  expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
  expect(screen.queryByText(/Загружаем «Чувства AI»…/)).toBeNull();
});
