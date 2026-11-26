/**
 * Клиент админ-панели: только `/api/v1/admin/*`, токен — только заголовком, без cookie; строгая проверка
 * `KbDocument`; коды ответа — по контракту, при теле не-JSON — по статусу; 204/404 удаления.
 */
import { AdminApi, AdminApiError, adminPath, toKbDocument } from "./api";
import { ADMIN_TOKEN, FakeAdminApi, json, makeDoc } from "./test/fakeAdminApi";

const make = (fake = new FakeAdminApi(), timeouts?: { default?: number; upload?: number }) => ({
  fake,
  api: new AdminApi({ token: ADMIN_TOKEN, fetch: fake.fetch, timeouts }),
});

async function code(p: Promise<unknown>): Promise<[number, string]> {
  try {
    await p;
  } catch (e) {
    if (e instanceof AdminApiError) return [e.status, e.code];
    throw e;
  }
  throw new Error("expected AdminApiError");
}

test("adminPath: только /admin/*", () => {
  expect(adminPath("/admin/kb/documents")).toBe("/admin/kb/documents");
  for (const bad of ["/chat/messages", "/memory", "/auth/telegram", "/admin/../memory", "admin/kb", "/admin/kb?x=1", "//evil/admin/", "/drafts"])
    expect(() => adminPath(bad), bad).toThrow();
});

test("запросы: путь /api/v1/admin/*, Bearer, credentials omit, cache no-store; ошибка не содержит токена", async () => {
  const calls: [string, RequestInit][] = [];
  const api = new AdminApi({
    token: ADMIN_TOKEN,
    fetch: async (u, i) => {
      calls.push([String(u), i!]);
      return json(401, { code: "unauthorized", message: "x" });
    },
  });
  const e = await api.listDocuments().catch((x: unknown) => x);
  expect(calls[0][0]).toBe("/api/v1/admin/kb/documents");
  expect(calls[0][1]).toMatchObject({ method: "GET", credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer" });
  expect(new Headers(calls[0][1].headers).get("Authorization")).toBe(`Bearer ${ADMIN_TOKEN}`);
  expect(e).toBeInstanceOf(AdminApiError);
  expect(String(e) + JSON.stringify(e) + (e as Error).stack).not.toContain(ADMIN_TOKEN);
  expect(JSON.stringify(api)).not.toContain(ADMIN_TOKEN); // закрытое поле
});

test("список: строгая проверка, повтор id — bad_response", async () => {
  const { fake, api } = make();
  const d = makeDoc();
  fake.inject("GET", "/admin/kb/documents", { status: 200, body: [d, d] });
  expect(await code(api.listDocuments())).toEqual([200, "bad_response"]);
  fake.inject("GET", "/admin/kb/documents", { status: 200, body: { items: [] } });
  expect(await code(api.listDocuments())).toEqual([200, "bad_response"]);
  fake.inject("GET", "/admin/kb/documents", { status: 200, raw: "not json" });
  expect(await code(api.listDocuments())).toEqual([200, "bad_response"]);
});

test.each([
  ["id не uuid", { id: "1" }],
  ["нет названия", { title: undefined }],
  ["нет основания прав", { rights_basis: undefined }],
  ["неизвестный статус", { index_status: "done" }],
  ["код причины не по шаблону", { index_error: "Текст ошибки" }],
  ["неизвестный формат", { source_format: "doc" }],
  ["размер больше 20 МиБ", { source_size: 20 * 1024 * 1024 + 1 }],
  ["размер 0", { source_size: 0 }],
  ["sha256 не hex", { sha256: "z".repeat(64) }],
  ["дата не дата", { created_at: "вчера" }],
])("toKbDocument отвергает: %s", (_, over) => {
  expect(toKbDocument({ ...makeDoc(), ...over })).toBeNull();
});

test("toKbDocument: только известные поля; необязательные — null", () => {
  const min: Record<string, unknown> = { ...makeDoc() };
  for (const k of ["author", "approach", "index_error", "source_format", "source_size", "sha256", "indexed_at"]) delete min[k];
  const out = toKbDocument({ ...min, text: "секрет", chunks: ["фрагмент"] })!;
  expect(Object.keys(out).sort()).toEqual(
    ["approach", "author", "created_at", "id", "index_error", "index_status", "indexed_at", "rights_basis", "sha256", "source_format", "source_size", "title"],
  );
  expect(out.author).toBeNull();
  expect(out.indexed_at).toBeNull();
});

test("коды ответа: из тела по контракту, без JSON — по статусу", async () => {
  const { fake, api } = make();
  for (const [status, expected] of [
    [413, "file_too_large"],
    [415, "unsupported_format"],
    [422, "validation_error"],
    [403, "forbidden"],
    [502, "server_error"],
    [503, "service_unavailable"],
  ] as const) {
    fake.inject("GET", "/admin/kb/documents", { status, raw: "<html/>" });
    expect(await code(api.listDocuments())).toEqual([status, expected]);
  }
  fake.inject("GET", "/admin/kb/documents", { status: 403, body: { code: "admin_only", message: "x" } });
  expect(await code(api.listDocuments())).toEqual([403, "admin_only"]);
  fake.inject("GET", "/admin/kb/documents", { status: 400, body: { code: "Bad Code!", message: "x" } });
  expect(await code(api.listDocuments())).toEqual([400, "http_400"]);
});

test("нет связи — network; срок вышел — timeout", async () => {
  const { fake, api } = make(undefined, { default: 20 });
  fake.inject("GET", "/admin/kb/documents", { status: 0, network: true });
  expect(await code(api.listDocuments())).toEqual([0, "network"]);
  const never = new AdminApi({ token: ADMIN_TOKEN, fetch: () => new Promise<Response>(() => {}), timeouts: { default: 20 } });
  expect(await code(never.listDocuments())).toEqual([0, "timeout"]);
});

test("удаление: 204 — deleted, 404 — gone, 200 — bad_response, не uuid — запрос не уходит", async () => {
  const { fake, api } = make();
  const d = makeDoc();
  fake.docs = [d];
  expect(await api.deleteDocument(d.id)).toBe("deleted");
  expect(await api.deleteDocument(d.id)).toBe("gone");
  fake.inject("DELETE", `/admin/kb/documents/${d.id}`, { status: 200, body: {} });
  expect(await code(api.deleteDocument(d.id))).toEqual([200, "bad_response"]);
  const n = fake.log.length;
  expect(await code(api.deleteDocument("../../memory"))).toEqual([0, "bad_response"]);
  expect(fake.log).toHaveLength(n);
});

test("загрузка: multipart с полями контракта; только 202 — успех", async () => {
  const { fake, api } = make();
  const doc = await api.uploadDocument({ file: new File(["x"], "a.txt"), title: "Т", rights_basis: "Лицензия" });
  expect(doc.index_status).toBe("uploaded");
  expect(fake.log[0]).toMatchObject({ method: "POST", path: "/api/v1/admin/kb/documents", body: { title: "Т", rights_basis: "Лицензия", file: { name: "a.txt" } } });
  fake.inject("POST", "/admin/kb/documents", { status: 201, body: makeDoc() });
  expect(await code(api.uploadDocument({ file: new File(["x"], "a.txt"), title: "Т", rights_basis: "Л" }))).toEqual([201, "bad_response"]);
});
