import type { KbDocument } from "../types";

/**
 * Заглушка админ-API по контракту `api/openapi.yaml` (пути `/admin/kb/documents*`, коды, схема `KbDocument`).
 * Отдельно от заглушки Mini App (`src/test/fakeApi.ts`). Данные выдуманные; текстов материалов нет и в контракте.
 *
 * Журнал запросов (`log`) — для проверки, что клиент панели ходит только к `/api/v1/admin/*`. Любой другой путь
 * тоже попадает в журнал и получает 404.
 */

export const ADMIN_TOKEN = "adm-test-" + "k".repeat(40);
/** Действующая сессия пользователя Mini App — сервер отвечает 403 `admin_only`. */
export const USER_SESSION_TOKEN = "user-session." + "s".repeat(40);

export interface AdminLogged {
  method: string;
  path: string;
  query: string;
  auth: string | null;
  /** Поля multipart (файл — `{ name, size }`) или `null`. */
  body: Record<string, unknown> | null;
  credentials: RequestCredentials | undefined;
}

interface Injected {
  status: number;
  body?: unknown;
  raw?: string;
  times: number;
  network?: boolean;
  /** Ответ не приходит, пока не вызван `release()`. */
  hold?: Promise<void>;
}

const MAX = 20 * 1024 * 1024;
const FORMATS = ["pdf", "epub", "docx", "txt"];
const err = (code: string, message: string) => ({ code, message });

export function json(status: number, body?: unknown): Response {
  if (status === 204 || body === undefined) return new Response(null, { status, headers: { "Cache-Control": "no-store" } });
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

let seq = 0;
export function docId(n: number): string {
  return `00000000-0000-4000-8000-${n.toString(16).padStart(12, "0")}`;
}

export function makeDoc(over: Partial<KbDocument> = {}): KbDocument {
  seq += 1;
  return {
    id: docId(1000 + seq),
    title: "Выдуманная книга об общении",
    author: "И. Иванова",
    approach: "Ненасильственное общение",
    rights_basis: "Лицензия издательства (выдуманная)",
    index_status: "indexed",
    index_error: null,
    source_format: "pdf",
    source_size: 1_572_864,
    sha256: "a".repeat(64),
    created_at: "2026-11-20T10:00:00Z",
    indexed_at: "2026-11-20T10:05:00Z",
    ...over,
  };
}

export class FakeAdminApi {
  readonly log: AdminLogged[] = [];
  docs: KbDocument[] = [];
  private readonly injected = new Map<string, Injected[]>();
  private uploads = 0;

  /** Следующие `times` ответов на `METHOD /path` (путь без `/api/v1`) — заданные. */
  inject(method: string, path: string, r: Omit<Injected, "times"> & { times?: number }): void {
    const key = `${method} ${path}`;
    const list = this.injected.get(key) ?? [];
    list.push({ times: 1, ...r });
    this.injected.set(key, list);
  }

  /** Сменить статус материала (как воркер индексации). */
  setStatus(id: string, index_status: KbDocument["index_status"], index_error: string | null = null): void {
    this.docs = this.docs.map((d) =>
      d.id === id
        ? { ...d, index_status, index_error, indexed_at: index_status === "indexed" ? "2026-11-26T12:00:00Z" : d.indexed_at }
        : d,
    );
  }

  requests(method?: string, path?: string): AdminLogged[] {
    return this.log.filter((l) => (!method || l.method === method) && (!path || l.path === path));
  }

  fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(String(input), "http://admin.test");
    const method = (init.method ?? "GET").toUpperCase();
    const headers = new Headers(init.headers);
    const auth = headers.get("Authorization");
    let body: Record<string, unknown> | null = null;
    if (init.body instanceof FormData) {
      body = {};
      for (const [k, v] of init.body.entries()) body[k] = typeof v === "string" ? v : { name: v.name, size: v.size };
    }
    this.log.push({ method, path: url.pathname, query: url.search, auth, body, credentials: init.credentials });

    const p = url.pathname.startsWith("/api/v1") ? url.pathname.slice("/api/v1".length) : null;
    const key = `${method} ${p}`;
    const queue = this.injected.get(key);
    if (queue && queue.length) {
      const r = queue[0];
      r.times -= 1;
      if (r.times <= 0) queue.shift();
      if (r.hold) await r.hold;
      if (r.network) throw new TypeError("Failed to fetch");
      if (r.raw !== undefined) return new Response(r.raw, { status: r.status });
      return json(r.status, r.body);
    }

    if (p === null || !p.startsWith("/admin/")) return json(404, err("not_found", "Не найдено"));
    if (!auth?.startsWith("Bearer ")) return json(401, err("unauthorized", "Нужен вход"));
    const token = auth.slice("Bearer ".length);
    if (token === USER_SESSION_TOKEN) return json(403, err("admin_only", "Доступно только администратору"));
    if (token !== ADMIN_TOKEN) return json(401, err("unauthorized", "Нужен вход"));

    if (p === "/admin/kb/documents" && method === "GET") return json(200, this.docs);
    if (p === "/admin/kb/documents" && method === "POST") return this.upload(init.body);
    const m = /^\/admin\/kb\/documents\/([^/]+)$/.exec(p);
    if (m && method === "DELETE") {
      const before = this.docs.length;
      this.docs = this.docs.filter((d) => d.id !== m[1]);
      return before === this.docs.length ? json(404, err("not_found", "Не найдено")) : json(204);
    }
    return json(404, err("not_found", "Не найдено"));
  };

  private upload(raw: unknown): Response {
    if (!(raw instanceof FormData)) return json(422, err("validation_error", "Проверьте данные"));
    const file = raw.get("file");
    const title = raw.get("title");
    const rights = raw.get("rights_basis");
    if (!(file instanceof Blob) || typeof title !== "string" || !title.trim() || title.length > 300) return json(422, err("validation_error", "Проверьте данные"));
    if (typeof rights !== "string" || !rights.trim()) return json(422, err("validation_error", "Проверьте данные"));
    if (file.size > MAX) return json(413, err("file_too_large", "Файл больше 20 МиБ"));
    const name = (file as File).name ?? "";
    const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
    if (!FORMATS.includes(ext)) return json(415, err("unsupported_format", "Поддерживаются PDF, EPUB, DOCX и TXT"));
    const author = raw.get("author");
    const approach = raw.get("approach");
    this.uploads += 1;
    const doc: KbDocument = {
      id: docId(this.uploads),
      title,
      author: typeof author === "string" ? author : null,
      approach: typeof approach === "string" ? approach : null,
      rights_basis: rights,
      index_status: "uploaded",
      index_error: null,
      source_format: ext as KbDocument["source_format"],
      source_size: file.size,
      sha256: this.uploads.toString(16).padStart(64, "0"),
      created_at: "2026-11-26T12:00:00Z",
      indexed_at: null,
    };
    this.docs = [doc, ...this.docs];
    return json(202, doc);
  }
}

/** Отложенный ответ: `hold` для `inject`, `release()` — отпустить. */
export function deferred(): { hold: Promise<void>; release(): void } {
  let release!: () => void;
  const hold = new Promise<void>((r) => (release = r));
  return { hold, release };
}
