import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Api } from "./api/client";
import { App, type SdkWait } from "./app/App";
import {
  SDK_WAIT_MS,
  TelegramApi,
  findWebApp,
  launchedFromTelegram,
  onSdkScriptSettled,
  sdkScriptState,
  waitForTelegramSdk,
} from "./telegram/webapp";

export interface BootOptions {
  container: HTMLElement;
  win?: Window;
  fetch?: typeof fetch;
  sdkWaitMs?: number;
}

/**
 * Запуск Mini App. SDK Telegram грузится асинхронно: если telegram.org недоступен, через SDK_WAIT_MS
 * приложение всё равно отрисуется — «Открой в Telegram» и «Помощь» с номером 112 (ТЗ §9.3).
 * Внутри Telegram (в адресе есть параметры запуска) вместо ложного «Открой в Telegram» показывается
 * «Загружаем…» с «Помощью»; когда скрипт SDK всё же загрузится, приложение запускается заново уже с SDK (n-2).
 */
export async function boot({ container, win = window, fetch, sdkWaitMs = SDK_WAIT_MS }: BootOptions): Promise<() => void> {
  await waitForTelegramSdk(win, sdkWaitMs);
  const root = createRoot(container);
  let stop: () => void = () => undefined;
  let generation = 0;

  const mount = (): SdkWait | undefined => {
    stop();
    const tg = new TelegramApi(findWebApp(win));
    stop = tg.start(); // не бросает: ready() вызывается всегда, отказ SDK не мешает отрисовке
    const api = new Api({ initData: () => tg.initData, fetch });
    let sdk: SdkWait | undefined;
    if (!tg.inside && !win.Telegram?.WebApp && launchedFromTelegram(win)) {
      // Скрипт ещё грузится — ждём; упал или загрузился без Telegram.WebApp — сказать об этом прямо.
      sdk = sdkScriptState(win) === "pending" ? "waiting" : "failed";
    }
    generation += 1;
    root.render(
      <StrictMode>
        <App key={generation} tg={tg} api={api} sdk={sdk} />
      </StrictMode>,
    );
    return sdk;
  };

  const offLate = mount() === "waiting" ? onSdkScriptSettled(win, () => void mount()) : () => undefined;
  return () => {
    offLate();
    stop();
    root.unmount();
  };
}
