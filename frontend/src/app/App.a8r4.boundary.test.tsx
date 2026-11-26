/**
 * A8, задача 1.5, раунд 4 — «Помощь», перенесённая из ожидания SDK (свойство `help`), открывается только в
 * первом поколении: «Перезапустить» с запасного экрана начинает сессию с нуля (n-6, ТЗ §9.3).
 */
import { act, render, screen } from "@testing-library/react";
import { Api } from "../api/client";
import { FakeServer } from "../test/fakeApi";
import { fakeTelegram } from "../test/fakeTelegram";
import { heading } from "../test/renderApp";
import { TelegramApi, findWebApp } from "../telegram/webapp";
import { App } from "./App";

const crash = vi.hoisted(() => ({ on: false }));

vi.mock("../screens/Welcome", async (importOriginal) => {
  const real = await importOriginal<typeof import("../screens/Welcome")>();
  return {
    Welcome: (props: { slide: number }) => {
      if (crash.on) throw new Error("отрисовка упала");
      return real.Welcome(props);
    },
  };
});

afterEach(() => {
  crash.on = false;
});

test("A8 р.4: help=true → «Помощь» после входа; экран упал → «Перезапустить» — первый экран, а не снова «Помощь»", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const server = new FakeServer();
  const tg = fakeTelegram();
  window.Telegram = { WebApp: tg.webApp };
  const tgApi = new TelegramApi(findWebApp(window));
  tgApi.start();
  const api = new Api({ initData: () => tgApi.initData, fetch: server.fetch });
  render(<App tg={tgApi} api={api} help />);

  // Вход ещё идёт — «Помощь» уже на экране; ждём окончания входа.
  await act(async () => new Promise((res) => setTimeout(res, 30)));
  expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
  expect(screen.getByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
  expect(tg.back.visible).toBe(true);
  crash.on = true;
  await act(async () => tg.back.click());
  expect(await heading("Что-то пошло не так")).toBeInTheDocument();

  crash.on = false;
  await act(async () => tg.main.click());
  expect(await heading("Твой личный агент")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { level: 1, name: "Помощь" })).toBeNull();
  expect(tg.back.visible).toBe(false);
  expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
});
