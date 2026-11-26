import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Api } from "./api/client";
import { App } from "./app/App";
import { TelegramApi, findWebApp } from "./telegram/webapp";
import "./theme.css";
import "./app.css";

const tg = new TelegramApi(findWebApp());
tg.start();
const api = new Api({ initData: () => tg.initData });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App tg={tg} api={api} />
  </StrictMode>,
);
