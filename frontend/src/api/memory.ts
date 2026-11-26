/**
 * «Что агент помнит» по контракту `api/openapi.yaml` (`GET /memory`, `MemoryItem`, ФТ-13): строгая проверка вида
 * ответа. Ответ не того вида целиком отвергается — экран не показывает ни одной записи из него.
 * Тексты записей здесь только проверяются — никуда не пишутся и не журналируются.
 */
import { isExportText } from "./chat";
import type { MemoryItem, MemoryList, MemoryType } from "./types";

/** Шесть типов записей в порядке таблицы ТЗ §6.3. */
export const MEMORY_TYPES: readonly MemoryType[] = ["fact", "date", "preference", "sensitive_topic", "observation", "from_partner"];

const SOURCES: readonly MemoryItem["source"][] = ["owner_dialog", "agent_inference", "partner_message"];

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** Календарная дата `YYYY-MM-DD` (формат `date` контракта): существующий день, без времени и пояса. */
export function isDay(v: unknown): v is string {
  if (typeof v !== "string") return false;
  const m = DAY.exec(v);
  if (!m) return false;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  // `Date.UTC` годы 0–99 толкует как 1900–1999 (A8-1): год задаётся отдельно.
  const dt = new Date(0);
  dt.setUTCFullYear(y, mo - 1, d);
  return y >= 1 && dt.getUTCFullYear() === y && dt.getUTCMonth() === mo - 1 && dt.getUTCDate() === d;
}

const DATE_TIME = /^(\d{4}-\d{2}-\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-](\d{2}):(\d{2}))$/;

/**
 * Дата-время RFC 3339 (формат `date-time` контракта): существующий день, время 00:00:00–23:59:60, обязательный пояс
 * (`Z` или `±hh:mm`). `Date.parse` принимает и «1», и «Nov 5» (A8-3) — поэтому сначала строгий вид.
 */
export function isDateTime(v: unknown): v is string {
  if (typeof v !== "string") return false;
  const m = DATE_TIME.exec(v);
  if (!m || !isDay(m[1])) return false;
  const [h, mi, se] = [Number(m[2]), Number(m[3]), Number(m[4])];
  if (h > 23 || mi > 59 || se > 60) return false;
  if (m[7] !== undefined && (Number(m[7]) > 23 || Number(m[8]) > 59)) return false;
  return !Number.isNaN(Date.parse(v.replace(/:60(?=[.Zz+-])/, ":59")));
}

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
