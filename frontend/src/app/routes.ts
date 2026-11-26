/** Маршруты Mini App. Стек экранов связан с кнопкой «Назад» Telegram: назад — снять верхний. */
export type Route =
  | { name: "welcome"; slide: number }
  | { name: "onboarding" }
  | { name: "invite" }
  | { name: "incoming"; token: string }
  | { name: "paired" }
  | { name: "chat" }
  | { name: "memory" }
  /** Черновик сообщения партнёру; `replyTo` — id входящего, на которое пишется ответ (АР-56, Б-30). */
  | { name: "draft"; replyTo: string | null }
  | { name: "messages" }
  | { name: "underage" }
  | { name: "help" };

export const WELCOME_SLIDES = 3;

/** Токен из `startapp`: 32 случайных байта в base64url (контракт `InviteToken`). */
export const INVITE_TOKEN = /^[A-Za-z0-9_-]{32,64}$/;
