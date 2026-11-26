import { createContext, useContext } from "react";
import { TelegramApi } from "./webapp";

export const TelegramContext = createContext<TelegramApi>(new TelegramApi(null));

export function useTelegram(): TelegramApi {
  return useContext(TelegramContext);
}
