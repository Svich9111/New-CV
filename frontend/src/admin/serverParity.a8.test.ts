/// <reference types="node" />
/**
 * A8, задача 2.8 (база знаний), раунд 1 — сверка клиента панели с контрактом (`api/openapi.yaml`, `/admin/kb*`)
 * и сервером (`backend/app/api/admin_kb.py`, `app/errors.py`, `app/main.py`, перечень `index_error` —
 * `app/kb/parse.py`, `app/workers/kb.py`). ФТ-15, ФТ-20, ТЗ §6.4.
 *
 * Тест читает исходники как текст: расхождение предела, формата, поля формы или кода ответа на любой стороне —
 * провал. Дефекты закреплены `test.fails` (см. `docs/reviews/2.8-kb-test-r1.md`).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { AdminApi, AdminApiError, KB_FORMATS, MAX_FILE_BYTES } from "./api";
import { adminErrorText, indexErrorText } from "./texts";
import { ADMIN_TOKEN, json } from "./test/fakeAdminApi";

const repo = join(__dirname, "..", "..", "..");
const read = (p: string) => readFileSync(join(repo, p), "utf8");

const openapi = read("api/openapi.yaml");
const adminKb = read("backend/app/api/admin_kb.py");
const errorsPy = read("backend/app/errors.py");
const depsPy = read("backend/app/api/deps.py");
const mainPy = read("backend/app/main.py");
const parsePy = read("backend/app/kb/parse.py");
const workerPy = read("backend/app/workers/kb.py");

/** Участок контракта `POST /admin/kb/documents` (до следующего пути). */
const uploadSpec = (() => {
  const start = openapi.indexOf("  /admin/kb/documents:\n");
  const end = openapi.indexOf("  /admin/kb/documents/{document_id}:\n");
  expect(start).toBeGreaterThan(0);
  expect(end).toBeGreaterThan(start);
  return openapi.slice(start, end);
})();
const kbSchema = (() => {
  const start = openapi.indexOf("    KbDocument:\n");
  return openapi.slice(start, openapi.indexOf("\n\n", start));
})();

const UI_TEXT_LIMITS = { title: 300, author: 200, approach: 100 };

describe("пределы и форматы: клиент = контракт = сервер (ФТ-15, §6.4)", () => {
  test("20 МиБ: клиент 20 971 520 байт, контракт (описание и maximum source_size), сервер MAX_FILE_BYTES и предел тела", () => {
    expect(MAX_FILE_BYTES).toBe(20_971_520);
    expect(uploadSpec).toContain("20 971 520 байт");
    expect(kbSchema).toMatch(/source_size:.*maximum: 20971520/);
    expect(adminKb).toMatch(/^MAX_FILE_BYTES = 20 \* 1024 \* 1024/m);
    // сервер отвергает только «больше» (ровно 20 МиБ — можно), как и клиент
    expect(adminKb).toContain("if len(buf) > MAX_FILE_BYTES:");
    // предел тела на загрузку — файл + запас на поля формы; ответ — тот же код file_too_large
    expect(adminKb).toMatch(/^MAX_UPLOAD_BODY_BYTES = MAX_FILE_BYTES \+ 1024 \* 1024/m);
    expect(mainPy).toMatch(/\("POST", admin_kb\.UPLOAD_PATH\): \(\s*admin_kb\.MAX_UPLOAD_BODY_BYTES,\s*"file_too_large"/);
    expect(adminKb).toContain('UPLOAD_PATH = "/api/v1/admin/kb/documents"');
  });

  test("форматы: клиент = enum source_format = detect_format сервера", () => {
    expect([...KB_FORMATS]).toEqual(["pdf", "epub", "docx", "txt"]);
    expect(kbSchema).toContain("enum: [pdf, epub, docx, txt, null]");
    for (const f of KB_FORMATS) expect(adminKb).toContain(`if ext == "${f}"`);
    // сервер берёт расширение в нижнем регистре — клиент тоже (extensionOf), «Книга.PDF» проходит обе проверки
    expect(adminKb).toContain('filename.rsplit(".", 1)[-1].lower()');
  });

  test("поля формы multipart: имена и обязательность — как в контракте и в UploadFields сервера", () => {
    expect(uploadSpec).toContain("required: [file, title, rights_basis]");
    for (const [f, n] of Object.entries(UI_TEXT_LIMITS)) {
      expect(uploadSpec).toMatch(new RegExp(`${f}:[^\\n]*maxLength: ${n}|${f}:\\n\\s+type: string\\n\\s+maxLength: ${n}`));
      expect(adminKb).toMatch(new RegExp(`${f}: str[^\\n]*max_length=${n}\\)`));
    }
    expect(adminKb).toMatch(/rights_basis: str = Field\(min_length=1\)/);
    // сервер запрещает лишние поля (extra="forbid") — клиент обязан слать только поля контракта
    expect(adminKb).toContain('ConfigDict(extra="forbid", strict=True)');
  });

  test("клиент шлёт ровно поля контракта, файл — полем file с именем файла, Content-Type не задаёт (граница multipart)", async () => {
    let init: RequestInit | undefined;
    const api = new AdminApi({
      token: ADMIN_TOKEN,
      fetch: async (_u, i) => {
        init = i;
        return json(202, {
          id: "00000000-0000-4000-8000-000000000001",
          title: "Т",
          author: "А",
          approach: "П",
          rights_basis: "Р",
          index_status: "uploaded",
          index_error: null,
          source_format: "txt",
          source_size: 5,
          sha256: null,
          created_at: "2026-11-26T12:00:00Z",
          indexed_at: null,
        });
      },
    });
    await api.uploadDocument({ file: new File(["текст"], "Книга.TXT"), title: "Т", author: "А", approach: "П", rights_basis: "Р" });
    const form = init!.body as FormData;
    expect(form).toBeInstanceOf(FormData);
    expect([...form.keys()].sort()).toEqual(["approach", "author", "file", "rights_basis", "title"]);
    expect((form.get("file") as File).name).toBe("Книга.TXT");
    expect(form.getAll("title")).toHaveLength(1); // сервер: повтор ключа — 422
    const headers = new Headers(init!.headers);
    expect(headers.has("Content-Type")).toBe(false);
    expect(init!.method).toBe("POST");
  });
});

describe("коды ответа сервера → понятный текст (критерий «все коды ответа — понятный текст»)", () => {
  const GENERIC = /Сервер ответил ошибкой \(код/;

  /** Коды, которые сервер действительно отдаёт на пути `/admin/kb/documents*`. */
  const serverCodes = [
    ...new Set([...adminKb.matchAll(/AppError\(\d{3}, "([a-z_]+)"/g)].map((m) => m[1])),
    ...[...depsPy.matchAll(/def unauthorized\(\)[^\n]*\n\s+return AppError\(401, "([a-z_]+)"/g)].map((m) => m[1]),
    "not_found", // app.errors.not_found
  ];

  test("коды из admin_kb.py найдены", () => {
    expect(serverCodes).toEqual(expect.arrayContaining(["file_too_large", "unsupported_format", "validation_error", "admin_only", "duplicate_document", "unauthorized"]));
  });

  test("каждый код обработчика admin_kb.py — свой текст, не нейтральный «код …»", () => {
    for (const c of serverCodes) {
      for (const op of ["list", "upload", "delete"] as const) expect(adminErrorText(c, op), `${op}: ${c}`).not.toMatch(GENERIC);
    }
  });

  test("409/413/415/422 из контракта POST — свой текст", () => {
    for (const c of ["duplicate_document", "file_too_large", "unsupported_format"]) {
      expect(uploadSpec).toContain(`value: { code: ${c},`);
      expect(adminErrorText(c, "upload")).not.toMatch(GENERIC);
    }
    expect(uploadSpec).toContain('"422": { $ref: "#/components/responses/Validation" }');
    expect(adminErrorText("validation_error", "upload")).not.toMatch(GENERIC);
  });

  test("ответ сервера без JSON (прокси) по статусу 401/403/404/413/415/422/500/503 — свой текст", async () => {
    for (const status of [401, 403, 404, 413, 415, 422, 500, 502, 503]) {
      const api = new AdminApi({ token: ADMIN_TOKEN, fetch: async () => new Response("<html>proxy</html>", { status }) });
      const e = (await api.listDocuments().catch((x: unknown) => x)) as AdminApiError;
      expect(e).toBeInstanceOf(AdminApiError);
      expect(adminErrorText(e.code, "upload"), String(status)).not.toMatch(GENERIC);
    }
  });

  // [MINOR-1] Необработанная ошибка сервера — 500 {"code":"internal"} (app/errors.py `_unhandled`); у клиента текст
  // «Сбой на сервере» есть только для кода server_error (ответ без JSON). Настоящий 500 сервера показывается
  // нейтральной строкой «Сервер ответил ошибкой (код «internal»)».
  test("[MINOR-1] 500 internal (настоящий ответ сервера) — «Сбой на сервере», а не «код internal»", () => {
    expect(errorsPy).toContain('"code": "internal"');
    expect(adminErrorText("internal", "upload")).toBe("Сбой на сервере. Повторите позже.");
  });

  // [MINOR-1] 409 conflict_retry (app/errors.py `_db_conflict`, АР-24) — сервер отдаёт на любом пути с БД,
  // в т.ч. загрузке и удалении; у клиента своего текста нет.
  test("[MINOR-1] 409 conflict_retry — свой текст («повторите»), а не «код conflict_retry»", () => {
    expect(errorsPy).toContain('"code": "conflict_retry"');
    expect(adminErrorText("conflict_retry", "upload")).not.toMatch(GENERIC);
  });

  // [MINOR-2] Сервер разбирает форму с max_part_size=64 КиБ (admin_kb.py MAX_FIELD_BYTES); поле длиннее — Starlette
  // отвечает 400, обработчик ошибок даёт {"code":"http_error"} (app/errors.py `_HTTP_CODES`). В контракте ни
  // maxLength у rights_basis, ни ответа 400 нет; клиент длину основания прав не проверяет — администратор видит
  // «Сервер ответил ошибкой (код «http_error»)». Проверено на Starlette 1.7.0 (поле 65 537 байт — 400 http_error).
  test("сервер: предел поля формы 64 КиБ, 400 → http_error; в контракте нет ни maxLength rights_basis, ни 400", () => {
    expect(adminKb).toMatch(/^MAX_FIELD_BYTES = 64 \* 1024/m);
    expect(adminKb).toContain("max_part_size=MAX_FIELD_BYTES");
    expect(errorsPy).toContain('("http_error", "Запрос не выполнен")');
    expect(uploadSpec).not.toMatch(/"400":/);
    expect(uploadSpec).toMatch(/rights_basis:\n\s+type: string\n\s+minLength: 1\n\s+description/);
  });
  test("[MINOR-2] 400 http_error на загрузке (поле формы больше 64 КиБ) — понятный текст", () => {
    expect(adminErrorText("http_error", "upload")).not.toMatch(GENERIC);
  });
});

describe("index_error: каждый код воркера — понятный текст (ФТ-15, статус индексации подписью)", () => {
  const constants = (src: string) => [...src.matchAll(/^[A-Z_]+ = "([a-z][a-z0-9_]*)"$/gm)].map((m) => m[1]);
  const codes = [...new Set([...constants(parsePy), ...constants(workerPy)])].filter(
    (c) => !["spawn"].includes(c),
  );

  test("перечень найден в parse.py и workers/kb.py", () => {
    expect(codes).toEqual(
      expect.arrayContaining(["too_large", "too_many_pages", "zip_bomb", "encrypted", "corrupt", "empty_text", "timeout", "unsupported_format", "attempts_exhausted", "too_many_chunks"]),
    );
  });

  test("для каждого кода — свой текст, не «Неизвестная причина»", () => {
    for (const c of codes) {
      expect(c, "код укладывается в CHECK index_error").toMatch(/^[a-z][a-z0-9_]{0,31}$/);
      expect(indexErrorText(c), c).not.toMatch(/Неизвестная причина/);
    }
  });

  test("предел страниц в тексте too_many_pages = ParseLimits.max_pages сервера", () => {
    const pages = /max_pages: int = (\d+)/.exec(parsePy)?.[1];
    expect(pages).toBeDefined();
    expect(indexErrorText("too_many_pages")).toContain(`больше ${pages}`);
  });
});

describe("удаление: 404 сервера — not_found (клиент считает «уже удалён» только при этом коде)", () => {
  test("app.errors.not_found → code not_found; неверный uuid на сервере — тоже 404", () => {
    expect(errorsPy).toContain('NOT_FOUND_CODE = "not_found"');
    expect(adminKb).toMatch(/except ValueError as exc:\n\s+raise not_found\(\) from exc/);
  });
});
