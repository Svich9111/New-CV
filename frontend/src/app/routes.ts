/** Маршруты Mini App. Стек экранов связан с кнопкой «Назад» Telegram: назад — снять верхний. */
export type Route =
  | { name: "welcome"; slide: number }
  | { name: "onboarding" }
  | { name: "invite" }
  | { name: "incoming"; token: string }
  | { name: "paired" }
  | { name: "chat" }
  | { name: "memory" }
  | { name: "underage" }
  | { name: "help" };

export const WELCOME_SLIDES = 3;

/** Токен из `startapp`: 32 случайных байта в base64url (контракт `InviteToken`). */
export const INVITE_TOKEN = /^[A-Za-z0-9_-]{32,64}$/;
