import { createContext, useContext, useEffect, useState } from "react";
import { TelegramApi } from "./webapp";

export const TelegramContext = createContext<TelegramApi>(new TelegramApi(null));

export function useTelegram(): TelegramApi {
  return useContext(TelegramContext);
}

/** Номер смены темы Telegram: меняется на каждое `themeChanged` — повод перечитать токены цвета. */
export function useThemeVersion(): number {
  const tg = useTelegram();
  const [version, setVersion] = useState(0);
  useEffect(() => tg.onThemeChange(() => setVersion((v) => v + 1)), [tg]);
  return version;
}
