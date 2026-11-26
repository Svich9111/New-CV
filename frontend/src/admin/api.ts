/**
 * Клиент админ-панели (АР-52): только пути `/api/v1/admin/*`, вход — токен `adminBearer` (АР-39).
 * Отдельно от клиента Mini App: ни Telegram, ни сессии пользователя, ни пользовательских путей.
 *
 * Токен хранится только в памяти вкладки — в закрытом поле объекта клиента, который держит экран; он не пишется
 * ни в хранилища браузера, ни в cookie (`credentials: "omit"`), ни в адрес, ни в журнал. Ошибки не содержат токена.
 */
import type { KbDocument, KbUploadFields } from "./types";

export const ADMIN_PATH_PREFIX = "/admin/";
/** Предел файла — как на сервере (`MAX_FILE_BYTES`, CHECK `source_data`). */
export const MAX_FILE_BYTES = 20 * 1024 * 1024;
export const KB_FORMATS = ["pdf", "epub", "docx", "txt"] as const;

export const ADMIN_TIMEOUTS = { default: 20_000, upload: 180_000 };

/** Ошибка запроса: статус HTTP (0 — нет связи или срок вышел) и код контракта. */
export class AdminApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string) {
    super(`admin api: ${status} ${code}`);
    this.name = "AdminApiError";
    this.status = status;
    this.code = code;
  }
}

export interface AdminApiOptions {
  token: string;
  /** Префикс API (по умолчанию `/api/v1`). */
  baseUrl?: string;
  fetch?: typeof fetch;
  timeouts?: Partial<typeof ADMIN_TIMEOUTS>;
}

export interface KbUpload extends KbUploadFields {
  file: File;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SAFE_PATH = /^\/admin\/[a-z0-9/_-]+$/i;
const CODE = /^[a-z][a-z0-9_]{0,63}$/;

/** Путь запроса — только `/admin/*`: любой другой путь — ошибка программы, запрос не уходит. */
export function adminPath(path: string): string {
  if (!path.startsWith(ADMIN_PATH_PREFIX) || !SAFE_PATH.test(path) || path.includes("..")) {
    throw new Error("admin client: path outside /admin/*");
  }
  return path;
}

function fallbackCode(status: number): string {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 413) return "file_too_large";
  if (status === 415) return "unsupported_format";
  if (status === 422) return "validation_error";
  if (status === 503) return "service_unavailable";
  if (status >= 500) return "server_error";
  return `http_${status}`;
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const optStr = (v: unknown) => v === undefined || v === null || typeof v === "string";
const isDate = (v: unknown) => typeof v === "string" && !Number.isNaN(Date.parse(v));
const optDate = (v: unknown) => v === undefined || v === null || isDate(v);

/**
 * Строгая проверка `KbDocument` и копия только известных полей: неизвестные поля ответа (если сервер когда-нибудь
 * добавит лишнее) до экрана не доходят.
 */
export function toKbDocument(v: unknown): KbDocument | null {
  if (!isObj(v)) return null;
  const { id, title, author, approach, rights_basis, index_status, index_error, source_format, source_size, sha256 } = v;
  if (typeof id !== "string" || !UUID.test(id)) return null;
  if (typeof title !== "string" || typeof rights_basis !== "string") return null;
  if (!optStr(author) || !optStr(approach)) return null;
  if (index_status !== "uploaded" && index_status !== "indexing" && index_status !== "indexed" && index_status !== "failed")
    return null;
  if (!(index_error === undefined || index_error === null || (typeof index_error === "string" && /^[a-z][a-z0-9_]{0,31}$/.test(index_error))))
    return null;
  if (!(source_format === undefined || source_format === null || (KB_FORMATS as readonly unknown[]).includes(source_format)))
    return null;
  if (
    !(
      source_size === undefined ||
      source_size === null ||
      (typeof source_size === "number" && Number.isInteger(source_size) && source_size >= 1 && source_size <= MAX_FILE_BYTES)
    )
  )
    return null;
  if (!(sha256 === undefined || sha256 === null || (typeof sha256 === "string" && /^[0-9a-f]{64}$/.test(sha256)))) return null;
  if (!isDate(v.created_at) || !optDate(v.indexed_at)) return null;
  return {
    id,
    title,
    author: (author as string | null | undefined) ?? null,
    approach: (approach as string | null | undefined) ?? null,
    rights_basis,
    index_status,
    index_error: (index_error as string | null | undefined) ?? null,
    source_format: (source_format as KbDocument["source_format"]) ?? null,
    source_size: (source_size as number | null | undefined) ?? null,
    sha256: (sha256 as string | null | undefined) ?? null,
    created_at: v.created_at as string,
    indexed_at: (v.indexed_at as string | null | undefined) ?? null,
  };
}

class Timeout extends Error {}

export class AdminApi {
  readonly #token: string;
  readonly #baseUrl: string;
  readonly #fetch: typeof fetch;
  readonly #timeouts: typeof ADMIN_TIMEOUTS;

  constructor(opts: AdminApiOptions) {
    this.#token = opts.token;
    this.#baseUrl = opts.baseUrl ?? "/api/v1";
    this.#fetch = opts.fetch ?? ((...a) => fetch(...a));
    this.#timeouts = { ...ADMIN_TIMEOUTS, ...opts.timeouts };
  }

  /** Все материалы базы знаний (`GET /admin/kb/documents`). */
  async listDocuments(): Promise<KbDocument[]> {
    const res = await this.#request("GET", "/admin/kb/documents");
    if (res.status !== 200) throw new AdminApiError(res.status, "bad_response");
    const body = await this.#json(res);
    if (!Array.isArray(body)) throw new AdminApiError(res.status, "bad_response");
    const out: KbDocument[] = [];
    const seen = new Set<string>();
    for (const item of body) {
      const doc = toKbDocument(item);
      if (!doc || seen.has(doc.id)) throw new AdminApiError(res.status, "bad_response");
      seen.add(doc.id);
      out.push(doc);
    }
    return out;
  }

  /** Загрузка (`POST /admin/kb/documents`, multipart); принято — 202 и материал в `uploaded`. */
  async uploadDocument(input: KbUpload): Promise<KbDocument> {
    const form = new FormData();
    form.append("file", input.file, input.file.name);
    form.append("title", input.title);
    if (input.author) form.append("author", input.author);
    if (input.approach) form.append("approach", input.approach);
    form.append("rights_basis", input.rights_basis);
    const res = await this.#request("POST", "/admin/kb/documents", form, this.#timeouts.upload);
    if (res.status !== 202) throw new AdminApiError(res.status, "bad_response");
    const doc = toKbDocument(await this.#json(res));
    if (!doc) throw new AdminApiError(res.status, "bad_response");
    return doc;
  }

  /** Удаление (`DELETE /admin/kb/documents/{id}`): 204 — `deleted`, 404 — `gone` (уже удалён). */
  async deleteDocument(id: string): Promise<"deleted" | "gone"> {
    if (!UUID.test(id)) throw new AdminApiError(0, "bad_response");
    try {
      const res = await this.#request("DELETE", `/admin/kb/documents/${id.toLowerCase()}`);
      if (res.status !== 204) throw new AdminApiError(res.status, "bad_response");
      return "deleted";
    } catch (e) {
      if (e instanceof AdminApiError && e.status === 404 && e.code === "not_found") return "gone";
      throw e;
    }
  }

  async #request(method: string, path: string, body?: FormData, timeoutMs = this.#timeouts.default): Promise<Response> {
    const url = this.#baseUrl + adminPath(path);
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_, rej) => {
      timer = setTimeout(() => {
        ctrl.abort();
        rej(new Timeout());
      }, timeoutMs);
    });
    let res: Response;
    try {
      res = await Promise.race([
        this.#fetch(url, {
          method,
          headers: { Authorization: `Bearer ${this.#token}`, Accept: "application/json" },
          body,
          credentials: "omit",
          cache: "no-store",
          referrerPolicy: "no-referrer",
          signal: ctrl.signal,
        }),
        timeout,
      ]);
    } catch (e) {
      throw new AdminApiError(0, e instanceof Timeout || ctrl.signal.aborted ? "timeout" : "network");
    } finally {
      clearTimeout(timer);
    }
    if (res.ok) return res;
    let code = fallbackCode(res.status);
    try {
      const err: unknown = await res.json();
      if (isObj(err) && typeof err.code === "string" && CODE.test(err.code)) code = err.code;
    } catch {
      // тело ошибки не JSON (например, ответ прокси) — код по статусу
    }
    throw new AdminApiError(res.status, code);
  }

  async #json(res: Response): Promise<unknown> {
    try {
      return await res.json();
    } catch {
      throw new AdminApiError(res.status, "bad_response");
    }
  }
}
