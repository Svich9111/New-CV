/**
 * A8, задача 2.5 (раунд 1) — независимая проверка клиента памяти (ФТ-13; ТЗ §6.3, §12 п.7; контракт `MemoryItem`,
 * `DELETE /memory/{memory_id}`). Запрос клиента сверяется с тем, что повторяет серверный тест
 * `backend/tests/test_memory_a8_2_5.py::test_client_shaped_delete_erases_row_in_db`. Данные выдуманные.
 */
import { Api, ApiError } from "./client";
import { isDay, isMemoryItem, isMemoryList } from "./memory";
import { FakeServer } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";

const base = {
  id: "30000000-0000-4000-8000-000000000001",
  type: "fact",
  text: "Работает посменно",
  event_date: null,
  source: "owner_dialog",
  confidence: 0.7,
  created_at: "2026-11-01T09:00:00Z",
};

interface Seen {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
  credentials: RequestCredentials | undefined;
  cache: RequestCache | undefined;
}

/** Api поверх заглушки, с записью каждого запроса «как его видит сеть». */
async function recorded(server = new FakeServer().onboarded().seedMemory()) {
  const seen: Seen[] = [];
  const fetch: typeof globalThis.fetch = (input, init = {}) => {
    const h = new Headers(init.headers);
    const headers: Record<string, string> = {};
    h.forEach((v, k) => (headers[k] = v));
    seen.push({
      url: String(input),
      method: init.method ?? "GET",
      headers,
      body: init.body,
      credentials: init.credentials,
      cache: init.cache,
    });
    return server.fetch(input, init);
  };
  const a = new Api({ initData: () => INIT_DATA, fetch });
  await a.login();
  seen.length = 0;
  return { a, server, seen };
}

async function rejected(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p;
  } catch (e) {
    expect(e).toBeInstanceOf(ApiError);
    return e as ApiError;
  }
  throw new Error("ожидалась ошибка");
}

describe("A8: форма запроса удаления = то, что проверяет сервер (§12 п.7)", () => {
  test("DELETE /api/v1/memory/{id}: Accept + Bearer, без тела и Content-Type, без строки запроса, без cookie и кэша", async () => {
    const { a, server, seen } = await recorded();
    const id = server.memory[0].id;
    expect(await a.memoryDelete(id)).toBe("deleted");
    expect(seen).toHaveLength(1);
    const [req] = seen;
    expect(req.method).toBe("DELETE");
    expect(req.url).toBe(`/api/v1/memory/${id}`);
    expect(req.body).toBeUndefined();
    expect(Object.keys(req.headers).sort()).toEqual(["accept", "authorization"]);
    expect(req.headers.accept).toBe("application/json");
    expect(req.headers.authorization).toMatch(/^Bearer test-session-\d+$/);
    expect(req.credentials).toBe("omit");
    expect(req.cache).toBe("no-store");
  });

  test("повторное использование: второй DELETE того же id — 404 → gone, новых удалений нет", async () => {
    const { a, server } = await recorded();
    const id = server.memory[0].id;
    await a.memoryDelete(id);
    const before = server.deletedMemory.size;
    expect(await a.memoryDelete(id)).toBe("gone");
    expect(server.deletedMemory.size).toBe(before);
  });

  test("истёкшая сессия: 401 → один вход, повтор с новым Bearer; запрос удаления ровно два", async () => {
    const { a, server, seen } = await recorded();
    const id = server.memory[0].id;
    server.inject("DELETE", `/memory/${id}`, 401, { code: "unauthorized", message: "x" });
    expect(await a.memoryDelete(id)).toBe("deleted");
    const dels = seen.filter((s) => s.method === "DELETE");
    expect(dels).toHaveLength(2);
    expect(dels[0].headers.authorization).not.toBe(dels[1].headers.authorization);
    expect(seen.filter((s) => s.url.endsWith("/auth/telegram"))).toHaveLength(1);
  });

  test("чужая запись (партнёра) — gone; запись партнёра не попадает в свой список", async () => {
    const { a, server } = await recorded();
    const [partnerId] = [...server.partnerMemory];
    expect(await a.memoryDelete(partnerId)).toBe("gone");
    expect((await a.memoryList()).map((i) => i.id)).not.toContain(partnerId);
  });

  test("429 при удалении — ошибка, а не «удалено» и не «уже удалена»", async () => {
    const { a, server } = await recorded();
    const id = server.memory[0].id;
    server.inject("DELETE", `/memory/${id}`, 429, { code: "rate_limited", message: "x" }, { "Retry-After": "5" });
    expect((await rejected(a.memoryDelete(id))).status).toBe(429);
    expect(server.deletedMemory.has(id)).toBe(false);
  });
});

describe("A8: вид полей настоящего сервера принимается (backend/tests/test_memory_a8_2_5.py)", () => {
  test.each([
    ["created_at с микросекундами и Z (pydantic)", { created_at: "2026-11-26T19:45:43.123456Z" }],
    ["created_at с микросекундами и +00:00", { created_at: "2026-11-26T19:45:43.123456+00:00" }],
    ["confidence 0.0 / 1.0 как число", { confidence: 1.0 }],
    ["UnavailableText с кодом причины", { text: { unavailable: true, reason: "context" } }],
    ["тип и источник не «парные» (контракт не связывает)", { type: "observation", source: "owner_dialog" }],
  ])("%s", (_n, patch) => {
    expect(isMemoryItem({ ...base, ...patch })).toBe(true);
  });

  test("поддельные данные: confidence строкой, NaN-подобное, лишний источник, id не uuid — отвергаются", () => {
    for (const patch of [
      { confidence: "0.5" },
      { confidence: 1.0000001 },
      { source: "partner_agent" },
      { type: "summary" },
      { id: "../me" },
      { text: { unavailable: false, reason: "x" } },
    ]) {
      expect(isMemoryItem({ ...base, ...patch }), JSON.stringify(patch)).toBe(false);
    }
  });

  // ДЕФЕКТ A8-1 (MAJOR): сервер законно отдаёт `event_date` с годом < 100 (`date.fromisoformat` при извлечении,
  // `format: date` контракта), а `isDay` строит дату через `Date.UTC`, который годы 0–99 толкует как 1900–1999.
  // Итог — одна такая запись делает весь ответ `bad_response`: экран памяти пуст, удалить нельзя ни одной записи
  // (ТЗ §2 п.6 «человек видит и удаляет любую запись»).
  test("isDay('0050-03-01') — существующий день (год 50)", () => {
    expect(isDay("0050-03-01")).toBe(true);
  });

  test("список с записью «год 50» принимается — остальные записи видны и удаляемы", () => {
    const old = { ...base, id: "30000000-0000-4000-8000-000000000002", type: "date", event_date: "0050-03-01" };
    expect(isMemoryList({ items: [base, old] })).toBe(true);
  });

  test("для сравнения: год 100 и выше принимается", () => {
    expect(isDay("0100-03-01")).toBe(true);
    expect(isDay("2027-06-14")).toBe(true);
  });
});

describe("A8: 404 не по контракту", () => {
  // ЗАМЕЧАНИЕ A8-2 (MINOR): любой 404 (в том числе HTML-страница прокси или неверного адреса стенда) клиент
  // считает «запись уже удалена» и убирает её с экрана, хотя контракт для этого пути объявляет 404 только с кодом
  // `not_found`. Человек видит «Эта запись уже была удалена.», а запись на сервере цела.
  test("404 без тела `not_found` — ошибка, а не gone", async () => {
    const { a, server } = await recorded();
    const id = server.memory[0].id;
    server.inject("DELETE", `/memory/${id}`, 404, "<html>Not Found</html>");
    await expect(a.memoryDelete(id)).rejects.toBeInstanceOf(ApiError);
  });

  test("404 с кодом not_found — gone", async () => {
    const { a, server } = await recorded();
    server.deletedMemory.add(server.memory[0].id);
    expect(await a.memoryDelete(server.memory[0].id)).toBe("gone");
  });
});
