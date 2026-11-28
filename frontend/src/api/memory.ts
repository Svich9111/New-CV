/**
 * «Что агент помнит» по контракту `api/openapi.yaml` (`GET /memory`, `MemoryItem`, ФТ-13): строгая проверка вида
 * ответа. Ответ не того вида целиком отвергается — экран не показывает ни одной записи из него.
 * Тексты записей здесь только проверяются — никуда не пишутся и не журналируются.
 */
import { isExportText } from "./chat";
import { isDateTime, isDay } from "./datetime";
import type { MemoryItem, MemoryList, MemoryType } from "./types";

/** Проверки дат вынесены в `./datetime` (Б-99); реэкспорт — для прежних импортов. */
export { isDateTime, isDay };

/** Шесть типов записей в порядке таблицы ТЗ §6.3. */
export const MEMORY_TYPES: readonly MemoryType[] = ["fact", "date", "preference", "sensitive_topic", "observation", "from_partner"];

const SOURCES: readonly MemoryItem["source"][] = ["owner_dialog", "agent_inference", "partner_message"];

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/**
 * `MemoryItem`: id — uuid; тип и источник из перечней; текст — строка или `UnavailableText`; дата события —
 * нет поля, `null` или календарная дата; уверенность — конечное число 0–1; время создания — дата-время RFC 3339.
 */
export function isMemoryItem(v: unknown): v is MemoryItem {
  return (
    isObject(v) &&
    typeof v.id === "string" &&
    UUID.test(v.id) &&
    typeof v.type === "string" &&
    (MEMORY_TYPES as readonly string[]).includes(v.type) &&
    isExportText(v.text) &&
    (v.event_date === undefined || v.event_date === null || isDay(v.event_date)) &&
    typeof v.source === "string" &&
    (SOURCES as readonly string[]).includes(v.source) &&
    typeof v.confidence === "number" &&
    Number.isFinite(v.confidence) &&
    v.confidence >= 0 &&
    v.confidence <= 1 &&
    isDateTime(v.created_at)
  );
}

/** Ответ `GET /memory`: `items` — массив `MemoryItem` без повторов id (повтор — ответ не по контракту). */
export function isMemoryList(v: unknown): v is MemoryList {
  if (!isObject(v) || !Array.isArray(v.items) || !v.items.every(isMemoryItem)) return false;
  const ids = new Set(v.items.map((i) => i.id.toLowerCase()));
  return ids.size === v.items.length;
}
