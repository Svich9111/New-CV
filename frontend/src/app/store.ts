import { createContext, useContext } from "react";
import type { Api } from "../api/client";
import type { InviteCreated, Me } from "../api/types";
import type { ChatSession } from "../chat/session";
import type { CoupleSession } from "../couple/session";
import type { Route } from "./routes";

/** Приглашение, которое человек нажал «Принять» до онбординга: после онбординга оно принимается. */
export interface PendingAccept {
  token: string;
  inviterName: string | null;
  /**
   * Онбординг уже сохранён, а принять приглашение не удалось: причина и помогает ли повтор. Хранится в
   * сессии, а не в экране: после «Помощь» → «Назад» онбординг не отправляется второй раз (n-1).
   */
  failure?: { text: string; retry: boolean };
}

/** Введённое на онбординге: живёт в памяти сессии, чтобы уход в «Помощь» и «Назад» его не стирали. */
export interface OnboardingDraft {
  name: string;
  addressForm: string;
  partnerName: string;
  checks: Record<"consent_terms" | "consent_data" | "consent_sensitive" | "age_confirmed", boolean>;
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
  onboardingDraft: OnboardingDraft | null;
  /** Личный чат с агентом — на всю сессию: уход в «Помощь» и «Назад» не обрывают ответ и не стирают ввод. */
  chat: ChatSession;
  /** Черновик, контекст ответа, оригиналы и отметки «прочитано» — на всю сессию, только в памяти (задача 3.4). */
  couple: CoupleSession;
  setOnboardingDraft(d: OnboardingDraft | null): void;
  push(route: Route): void;
  /** Заменить весь стек: после шага, к которому возвращаться нельзя (онбординг пройден, пара создана). */
  reset(route: Route): void;
  /** Заменить верхний экран: «Назад» после него ведёт туда же, куда вёл бы с заменённого (черновик → лента). */
  replace(route: Route): void;
  back(): void;
  canGoBack: boolean;
}

export const AppContext = createContext<AppState | null>(null);

export function useApp(): AppState {
  const s = useContext(AppContext);
  if (!s) throw new Error("AppContext is missing");
  return s;
}
