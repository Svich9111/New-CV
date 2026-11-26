import { errorText, type Op } from "./messages";
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

/** Поможет ли повтор: нет связи, срок вышел, сбой сервера, лимит частоты, ответ не того вида. */
export function isRetryable(e: unknown): boolean {
  if (!(e instanceof ApiError)) return true;
  return e.status === 0 || e.status >= 500 || e.status === 429 || e.code === "conflict_retry" || e.code === "bad_response";
}

export interface ApiOptions {
  /** Строка Telegram.WebApp.initData — только для `POST /auth/telegram`. */
  initData: () => string;
  baseUrl?: string;
  fetch?: typeof fetch;
  /** Сроки ответа, мс (по умолчанию — `TIMEOUTS`). */
  timeouts?: Partial<typeof TIMEOUTS>;
}

/**
 * Сколько ждать ответа сервера. Вход — дольше обычного (холодный старт сервера), но не бесконечно:
 * по истечении человек видит причину и «Попробовать снова». Контакты помощи ждём недолго — запасной
 * 112 на экране и так виден сразу (ТЗ §9.3).
 */
export const TIMEOUTS = { default: 20_000, login: 15_000, help: 8_000 };

interface RequestOptions {
  body?: unknown;
  /** Публичный путь без сессии (`security: []`): вход, `/help/contacts`. */
  public?: boolean;
  /** Операция — для текста ошибки (общие коды контракта значат разное на разных путях). */
  op: Op;
  timeoutMs?: number;
  /** Проверка вида успешного ответа: тело не того вида — `ApiError` `bad_response`, а не падение экрана. */
  check?: (data: unknown) => boolean;
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
  if (!h || !/^\d+$/.test(h.trim())) return undefined;
  const n = Number.parseInt(h, 10);
  return Number.isFinite(n) && n > 0 ? n : undefined;
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isStr = (v: unknown) => typeof v === "string";

const checks = {
  session: (v: unknown) => isObject(v) && isStr(v.access_token) && isObject(v.me),
  me: (v: unknown) => isObject(v) && isStr(v.id),
  couple: (v: unknown) => isObject(v) && isStr(v.status) && Array.isArray(v.members),
  inviteCreated: (v: unknown) => isObject(v) && isStr(v.link) && isStr(v.expires_at),
  invitePreview: (v: unknown) => isObject(v) && isStr(v.expires_at),
  helpContacts: (v: unknown) => Array.isArray(v),
};

class Timeout extends Error {}

/**
 * Клиент API по контракту `api/openapi.yaml` (сервер `/api/v1`).
 *
 * - Токен сессии живёт только в памяти этого объекта: ни localStorage, ни sessionStorage, ни cookie.
 * - 401 на защищённом пути — один повторный вход по initData и один повтор запроса (истёк токен).
 *   Одновременные 401 ждут один общий вход; токен заменяется только после успешного входа.
 * - 409 `conflict_retry` — ничего не изменено (АР-24), запрос повторяется один раз.
 * - 422, 429 (`Retry-After`), 5xx, обрыв связи, превышение срока ответа и 2xx с телом не того вида —
 *   `ApiError` с понятным текстом.
 * - Тело ответа с ошибкой не журналируется; клиент не пишет в консоль вообще.
 */
export class Api {
  private token: string | null = null;
  private loginInFlight: Promise<Me> | null = null;
  private readonly initData: () => string;
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly timeouts: typeof TIMEOUTS;

  constructor(opts: ApiOptions) {
    this.initData = opts.initData;
    this.baseUrl = opts.baseUrl ?? "/api/v1";
    this.fetchImpl = opts.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.timeouts = { ...TIMEOUTS, ...opts.timeouts };
  }

  get hasSession(): boolean {
    return this.token !== null;
  }

  /** Вход по initData. Если вход уже идёт — тот же промис (один `POST /auth/telegram` на всех). */
  login(): Promise<Me> {
    if (!this.loginInFlight) {
      this.loginInFlight = this.doLogin().finally(() => {
        this.loginInFlight = null;
      });
    }
    return this.loginInFlight;
  }

  private async doLogin(): Promise<Me> {
    const body: AuthRequest = { init_data: this.initData() };
    let session: Session;
    try {
      session = await this.send<Session>("POST", "/auth/telegram", {
        body,
        public: true,
        op: "login",
        timeoutMs: this.timeouts.login,
        check: checks.session,
      });
    } catch (e) {
      this.token = null;
      throw e;
    }
    this.token = session.access_token;
    return session.me;
  }

  me(): Promise<Me> {
    return this.call<Me>("GET", "/me", { op: "me", check: checks.me });
  }

  saveOnboarding(req: OnboardingRequest): Promise<Me> {
    return this.call<Me>("PUT", "/me/onboarding", { body: req, op: "onboarding", check: checks.me });
  }

  /** Текущая пара; `null` — пары нет (404). */
  async couple(): Promise<Couple | null> {
    try {
      return await this.call<Couple>("GET", "/couple", { op: "couple", check: checks.couple });
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null;
      throw e;
    }
  }

  createCouple(): Promise<InviteCreated> {
    return this.call<InviteCreated>("POST", "/couple", { op: "createCouple", check: checks.inviteCreated });
  }

  reissueInvite(): Promise<InviteCreated> {
    return this.call<InviteCreated>("POST", "/couple/invite", { op: "reissueInvite", check: checks.inviteCreated });
  }

  invite(token: string): Promise<InvitePreview> {
    return this.call<InvitePreview>("GET", `/invites/${encodeURIComponent(token)}`, {
      op: "invite",
      check: checks.invitePreview,
    });
  }

  acceptInvite(token: string): Promise<Couple> {
    return this.call<Couple>("POST", `/invites/${encodeURIComponent(token)}/accept`, {
      op: "acceptInvite",
      check: checks.couple,
    });
  }

  helpContacts(): Promise<HelpContact[]> {
    return this.call<HelpContact[]>("GET", "/help/contacts", {
      public: true,
      op: "help",
      timeoutMs: this.timeouts.help,
      check: checks.helpContacts,
    });
  }

  private async call<T>(method: string, path: string, opts: RequestOptions): Promise<T> {
    const tokenBefore = this.token;
    try {
      return await this.sendWithConflictRetry<T>(method, path, opts);
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 401 || opts.public) throw e;
      // Токен истёк или сервер перезапущен: один повторный вход и один повтор запроса. Если вход уже
      // идёт или токен успели обновить, пока шёл этот запрос, — новый вход не начинается.
      if (this.loginInFlight) await this.loginInFlight;
      else if (this.token === null || this.token === tokenBefore) await this.login();
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
      if (!this.token) throw new ApiError(401, "unauthorized", errorText("unauthorized", undefined, undefined, opts.op));
      headers.Authorization = `Bearer ${this.token}`;
    }
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Срок ответа: запрос прерывается, а если реализация fetch сигнал не слушает — ожидание всё равно
    // заканчивается. Под срок попадает и чтение тела.
    const deadline = new Promise<never>((_res, rej) => {
      timer = setTimeout(() => {
        ctrl.abort();
        rej(new Timeout());
      }, opts.timeoutMs ?? this.timeouts.default);
    });
    let res: Response;
    let text: string;
    try {
      [res, text] = await Promise.race([
        (async () => {
          const r = await this.fetchImpl(this.baseUrl + path, {
            method,
            headers,
            body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
            credentials: "omit",
            cache: "no-store",
            signal: ctrl.signal,
          });
          return [r, r.status === 204 ? "" : await r.text()] as const;
        })(),
        deadline,
      ]);
    } catch (e) {
      const code = e instanceof Timeout || ctrl.signal.aborted ? "timeout" : "network";
      throw new ApiError(0, code, errorText(code, undefined, undefined, opts.op));
    } finally {
      clearTimeout(timer);
    }

    let data: unknown = undefined;
    let parsed = false;
    if (text.length > 0) {
      try {
        data = JSON.parse(text);
        parsed = true;
      } catch {
        parsed = false;
      }
    }

    if (res.ok) {
      // Пустое тело по контракту — только у 204; остальные 2xx обязаны прислать JSON нужного вида.
      if (res.status === 204) return undefined as T;
      if (!parsed || (opts.check && !opts.check(data))) {
        throw new ApiError(res.status, "bad_response", errorText("bad_response", undefined, undefined, opts.op));
      }
      return data as T;
    }
    const retryAfter = parseRetryAfter(res.headers.get("Retry-After"));
    if (isErrorBody(data)) {
      throw new ApiError(res.status, data.code, errorText(data.code, data.message, retryAfter, opts.op), retryAfter);
    }
    const code =
      res.status === 401 ? "unauthorized" : res.status === 429 ? "rate_limited" : res.status >= 500 ? "internal" : "http_error";
    throw new ApiError(res.status, code, errorText(code, undefined, retryAfter, opts.op), retryAfter);
  }
}
