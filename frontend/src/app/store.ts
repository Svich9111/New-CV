import { createContext, useContext } from "react";
import type { Api } from "../api/client";
import type { InviteCreated, Me } from "../api/types";
import type { Route } from "./routes";

/** Приглашение, которое человек нажал «Принять» до онбординга: после онбординга оно принимается. */
export interface PendingAccept {
  token: string;
  inviterName: string | null;
}

export interface AppState {
  api: Api;
  me: Me;
  setMe(me: Me): void;
  /** Последняя выданная ссылка — только в памяти: сервер хранит лишь хеш и повторно её не отдаёт. */
  invite: InviteCreated | null;
  setInvite(invite: InviteCreated | null): void;
  /** Ссылку уже открывали в выборе чата Telegram (отправлена ли она — клиенту не узнать). */
  shared: boolean;
  setShared(shared: boolean): void;
  pendingAccept: PendingAccept | null;
  setPendingAccept(p: PendingAccept | null): void;
  /** Ошибка выпуска ссылки сразу после онбординга — показывается на экране приглашения. */
  inviteError: string | null;
  setInviteError(e: string | null): void;
  push(route: Route): void;
  /** Заменить весь стек: после шага, к которому возвращаться нельзя (онбординг пройден, пара создана). */
  reset(route: Route): void;
  back(): void;
  canGoBack: boolean;
}

export const AppContext = createContext<AppState | null>(null);

export function useApp(): AppState {
  const s = useContext(AppContext);
  if (!s) throw new Error("AppContext is missing");
  return s;
}
