import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Api } from "../api/client";
import { App } from "../app/App";
import { TelegramApi, findWebApp } from "../telegram/webapp";
import { FakeServer } from "./fakeApi";
import { fakeTelegram, type FakeTelegram } from "./fakeTelegram";

export interface Rendered {
  server: FakeServer;
  tg: FakeTelegram | null;
  api: Api;
  user: ReturnType<typeof userEvent.setup>;
  /** Нажать нативную MainButton (как палец в Telegram). */
  pressMain(): Promise<void>;
  pressBack(): Promise<void>;
}

export function renderApp(opts: { server?: FakeServer; tg?: FakeTelegram | null } = {}): Rendered {
  const server = opts.server ?? new FakeServer();
  const tg = opts.tg === undefined ? fakeTelegram() : opts.tg;
  // Как в main.tsx: SDK кладёт объект в window.Telegram, приложение ищет его через findWebApp.
  if (tg) window.Telegram = { WebApp: tg.webApp };
  const tgApi = new TelegramApi(findWebApp(window));
  tgApi.start();
  const api = new Api({ initData: () => tgApi.initData, fetch: server.fetch });
  const user = userEvent.setup();
  // Человек нажимает кнопки не чаще раза в секунду: часы теста сдвигаются при каждом нажатии,
  // иначе подряд идущие нажатия гасит защита от двойного касания (DOUBLE_TAP_MS).
  const realNow = Date.now.bind(Date);
  let offset = 0;
  vi.spyOn(Date, "now").mockImplementation(() => realNow() + offset);
  render(<App tg={tgApi} api={api} />);
  return {
    server,
    tg,
    api,
    user,
    pressMain: async () => {
      offset += 1000;
      await act(async () => tg!.main.click());
    },
    pressBack: async () => {
      await act(async () => tg!.back.click());
    },
  };
}

export async function heading(name: string | RegExp) {
  return screen.findByRole("heading", { level: 1, name });
}
