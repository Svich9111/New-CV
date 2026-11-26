/**
 * A4, задача 2.5 — память по контракту: строгая проверка `MemoryItem` (граничные корректные и неверные значения),
 * `Api.memoryList` и `Api.memoryDelete` на заглушке сервера.
 */
import { Api, ApiError } from "./client";
import { MEMORY_TYPES, isDay, isMemoryItem, isMemoryList } from "./memory";
import { FakeServer } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";

const ok = {
  id: "30000000-0000-4000-8000-000000000001",
  type: "fact",
  text: "Работает посменно",
  event_date: null,
  source: "owner_dialog",
  confidence: 0.7,
  created_at: "2026-11-01T09:00:00Z",
};

describe("isMemoryItem — корректные", () => {
  test.each([
    ["базовая запись", {}],
    ["без event_date", { event_date: undefined }],
    ["event_date — дата", { type: "date", event_date: "2027-06-14" }],
    ["29 февраля високосного года", { event_date: "2028-02-29" }],
    ["уверенность 0", { confidence: 0 }],
    ["уверенность 1", { confidence: 1 }],
    ["UnavailableText", { text: { unavailable: true, reason: "auth" } }],
    ["пустой текст — строка", { text: "" }],
    ["id в верхнем регистре", { id: "3A000000-0000-4000-8000-0000000000AB" }],
    ["вывод агента", { type: "observation", source: "agent_inference" }],
    ["из сообщений партнёра", { type: "from_partner", source: "partner_message" }],
    ["created_at со смещением", { created_at: "2026-11-01T12:00:00+03:00" }],
  ])("%s", (_name, patch) => {
    const v: Record<string, unknown> = { ...ok, ...patch };
    for (const k of Object.keys(v)) if (v[k] === undefined) delete v[k];
    expect(isMemoryItem(v)).toBe(true);
  });

  test("все шесть типов ТЗ §6.3 в порядке таблицы", () => {
    expect(MEMORY_TYPES).toEqual(["fact", "date", "preference", "sensitive_topic", "observation", "from_partner"]);
    for (const type of MEMORY_TYPES) expect(isMemoryItem({ ...ok, type })).toBe(true);
  });
});

describe("isMemoryItem — неверные", () => {
  test.each([
    ["не объект", null],
    ["массив", [ok]],
    ["нет id", { ...ok, id: undefined }],
    ["id не uuid", { ...ok, id: "../couple" }],
    ["id пустой", { ...ok, id: "" }],
    ["неизвестный тип", { ...ok, type: "secret" }],
    ["тип числом", { ...ok, type: 1 }],
    ["нет текста", { ...ok, text: undefined }],
    ["текст числом", { ...ok, text: 42 }],
    ["UnavailableText без причины", { ...ok, text: { unavailable: true } }],
    ["UnavailableText с неизвестной причиной", { ...ok, text: { unavailable: true, reason: "other" } }],
    ["UnavailableText unavailable=false", { ...ok, text: { unavailable: false, reason: "auth" } }],
    ["неизвестный источник", { ...ok, source: "partner_dialog" }],
    ["нет источника", { ...ok, source: undefined }],
    ["уверенность > 1", { ...ok, confidence: 1.0001 }],
    ["уверенность < 0", { ...ok, confidence: -0.1 }],
    ["уверенность NaN", { ...ok, confidence: Number.NaN }],
    ["уверенность строкой", { ...ok, confidence: "0.5" }],
    ["нет уверенности", { ...ok, confidence: undefined }],
    ["event_date с временем", { ...ok, event_date: "2027-06-14T00:00:00Z" }],
    ["event_date несуществующий день", { ...ok, event_date: "2027-02-30" }],
    ["event_date 29 февраля невисокосного", { ...ok, event_date: "2027-02-29" }],
    ["event_date числом", { ...ok, event_date: 20270614 }],
    ["created_at не дата", { ...ok, created_at: "вчера" }],
    ["нет created_at", { ...ok, created_at: undefined }],
  ])("%s", (_name, v) => {
    expect(isMemoryItem(v)).toBe(false);
  });

  test("isDay: только YYYY-MM-DD существующего дня", () => {
    expect(isDay("2026-12-31")).toBe(true);
    for (const v of ["2026-13-01", "2026-00-10", "2026-1-01", "26-01-01", "", null]) expect(isDay(v)).toBe(false);
  });
});

describe("isMemoryList", () => {
  test("пустой список и список записей", () => {
    expect(isMemoryList({ items: [] })).toBe(true);
    expect(isMemoryList({ items: [ok, { ...ok, id: "30000000-0000-4000-8000-000000000002" }] })).toBe(true);
  });

  test("одна неверная запись отвергает весь ответ; нет items; items не массив; повтор id", () => {
    expect(isMemoryList({ items: [ok, { ...ok, id: "30000000-0000-4000-8000-000000000002", confidence: 2 }] })).toBe(false);
    expect(isMemoryList({})).toBe(false);
    expect(isMemoryList({ items: {} })).toBe(false);
    expect(isMemoryList([ok])).toBe(false);
    expect(isMemoryList({ items: [ok, { ...ok }] })).toBe(false);
  });
});

async function api(server = new FakeServer().onboarded()) {
  const a = new Api({ initData: () => INIT_DATA, fetch: server.fetch });
  await a.login();
  return { a, server };
}

async function err(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p;
  } catch (e) {
    expect(e).toBeInstanceOf(ApiError);
    return e as ApiError;
  }
  throw new Error("ожидалась ошибка");
}

describe("Api.memoryList", () => {
  test("GET /memory без параметров и тела, с токеном; записи всех типов", async () => {
    const { a, server } = await api(new FakeServer().onboarded().seedMemory());
    const items = await a.memoryList();
    expect(items.map((i) => i.type)).toEqual(["fact", "date", "preference", "sensitive_topic", "observation", "from_partner", "fact"]);
    const [call] = server.calls("GET", "/memory");
    expect(call).toMatchObject({ query: "", body: undefined });
    expect(call.auth).toMatch(/^Bearer /);
  });

  test("ответ не по схеме — bad_response «Память пришла с ошибкой»", async () => {
    const { a, server } = await api();
    server.inject("GET", "/memory", 200, { items: [{ ...ok, source: "hacker" }] });
    const e = await err(a.memoryList());
    expect(e.code).toBe("bad_response");
    expect(e.message).toMatch(/^Память пришла с ошибкой/);
  });

  test("204 вместо списка — bad_response", async () => {
    const { a, server } = await api();
    server.inject("GET", "/memory", 204);
    expect((await err(a.memoryList())).code).toBe("bad_response");
  });

  test("401 — один повторный вход и один повтор", async () => {
    const { a, server } = await api(new FakeServer().onboarded().seedMemory());
    server.expireSession();
    expect(await a.memoryList()).toHaveLength(7);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
    expect(server.calls("GET", "/memory")).toHaveLength(2);
  });

  test("заглушка: ?type фильтрует, неверный type — 422", async () => {
    const { server } = await api(new FakeServer().onboarded().seedMemory());
    const res = await server.fetch("/api/v1/memory?type=observation", { headers: { Authorization: `Bearer ${server.token}` } });
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    expect(((await res.json()) as { items: { type: string }[] }).items.map((i) => i.type)).toEqual(["observation"]);
    const bad = await server.fetch("/api/v1/memory?type=secret", { headers: { Authorization: `Bearer ${server.token}` } });
    expect(bad.status).toBe(422);
  });
});

describe("Api.memoryDelete", () => {
  test("204 — deleted; DELETE /memory/{id} без тела; повтор — 404 → gone; повторный GET записи не возвращает", async () => {
    const { a, server } = await api(new FakeServer().onboarded().seedMemory());
    const id = server.memory[0].id;
    expect(await a.memoryDelete(id)).toBe("deleted");
    const [call] = server.calls("DELETE", `/memory/${id}`);
    expect(call).toMatchObject({ query: "", body: undefined });
    expect(await a.memoryDelete(id)).toBe("gone");
    expect((await a.memoryList()).map((i) => i.id)).not.toContain(id);
  });

  test("чужая и несуществующая запись — 404 → gone", async () => {
    const { a, server } = await api(new FakeServer().onboarded().seedMemory());
    const [partnerId] = [...server.partnerMemory];
    expect(await a.memoryDelete(partnerId)).toBe("gone");
    expect(await a.memoryDelete("30000000-0000-4000-8000-00000000dead")).toBe("gone");
  });

  test("id кодируется в пути", async () => {
    const { a, server } = await api();
    await a.memoryDelete("a/b?c");
    expect(server.log.at(-1)!.path).toBe("/memory/a%2Fb%3Fc");
  });

  test("409 один раз — автоповтор, deleted; дважды — ApiError conflict_retry с текстом удаления", async () => {
    const { a, server } = await api(new FakeServer().onboarded().seedMemory());
    const [x, y] = server.memory.map((m) => m.id);
    server.inject("DELETE", `/memory/${x}`, 409, { code: "conflict_retry", message: "x" });
    expect(await a.memoryDelete(x)).toBe("deleted");
    expect(server.calls("DELETE", `/memory/${x}`)).toHaveLength(2);
    server.inject("DELETE", `/memory/${y}`, 409, { code: "conflict_retry", message: "x" }, undefined, 2);
    const e = await err(a.memoryDelete(y));
    expect(e.code).toBe("conflict_retry");
    expect(e.message).toBe("Не получилось удалить, попробуй ещё раз.");
    expect(server.calls("DELETE", `/memory/${y}`)).toHaveLength(2);
  });

  test("200 с телом вместо 204 — bad_response, а не «удалено»", async () => {
    const { a, server } = await api(new FakeServer().onboarded().seedMemory());
    const id = server.memory[0].id;
    server.inject("DELETE", `/memory/${id}`, 200, { ok: true });
    expect((await err(a.memoryDelete(id))).code).toBe("bad_response");
  });

  test("сеть и 5xx — ApiError, запись не удалена", async () => {
    const { a, server } = await api(new FakeServer().onboarded().seedMemory());
    const id = server.memory[0].id;
    server.failNetwork("DELETE", `/memory/${id}`);
    expect((await err(a.memoryDelete(id))).code).toBe("network");
    server.inject("DELETE", `/memory/${id}`, 500, { code: "internal", message: "x" });
    expect((await err(a.memoryDelete(id))).status).toBe(500);
    expect(server.deletedMemory.has(id)).toBe(false);
  });

  test("401 — один повторный вход и один повтор", async () => {
    const { a, server } = await api(new FakeServer().onboarded().seedMemory());
    const id = server.memory[0].id;
    server.expireSession();
    expect(await a.memoryDelete(id)).toBe("deleted");
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
  });
});
