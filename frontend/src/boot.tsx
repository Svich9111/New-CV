import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Api } from "./api/client";
import { App, type SdkWait } from "./app/App";
import {
  SDK_GIVE_UP_MS,
  SDK_WAIT_MS,
  TelegramApi,
  type SdkSettled,
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
  sdkGiveUpMs?: number;
  /** Часы для срока «Загружаем…» (мс); по умолчанию `performance.now()`. */
  now?: () => number;
}

/**
 * Запуск Mini App. SDK Telegram грузится асинхронно: если telegram.org недоступен, через SDK_WAIT_MS
 * приложение всё равно отрисуется — «Открой в Telegram» и «Помощь» с номером 112 (ТЗ §9.3).
 * Внутри Telegram (в адресе есть параметры запуска) вместо ложного «Открой в Telegram» показывается
 * «Загружаем…» с «Помощью»; когда скрипт SDK всё же загрузится, приложение запускается заново уже с SDK (n-2).
 * Экран выбирается по самому событию скрипта (`load`/`error`), а не только по отметке sdk-state.js, и
 * ожидание ограничено SDK_GIVE_UP_MS: без sdk-state.js человек не ждёт бесконечно (n-7).
 * Открытая «Помощь» переживает перезапуск: после входа она остаётся на месте, «Назад» ведёт на первый экран.
 *
 * Б-93: внутри Telegram экран ожидания с «Помощью» рисуется сразу, без SDK_WAIT_MS с голой заглушкой, а срок
 * SDK_GIVE_UP_MS считается от запуска страницы (вызова `boot`), а не после SDK_WAIT_MS. В обычном браузере
 * (параметров запуска нет) приложение по-прежнему ждёт SDK до SDK_WAIT_MS — заглушка в index.html со 112.
 */
export async function boot({
  container,
  win = window,
  fetch,
  sdkWaitMs = SDK_WAIT_MS,
  sdkGiveUpMs = SDK_GIVE_UP_MS,
  now = () => win.performance.now(),
}: BootOptions): Promise<() => void> {
  const openedAt = now();
  // Внутри Telegram ждать SDK молча незачем: экран ожидания с «Помощью» лучше голой заглушки.
  const insideButNoSdk = () => !win.Telegram?.WebApp && launchedFromTelegram(win) && sdkScriptState(win) === "pending";
  if (!insideButNoSdk()) await waitForTelegramSdk(win, sdkWaitMs);
  const root = createRoot(container);
  let stop: () => void = () => undefined;
  let generation = 0;
  // «Помощь» открыта на экране без SDK: при перезапуске приложение откроется на ней же (ТЗ §9.3).
  let helpOpen = false;
  const onHelp = (open: boolean) => {
    helpOpen = open;
  };

  /** `settled` — загрузка скрипта уже закончилась (событие или срок): «грузится» больше не показываем. */
  const mount = (settled?: SdkSettled | "timeout"): SdkWait | undefined => {
    stop();
    const tg = new TelegramApi(findWebApp(win));
    stop = tg.start(); // не бросает: ready() вызывается всегда, отказ SDK не мешает отрисовке
    const api = new Api({ initData: () => tg.initData, fetch });
    let sdk: SdkWait | undefined;
    if (!tg.inside && !win.Telegram?.WebApp && launchedFromTelegram(win)) {
      // Скрипт ещё грузится — ждём; упал или загрузился без Telegram.WebApp — сказать об этом прямо.
      sdk = !settled && sdkScriptState(win) === "pending" ? "waiting" : "failed";
    }
    generation += 1;
    root.render(
      <StrictMode>
        <App key={generation} tg={tg} api={api} sdk={sdk} help={helpOpen} onHelp={onHelp} />
      </StrictMode>,
    );
    return sdk;
  };

  let offLate: () => void = () => undefined;
  let giveUp: ReturnType<typeof setTimeout> | undefined;
  if (mount() === "waiting") {
    offLate = onSdkScriptSettled(win, (event) => {
      clearTimeout(giveUp);
      mount(event);
    });
    // Слушатель события остаётся и после срока: поздний `load` всё равно запустит приложение с SDK.
    // Срок — от запуска страницы (Б-93), а не от конца ожидания.
    giveUp = setTimeout(() => mount("timeout"), Math.max(0, sdkGiveUpMs - (now() - openedAt)));
  }
  return () => {
    clearTimeout(giveUp);
    offLate();
    stop();
    root.unmount();
  };
}
