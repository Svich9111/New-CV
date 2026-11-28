/**
 * Строгие проверки дат по форматам контракта `api/openapi.yaml` (`date`, `date-time`) — общие для памяти (2.5),
 * чата (2.2) и ленты/черновиков пары (3.3–3.4), Б-99. `Date.parse` здесь не годится сам по себе: он принимает
 * «1», «2026», «Nov 5» (A8-3).
 */

const DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

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
