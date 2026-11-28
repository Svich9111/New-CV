/**
 * Черновики (`/drafts*`, задача 3.1) и лента пары (`/couple/messages*`, задача 3.3) по контракту `api/openapi.yaml`:
 * строгие проверки вида ответов. Ответ не того вида целиком отвергается (`bad_response`) — экран не показывает
 * из него ничего. Исключение одно — кризисный протокол (`kind: crisis`): помощь показывается при любом его виде
 * (ТЗ §9.3), как событие `crisis` чата (`parseEvent`).
 * Тексты здесь только проверяются — никуда не пишутся и не журналируются.
 */
import { isExportText } from "./chat";
import { isDateTime } from "./datetime";
import type { CoupleFeed, Draft, DraftCrisis, HelpContact, MessageOriginal, PartnerMessage } from "./types";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isUuid = (v: unknown): v is string => typeof v === "string" && UUID.test(v);
/** Необязательное поле `uuid | null`: нет поля, `null` или uuid. */
const optUuidOrNull = (v: unknown) => v === undefined || v === null || isUuid(v);

/** Предел текста черновика и сообщения партнёру (контракт `PATCH /drafts/{id}`: 1–4000). */
export const DRAFT_MAX = 4000;
/** Предел пожелания к «другому варианту» (контракт `regenerate.wish`). */
export const WISH_MAX = 500;

/**
 * Длина в символах Unicode (кодовых точках), как считает сервер (`max_length` у строки Python) и JSON Schema, а не в
 * единицах UTF-16 (`String.length`): эмодзи — один символ, не два (ревью 3.4 MINOR-2, A8-3.4-3).
 */
export const charCount = (s: string): number => [...s].length;

/** Обрезать до `max` символов Unicode — не посреди суррогатной пары. */
export function clipChars(s: string, max: number): string {
  if (s.length <= max) return s; // единиц UTF-16 не больше предела — символов тем более
  const cps = [...s];
  return cps.length <= max ? s : cps.slice(0, max).join("");
}

export const DRAFT_STATUSES: readonly Draft["status"][] = ["draft", "approved", "rejected", "blocked", "sent"];
export const MESSAGE_STATUSES: readonly PartnerMessage["status"][] = ["processing", "withheld", "delivered", "read", "answered"];
/** Получателю видны только доставленные (контракт `PartnerMessage.status`). */
const INCOMING_STATUSES: readonly PartnerMessage["status"][] = ["delivered", "read", "answered"];
export const WITHHELD_REASONS = ["safety", "model_refusal", "pipeline_error", "attempts_exhausted", "couple_ended"] as const;
export const WITHHELD_CRISES = ["violence", "threat_outgoing", "self_harm", "life_threat"] as const;

/** `Draft`: `kind: draft`, id — uuid, текст — строка, версия — целое ≥ 1, статус из перечня, ответ — uuid или null. */
export function isDraft(v: unknown): v is Draft {
  return (
    isObject(v) &&
    v.kind === "draft" &&
    isUuid(v.id) &&
    typeof v.text === "string" &&
    typeof v.version === "number" &&
    Number.isInteger(v.version) &&
    v.version >= 1 &&
    typeof v.status === "string" &&
    (DRAFT_STATUSES as readonly string[]).includes(v.status) &&
    optUuidOrNull(v.reply_to_id)
  );
}

const CRISIS_KEYS = new Set(["kind", "text", "draft_id", "contacts"]);

/**
 * `DraftCrisis` строго по контракту: `kind: crisis`, непустой текст, контакты — массив из ≥ 1 записи, `draft_id` — uuid,
 * null или нет поля; других полей нет (`additionalProperties: false`). Экран кризис показывает и при нарушении
 * (см. `parseDraftOutcome`) — эта проверка для тестов контракта и сверки заглушки.
 */
export function isDraftCrisis(v: unknown): v is DraftCrisis {
  return (
    isObject(v) &&
    Object.keys(v).every((k) => CRISIS_KEYS.has(k)) &&
    v.kind === "crisis" &&
    typeof v.text === "string" &&
    v.text.length > 0 &&
    Array.isArray(v.contacts) &&
    v.contacts.length >= 1 &&
    optUuidOrNull(v.draft_id)
  );
}

/** Кризисный протокол для экрана: текст (`null` — пришёл без текста) и контакты (неверные отброшены). */
export interface CrisisView {
  kind: "crisis";
  text: string | null;
  contacts: HelpContact[];
  /** «Другой вариант»: прежний черновик, не изменённый сервером. */
  draftId: string | null;
}

export type DraftResult = { kind: "draft"; draft: Draft } | CrisisView;

/**
 * Итог `POST /drafts` и `regenerate` (`DraftOutcome`, дискриминатор `kind`). `draft` — только строго по схеме;
 * `crisis` — при любом виде тела: помощь показывается всегда, ничего не смягчается (ТЗ §9.3). `null` — ни то ни другое.
 */
export function parseDraftOutcome(v: unknown, isContact: (c: unknown) => c is HelpContact): DraftResult | null {
  if (isDraft(v)) return { kind: "draft", draft: v };
  if (isObject(v) && v.kind === "crisis") {
    return {
      kind: "crisis",
      text: typeof v.text === "string" && v.text.trim().length > 0 ? v.text : null,
      contacts: Array.isArray(v.contacts) ? v.contacts.filter(isContact) : [],
      draftId: isUuid(v.draft_id) ? v.draft_id : null,
    };
  }
  return null;
}

/**
 * `PartnerMessage` по контракту и правилам видимости (АР-11, АР-44, АР-45):
 * - id — uuid; направление и статус из перечней; текст — строка или `UnavailableText`; `is_adapted` — булево;
 *   `harsh_tone_warning` — булево или нет поля; `sent_at` — дата-время; `read_at` — дата-время, null или нет поля;
 * - полученное — только `delivered`/`read`/`answered`, без причины недоставки и типа кризиса;
 * - `withheld_reason` — только при `withheld` (иначе null или нет поля), из перечня;
 * - `withheld_crisis` — только при `withheld_reason: safety`, из перечня или null.
 */
export function isPartnerMessage(v: unknown): v is PartnerMessage {
  if (
    !(
      isObject(v) &&
      isUuid(v.id) &&
      (v.direction === "outgoing" || v.direction === "incoming") &&
      isExportText(v.text) &&
      typeof v.is_adapted === "boolean" &&
      (v.harsh_tone_warning === undefined || typeof v.harsh_tone_warning === "boolean") &&
      typeof v.status === "string" &&
      (MESSAGE_STATUSES as readonly string[]).includes(v.status) &&
      optUuidOrNull(v.reply_to_id) &&
      isDateTime(v.sent_at) &&
      (v.read_at === undefined || v.read_at === null || isDateTime(v.read_at))
    )
  ) {
    return false;
  }
  const reason = v.withheld_reason ?? null;
  const crisis = v.withheld_crisis ?? null;
  if (reason !== null && !(WITHHELD_REASONS as readonly unknown[]).includes(reason)) return false;
  if (crisis !== null && !(WITHHELD_CRISES as readonly unknown[]).includes(crisis)) return false;
  if (reason !== null && v.status !== "withheld") return false;
  if (crisis !== null && reason !== "safety") return false;
  if (v.direction === "incoming" && !(INCOMING_STATUSES as readonly unknown[]).includes(v.status)) return false;
  return true;
}

/** Страница ленты: `items` — `PartnerMessage` без повторов id, `next_cursor` — непустая строка, null или нет поля. */
export function isCoupleFeed(v: unknown): v is CoupleFeed {
  if (!isObject(v) || !Array.isArray(v.items) || !v.items.every(isPartnerMessage)) return false;
  if (!(v.next_cursor === undefined || v.next_cursor === null || (typeof v.next_cursor === "string" && v.next_cursor.length > 0)))
    return false;
  const ids = new Set(v.items.map((m) => m.id.toLowerCase()));
  return ids.size === v.items.length;
}

/** Ответ `/original`: `{ text: ExportText }`. */
export function isMessageOriginal(v: unknown): v is MessageOriginal {
  return isObject(v) && isExportText(v.text);
}
