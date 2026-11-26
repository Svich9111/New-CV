/**
 * Типы API — из сгенерированного `schema.d.ts` (openapi-typescript по `api/openapi.yaml`).
 * Сгенерированный файл не правится руками: `npm run gen:api`; свежесть проверяет `npm run lint`.
 */
import type { components, paths } from "./schema";

type Schemas = components["schemas"];
type Json<R> = R extends { content: { "application/json": infer B } } ? B : never;

export type Me = Schemas["Me"];
export type Session = Schemas["Session"];
export type OnboardingRequest = Schemas["OnboardingRequest"];
export type Couple = Schemas["Couple"];
export type InviteCreated = Schemas["InviteCreated"];
export type HelpContact = Schemas["HelpContact"];
export type ApiErrorBody = Schemas["Error"];
export type InvitePreview = Json<paths["/invites/{token}"]["get"]["responses"][200]>;
export type AuthRequest = NonNullable<
  paths["/auth/telegram"]["post"]["requestBody"]
>["content"]["application/json"];

export type ChatMessage = Schemas["ChatMessage"];
export type UnavailableText = Schemas["UnavailableText"];
export type ChatHistory = Json<paths["/chat/messages"]["get"]["responses"][200]>;
export type ChatSendRequest = NonNullable<
  paths["/chat/messages"]["post"]["requestBody"]
>["content"]["application/json"];
export type ChatStreamAccepted = Schemas["ChatStreamAccepted"];
export type ChatStreamDelta = Schemas["ChatStreamDelta"];
export type ChatStreamDone = Schemas["ChatStreamDone"];
export type ChatStreamCrisis = Schemas["ChatStreamCrisis"];
export type ChatStreamError = Schemas["ChatStreamError"];
export type ChatErrorCode = Schemas["ChatErrorCode"];
