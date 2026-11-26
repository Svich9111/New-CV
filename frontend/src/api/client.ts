import { errorText } from "./messages";
import type {
  ApiErrorBody,
  AuthRequest,
  Couple,
  HelpContact,
  InviteCreated,
  InvitePreview,
  Me,
  OnboardingRequest,
  Session,
} from "./types";

/** Ошибка запроса: статус HTTP (0 — нет связи), код контракта и текст для человека. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfter?: number;

  constructor(status: number, code: string, text: string, retryAfter?: number) {
    super(text);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

export interface ApiOptions {
  /** Строка Telegram.WebApp.initData — только для `POST /auth/telegram`. */
  initData: () => string;
  baseUrl?: string;
  fetch?: typeof fetch;
}

interface RequestOptions {
  body?: unknown;
  /** Публичный путь без сессии (`security: []`): вход, `/help/contacts`. */
  public?: boolean;
}

function isErrorBody(v: unknown): v is ApiErrorBody {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as ApiErrorBody).code === "string" &&
    typeof (v as ApiErrorBody).message === "string"
  );
}

function parseRetryAfter(h: string | null): number | undefined {
  if (!h) return undefined;
  const n = Number.parseInt(h, 10);
  return Number.isFinite(n) && n > 0 ? n : undefined;
}

/**
 * Клиент API по контракту `api/openapi.yaml` (сервер `/api/v1`).
 *
 * - Токен сессии живёт только в памяти этого объекта: ни localStorage, ни sessionStorage, ни cookie.
 * - 401 на защищённом пути — один повторный вход по initData и один повтор запроса (истёк токен).
 * - 409 `conflict_retry` — ничего не изменено (АР-24), запрос повторяется один раз.
 * - 422, 429 (`Retry-After`), 5xx и обрыв связи — `ApiError` с понятным текстом.
 * - Тело ответа с ошибкой не журналируется; клиент не пишет в консоль вообще.
 */
export class Api {
  private token: string | null = null;
  private readonly initData: () => string;
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: ApiOptions) {
    this.initData = opts.initData;
    this.baseUrl = opts.baseUrl ?? "/api/v1";
    this.fetchImpl = opts.fetch ?? ((input, init) => globalThis.fetch(input, init));
  }

  get hasSession(): boolean {
    return this.token !== null;
  }

  async login(): Promise<Me> {
    this.token = null;
    const body: AuthRequest = { init_data: this.initData() };
    const session = await this.send<Session>("POST", "/auth/telegram", { body, public: true });
    this.token = session.access_token;
    return session.me;
  }

  me(): Promise<Me> {
    return this.call<Me>("GET", "/me");
  }

  saveOnboarding(req: OnboardingRequest): Promise<Me> {
    return this.call<Me>("PUT", "/me/onboarding", { body: req });
  }

  /** Текущая пара; `null` — пары нет (404). */
  async couple(): Promise<Couple | null> {
    try {
      return await this.call<Couple>("GET", "/couple");
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null;
      throw e;
    }
  }

  createCouple(): Promise<InviteCreated> {
    return this.call<InviteCreated>("POST", "/couple");
  }

  reissueInvite(): Promise<InviteCreated> {
    return this.call<InviteCreated>("POST", "/couple/invite");
  }

  invite(token: string): Promise<InvitePreview> {
    return this.call<InvitePreview>("GET", `/invites/${encodeURIComponent(token)}`);
  }

  acceptInvite(token: string): Promise<Couple> {
    return this.call<Couple>("POST", `/invites/${encodeURIComponent(token)}/accept`);
  }

  helpContacts(): Promise<HelpContact[]> {
    return this.call<HelpContact[]>("GET", "/help/contacts", { public: true });
  }

  private async call<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    try {
      return await this.sendWithConflictRetry<T>(method, path, opts);
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 401 || opts.public) throw e;
      // Токен истёк или сервер перезапущен: один повторный вход и один повтор запроса.
      await this.login();
      return this.sendWithConflictRetry<T>(method, path, opts);
    }
  }

  private async sendWithConflictRetry<T>(method: string, path: string, opts: RequestOptions): Promise<T> {
    try {
      return await this.send<T>(method, path, opts);
    } catch (e) {
      if (e instanceof ApiError && e.code === "conflict_retry") return this.send<T>(method, path, opts);
      throw e;
    }
  }

  private async send<T>(method: string, path: string, opts: RequestOptions): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (!opts.public) {
      if (!this.token) throw new ApiError(401, "unauthorized", errorText("unauthorized"));
      headers.Authorization = `Bearer ${this.token}`;
    }
    let res: Response;
    try {
      res = await this.fetchImpl(this.baseUrl + path, {
        method,
        headers,
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
        credentials: "omit",
        cache: "no-store",
      });
    } catch {
      throw new ApiError(0, "network", errorText("network"));
    }
    if (res.status === 204) return undefined as T;
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    if (res.ok) return data as T;
    const retryAfter = parseRetryAfter(res.headers.get("Retry-After"));
    if (isErrorBody(data)) {
      throw new ApiError(res.status, data.code, errorText(data.code, data.message, retryAfter), retryAfter);
    }
    const code =
      res.status === 401 ? "unauthorized" : res.status === 429 ? "rate_limited" : res.status >= 500 ? "internal" : "http_error";
    throw new ApiError(res.status, code, errorText(code, undefined, retryAfter), retryAfter);
  }
}
