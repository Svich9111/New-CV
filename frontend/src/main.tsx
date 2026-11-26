import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Api } from "./api/client";
import { App } from "./app/App";
import { TelegramApi, findWebApp, waitForTelegramSdk } from "./telegram/webapp";
import "./theme.css";
import "./app.css";

// SDK Telegram грузится асинхронно: если telegram.org недоступен, через SDK_WAIT_MS приложение всё равно
// отрисуется — экран «Открой в Telegram» и «Помощь» с номером 112 (ТЗ §9.3).
void waitForTelegramSdk().then(() => {
  const tg = new TelegramApi(findWebApp());
  tg.start(); // не бросает: ready() вызывается всегда, отказ SDK не мешает отрисовке
  const api = new Api({ initData: () => tg.initData });
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App tg={tg} api={api} />
    </StrictMode>,
  );
});
