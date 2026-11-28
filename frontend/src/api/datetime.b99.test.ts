/**
 * A4, Б-99 — одна строгая проверка `date-time` (RFC 3339) для памяти, чата и ленты пары: принятие и отказ в каждом
 * месте. `Date.parse` принимает «1», «2026», «Nov 5» — ни одно из мест их не принимает. Тексты выдуманные.
 */
import { isChatHistory, isChatMessage } from "./chat";
import { isCoupleFeed, isPartnerMessage } from "./couple";
import { isDateTime, isDay } from "./datetime";
import * as memory from "./memory";
import { isMemoryItem } from "./memory";

/** Вид, который отдают сервер (pydantic, aware datetime) и заглушка (`toISOString`). */
const GOOD = [
  "2026-11-20T10:00:00Z",
  "2026-11-20T10:00:00.000Z",
  "2026-11-26T19:45:43.123456Z",
  "2026-11-26T19:45:43.123456+00:00",
  "2026-11-01T12:00:00+03:00",
  new Date(Date.UTC(2026, 10, 20, 10, 0, 0)).toISOString(),
];
/** Понимает `Date.parse`, но не контракт; плюс явный мусор. */
const PARSEABLE_BAD = ["1", "2026", "Nov 5", "Nov 5, 2026 09:00:00 GMT", "2026-11-20", "2026-11-20T10:00:00", "2026-11-20T10:00"];
const BAD: unknown[] = [...PARSEABLE_BAD, "вчера", "", 1732096800000, null, undefined, {}];

test("предпосылка: Date.parse действительно принимает слабые значения (иначе тест ничего не проверяет)", () => {
  for (const v of ["1", "2026", "Nov 5"]) expect(Number.isNaN(Date.parse(v)), v).toBe(false);
});

test("общий модуль: память переэкспортирует те же функции, а не копию", () => {
  expect(memory.isDateTime).toBe(isDateTime);
  expect(memory.isDay).toBe(isDay);
});

describe("isChatMessage.created_at", () => {
  const owner = {
    id: "00000000-0000-4000-9000-000000000001",
    author: "owner",
    text: "Привет",
    created_at: "2026-11-20T10:00:00Z",
    client_message_id: null,
    reply_to: null,
    crisis: false,
  };
  const agent = { ...owner, id: "00000000-0000-4000-9000-000000000002", author: "agent", reply_to: owner.id };

  test.each(GOOD)("принимает %s", (created_at) => {
    expect(isChatMessage({ ...owner, created_at })).toBe(true);
    expect(isChatMessage({ ...agent, created_at })).toBe(true);
  });
  test.each(BAD.map((v) => [String(v), v]))("отвергает %s", (_n, created_at) => {
    expect(isChatMessage({ ...owner, created_at })).toBe(false);
    expect(isChatMessage({ ...agent, created_at })).toBe(false);
  });
  test("одна реплика со слабой датой отвергает всю страницу истории", () => {
    expect(isChatHistory({ items: [owner, agent], next_cursor: null })).toBe(true);
    expect(isChatHistory({ items: [owner, { ...agent, created_at: "Nov 5" }], next_cursor: null })).toBe(false);
  });
});

describe("isMemoryItem.created_at", () => {
  const item = {
    id: "30000000-0000-4000-8000-000000000001",
    type: "fact",
    text: "Любит чай",
    event_date: null,
    source: "owner_dialog",
    confidence: 0.9,
    created_at: "2026-11-20T10:00:00Z",
  };
  test.each(GOOD)("принимает %s", (created_at) => expect(isMemoryItem({ ...item, created_at })).toBe(true));
  test.each(BAD.map((v) => [String(v), v]))("отвергает %s", (_n, created_at) =>
    expect(isMemoryItem({ ...item, created_at })).toBe(false),
  );
});

describe("isPartnerMessage.sent_at / read_at (лента пары, 3.3–3.4)", () => {
  const msg = {
    id: "e0000000-0000-4000-8000-000000000001",
    direction: "outgoing",
    text: "Моё",
    is_adapted: false,
    harsh_tone_warning: false,
    status: "read",
    withheld_reason: null,
    withheld_crisis: null,
    reply_to_id: null,
    sent_at: "2026-11-25T19:00:00Z",
    read_at: "2026-11-25T19:05:00Z",
  };
  test.each(GOOD)("принимает %s", (v) => {
    expect(isPartnerMessage({ ...msg, sent_at: v })).toBe(true);
    expect(isPartnerMessage({ ...msg, read_at: v })).toBe(true);
  });
  test("read_at: null и нет поля — допустимы", () => {
    expect(isPartnerMessage({ ...msg, read_at: null })).toBe(true);
    const noRead: Record<string, unknown> = { ...msg };
    delete noRead.read_at;
    expect(isPartnerMessage(noRead)).toBe(true);
  });
  test.each(BAD.map((v) => [String(v), v]))("sent_at отвергает %s", (_n, v) => {
    expect(isPartnerMessage({ ...msg, sent_at: v })).toBe(false);
  });
  test.each(PARSEABLE_BAD.concat("вчера", ""))("read_at отвергает %s", (v) => {
    expect(isPartnerMessage({ ...msg, read_at: v })).toBe(false);
  });
  test("одно сообщение со слабой датой отвергает всю страницу ленты", () => {
    expect(isCoupleFeed({ items: [msg], next_cursor: null })).toBe(true);
    expect(isCoupleFeed({ items: [{ ...msg, sent_at: "2026" }], next_cursor: null })).toBe(false);
  });
});
