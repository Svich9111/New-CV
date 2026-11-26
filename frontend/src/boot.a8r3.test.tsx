/**
 * A8, задача 1.5, раунд 3 — запуск (boot.tsx, n-2) и отметка загрузки SDK (public/sdk-state.js, n-3).
 * Негативные сценарии ТЗ: поддельные данные запуска в адресе вне Telegram (ФТ-01: вход только по подписанному
 * initData; §9.1 п.5: initData не пишется в хранилище и не показывается), гонки загрузки скрипта SDK.
 * §9.3 — «Помощь» доступна при любом состоянии SDK.
 */
import { act, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { boot } from "./boot";
import { FakeServer } from "./test/fakeApi";
import { fakeTelegram } from "./test/fakeTelegram";
import { SDK_SCRIPT_ID } from "./telegram/webapp";

const SECRET = "Зинаида-Тайная";
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
  script.remove();
  container.remove();
  delete window.Telegram;
  window.history.replaceState(null, "", window.location.pathname);
});

/** Адрес с параметрами запуска, как их кладёт Telegram, — но страница открыта в обычном браузере (подделка). */
function forgedHash() {
  const data = "query_id=X&user=" + encodeURIComponent(JSON.stringify({ id: 9, first_name: SECRET })) + "&auth_date=1&hash=forged";
  window.history.replaceState(null, "", "#" + new URLSearchParams({ tgWebAppData: data, tgWebAppVersion: "8.0" }).toString());
}

async function start(server = new FakeServer()) {
  await act(async () => {
    unmount = await boot({ container, fetch: server.fetch, sdkWaitMs: 20 });
  });
  return server;
}

const settle = (state: "loaded" | "failed") =>
  act(() => {
    script.dataset.state = state;
    script.dispatchEvent(new Event(state === "loaded" ? "load" : "error"));
  });

describe("A8 р.3 · tgWebAppData в адресе вне Telegram без SDK (ФТ-01, §9.1 п.5)", () => {
  test("ожидание SDK: ни одного запроса к серверу, имя из адреса не показано, хранилища пусты", async () => {
    forgedHash();
    const server = await start();
    expect(await screen.findByText(/Загружаем «Чувства AI»…/)).toBeInTheDocument();
    await act(async () => new Promise((r) => setTimeout(r, 50)));
    expect(server.log).toHaveLength(0);
    expect(document.body.textContent).not.toContain(SECRET);
    expect(document.body.textContent).not.toContain("query_id");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });

  test("SDK упал — «Не получилось связаться», без входа и без имени из адреса; «Помощь» с tel:112", async () => {
    forgedHash();
    const server = await start();
    await screen.findByText(/Загружаем «Чувства AI»…/);
    settle("failed");
    expect(await screen.findByRole("heading", { level: 1, name: "Не получилось связаться с Telegram" })).toBeInTheDocument();
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(0);
    expect(document.body.textContent).not.toContain(SECRET);
    await act(async () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" }).click());
    expect(await screen.findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(0);
  });

  test("скрипт «загрузился», но Telegram.WebApp нет — причина, а не вход и не вечное ожидание", async () => {
    forgedHash();
    script.dataset.state = "loaded";
    const server = await start();
    expect(await screen.findByRole("heading", { level: 1, name: "Не получилось связаться с Telegram" })).toBeInTheDocument();
    expect(server.log).toHaveLength(0);
  });

  test("SDK пришёл, но initData пустой (обычный браузер) — «Открой в Telegram», вход не начинается", async () => {
    forgedHash();
    const server = await start();
    await screen.findByText(/Загружаем «Чувства AI»…/);
    window.Telegram = { WebApp: fakeTelegram({ initData: "" }).webApp };
    settle("loaded");
    expect(await screen.findByRole("heading", { level: 1, name: "Открой в Telegram" })).toBeInTheDocument();
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(0);
  });
});

describe("A8 р.3 · гонки загрузки SDK (n-2, n-3)", () => {
  test("load после размонтирования — приложение не запускается заново, запросов нет", async () => {
    forgedHash();
    const server = await start();
    await screen.findByText(/Загружаем «Чувства AI»…/);
    act(() => unmount?.());
    unmount = null;
    window.Telegram = { WebApp: fakeTelegram().webApp };
    settle("loaded");
    await act(async () => new Promise((r) => setTimeout(r, 30)));
    expect(container.textContent).toBe("");
    expect(server.log).toHaveLength(0);
  });

  test("load пришёл дважды (повтор события) — один перезапуск, один вход", async () => {
    forgedHash();
    const server = await start();
    await screen.findByText(/Загружаем «Чувства AI»…/);
    const tg = fakeTelegram();
    window.Telegram = { WebApp: tg.webApp };
    settle("loaded");
    act(() => {
      script.dispatchEvent(new Event("load"));
      script.dispatchEvent(new Event("error"));
    });
    expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
    await act(async () => new Promise((r) => setTimeout(r, 30)));
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
  });

  test("SDK загрузился во время ожидания, пока была открыта «Помощь» — вход и обычный экран, без запасного", async () => {
    forgedHash();
    const server = await start();
    await screen.findByText(/Загружаем «Чувства AI»…/);
    await act(async () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" }).click());
    await screen.findByRole("link", { name: "112" });
    window.Telegram = { WebApp: fakeTelegram().webApp };
    settle("loaded");
    expect(await screen.findByRole("heading", { level: 1, name: "Твой личный агент" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Что-то пошло не так" })).toBeNull();
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
  });
});

describe("A8 р.3 · public/sdk-state.js — первая отметка окончательная", () => {
  const code = readFileSync(resolve(__dirname, "../public/sdk-state.js"), "utf8");
  const listeners: [string, EventListener][] = [];

  beforeEach(() => {
    const add = document.addEventListener.bind(document);
    vi.spyOn(document, "addEventListener").mockImplementation((t: string, l: EventListenerOrEventListenerObject, o?: boolean | AddEventListenerOptions) => {
      listeners.push([t, l as EventListener]);
      add(t, l, o);
    });
    new Function(code)();
    vi.mocked(document.addEventListener).mockRestore();
  });

  afterEach(() => {
    for (const [t, l] of listeners.splice(0)) document.removeEventListener(t, l, true);
  });

  test("error, затем load — остаётся failed; приложение показывает причину", async () => {
    script.dispatchEvent(new Event("error"));
    script.dispatchEvent(new Event("load"));
    expect(script.dataset.state).toBe("failed");
  });

  test("событие load у другого элемента (изображение) — SDK не отмечен", () => {
    const other = document.createElement("img");
    document.body.appendChild(other);
    other.dispatchEvent(new Event("load"));
    other.remove();
    expect(script.dataset.state).toBeUndefined();
  });
});
