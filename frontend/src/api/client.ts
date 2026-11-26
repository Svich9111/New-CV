import { BadEvent, SseParser, isChatHistory, parseEvent, type AcceptedEvent, type CrisisEvent, type RawEvent } from "./chat";
import { isCoupleFeed, isDraft, isMessageOriginal, isPartnerMessage, parseDraftOutcome, type DraftResult } from "./couple";
import { isMemoryList } from "./memory";
import { errorText, type Op } from "./messages";
import type {
  ApiErrorBody,
  AuthRequest,
  ChatErrorCode,
  ChatHistory,
  ChatSendRequest,
  ChatStreamDone,
  Couple,
  CoupleFeed,
  Draft,
  ExportText,
  HelpContact,
  InviteCreated,
  InvitePreview,
  Me,
  MemoryItem,
  MemoryList,
  OnboardingRequest,
  PartnerMessage,
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
export const TIMEOUTS = {
  default: 20_000,
  login: 15_000,
  help: 8_000,
  /**
   * Чат: до ответа сервера (заголовков потока) — сначала сохранение реплики и классификатор безопасности
   * (АР-30), поэтому дольше обычного.
   */
  chatStart: 45_000,
  /** Чат: тишина в потоке между событиями. Сервер шлёт комментарий-пинг раз в 15 с — три пропуска подряд = обрыв. */
  chatIdle: 45_000,
  /** Черновик и «другой вариант»: ответ модели (схема, проверка безопасности) — дольше обычного. */
  draft: 60_000,
};

/** Чем закончился поток ответа агента (завершающее событие контракта). */
export type ChatOutcome =
  | { kind: "done"; data: ChatStreamDone }
  | { kind: "crisis"; data: CrisisEvent }
  | { kind: "error"; code: ChatErrorCode };

export interface ChatSendOptions {
  /** Отмена человеком («Остановить»): поток закрывается, сервер отменяет вызовы модели. */
  signal?: AbortSignal;
  /** Очередной кусок ответа агента (`delta`). */
  onDelta(text: string): void;
  /**
   * Ключ идемпотентности реплики (АР-51): новый на каждую новую реплику, тот же — при её повторе. С ключом
   * первое событие потока — `accepted` (до него `delta`/`done` — поток не по контракту, `bad_response`).
   */
  clientMessageId?: string;
  /** Реплика владельца принята (`accepted`): её id — для сверки по id, а не по тексту. */
  onAccepted?(a: AcceptedEvent): void;
}

interface RequestOptions {
  body?: unknown;
  /** Публичный путь без сессии (`security: []`): вход, `/help/contacts`. */
  public?: boolean;
  /** Операция — для текста ошибки (общие коды контракта значат разное на разных путях). */
  op: Op;
  timeoutMs?: number;
  /** Проверка вида успешного ответа: тело не того вида — `ApiError` `bad_response`, а не падение экрана. */
  check?: (data: unknown, status: number) => boolean;
  /**
   * Контракт объявляет у пути ответ 204 без тела. Без этого флага 204 — `bad_response` (A8-1.5-D5): ни один
   * путь задачи 1.5 204 не отдаёт.
   */
  allowNoContent?: boolean;
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
const isStr = (v: unknown): v is string => typeof v === "string";
const isBool = (v: unknown): v is boolean => typeof v === "boolean";
/** Необязательное поле: нет его — или он нужного вида. */
const opt = (v: unknown, ok: (x: unknown) => boolean) => v === undefined || ok(v);
const strOrNull = (v: unknown) => v === null || isStr(v);

/** `Me` по контракту: экраны выбирают первый экран по двум булевым полям и подставляют имена в форму. */
function isMe(v: unknown): boolean {
  return (
    isObject(v) &&
    isStr(v.id) &&
    isBool(v.onboarding_completed) &&
    isBool(v.has_active_couple) &&
    opt(v.name, strOrNull) &&
    opt(v.address_form, strOrNull) &&
    opt(v.partner_name, strOrNull) &&
    opt(v.timezone, isStr)
  );
}

const COUPLE_STATUS = ["pending", "active", "dissolved"];
const MEMBER_ROLE = ["initiator", "partner"];

function isCoupleMember(v: unknown): boolean {
  return isObject(v) && isStr(v.role) && MEMBER_ROLE.includes(v.role) && isBool(v.is_me) && opt(v.name, strOrNull);
}

/**
 * Контакт помощи по контракту (`HelpContact`): строковые `name`, непустой `phone`, `description`;
 * `round_the_clock` — boolean или нет поля. Неверные записи экран «Помощь» отбрасывает, а не падает (A8-1.5-D7).
 */
export function isHelpContact(v: unknown): v is HelpContact {
  return (
    isObject(v) &&
    isStr(v.name) &&
    isStr(v.phone) &&
    v.phone.trim().length > 0 &&
    isStr(v.description) &&
    opt(v.round_the_clock, isBool)
  );
}

/** Проверки вида успешного ответа по `api/openapi.yaml` — для полей, которые читают экраны 1.5. */
const checks = {
  session: (v: unknown) => isObject(v) && isStr(v.access_token) && v.access_token.length > 0 && isMe(v.me),
  me: isMe,
  couple: (v: unknown) =>
    isObject(v) &&
    isStr(v.id) &&
    isStr(v.status) &&
    COUPLE_STATUS.includes(v.status) &&
    Array.isArray(v.members) &&
    v.members.length <= 2 &&
    v.members.every(isCoupleMember),
  inviteCreated: (v: unknown) => isObject(v) && isStr(v.couple_id) && isStr(v.link) && isStr(v.expires_at),
  // `inviter_name` обязателен и бывает `null` (имя не указано) — но не объектом и не числом (A8-1.5-D8).
  invitePreview: (v: unknown) => isObject(v) && "inviter_name" in v && strOrNull(v.inviter_name) && isStr(v.expires_at),
  // Сам список — массив; отдельные записи проверяет экран «Помощь» (`isHelpContact`) и отбрасывает неверные.
  helpContacts: (v: unknown) => Array.isArray(v),
  chatHistory: isChatHistory,
  memoryList: isMemoryList,
};

class Timeout extends Error {}
class Aborted extends Error {}

interface Opened {
  res: Response;
  ctrl: AbortController;
  release: () => void;
}

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

  /**
   * Страница истории личного чата (от новых к старым). Курсор — непрозрачная строка сервера; 422 по курсору
   * (в том числе после смены ключа сервера, Б-62) разбирает вызывающий: история начинается с первой страницы.
   */
  chatHistory(opts: { cursor?: string | null; limit?: number } = {}): Promise<ChatHistory> {
    const q = new URLSearchParams();
    if (opts.limit !== undefined) q.set("limit", String(opts.limit));
    if (opts.cursor) q.set("cursor", opts.cursor);
    const qs = q.toString();
    return this.call<ChatHistory>("GET", "/chat/messages" + (qs ? `?${qs}` : ""), {
      op: "chatHistory",
      check: checks.chatHistory,
    });
  }

  /** Все свои записи памяти одним запросом (ФТ-13); группирует экран. Ответ не по схеме — `bad_response`. */
  async memoryList(): Promise<MemoryItem[]> {
    const list = await this.call<MemoryList>("GET", "/memory", { op: "memoryList", check: checks.memoryList });
    return list.items;
  }

  /**
   * Удалить запись памяти (`DELETE /memory/{id}`, тело пустое). `deleted` — 204; `gone` — 404: запись уже удалена
   * (например, с другого устройства) или недоступна — на экране её больше быть не должно. 409 `conflict_retry` —
   * один повтор (общее правило клиента), 401 — один повторный вход; остальное — `ApiError`.
   */
  async memoryDelete(id: string): Promise<"deleted" | "gone"> {
    try {
      await this.call<undefined>("DELETE", `/memory/${encodeURIComponent(id)}`, {
        op: "memoryDelete",
        allowNoContent: true,
        // Контракт объявляет только 204: другой успешный ответ — `bad_response`, а не «удалено».
        check: () => false,
      });
      return "deleted";
    } catch (e) {
      // «Уже удалено» — только 404 API с кодом `not_found` (контракт `NotFound`); 404 прокси или чужой страницы —
      // ошибка: запись на сервере может быть цела (A8-2).
      if (e instanceof ApiError && e.status === 404 && e.code === "not_found") return "gone";
      throw e;
    }
  }

  /**
   * Черновик сообщения партнёру по текущему разговору (`POST /drafts`, ФТ-07). 201 — `Draft`, 200 — кризисный
   * протокол (`DraftCrisis`, черновика нет, ТЗ §9.3); статус и `kind` должны совпасть (201 с `kind: crisis` и 200 с
   * `kind: draft` — `bad_response`). `replyTo` — id входящего, на которое пишется ответ («Ответить с агентом»).
   * Ничего не отправляет партнёру: отправка — только `draftSend` по нажатию «Отправить» (ТЗ §2 п.2).
   */
  async draftCreate(replyTo: string | null = null): Promise<DraftResult> {
    let result: DraftResult | null = null;
    await this.call<unknown>("POST", "/drafts", {
      body: replyTo ? { reply_to_id: replyTo } : {},
      op: "draftCreate",
      timeoutMs: this.timeouts.draft,
      check: (data, status) => {
        result = parseDraftOutcome(data, isHelpContact);
        return result !== null && (status === 201 ? result.kind === "draft" : status === 200 && result.kind === "crisis");
      },
    });
    return result as unknown as DraftResult;
  }

  /** Свой черновик (`GET /drafts/{id}`) — перечитать после 409 `version_conflict`. */
  draftGet(id: string): Promise<Draft> {
    return this.call<Draft>("GET", `/drafts/${encodeURIComponent(id)}`, { op: "draftGet", check: isDraft });
  }

  /** Правка автора (`PATCH /drafts/{id}`): текст как есть и версия, которую автор правил. Успех — версия +1. */
  draftPatch(id: string, text: string, expectedVersion: number): Promise<Draft> {
    return this.call<Draft>("PATCH", `/drafts/${encodeURIComponent(id)}`, {
      body: { text, expected_version: expectedVersion },
      op: "draftPatch",
      check: isDraft,
    });
  }

  /** «Другой вариант» (`POST /drafts/{id}/regenerate`): 200 `Draft` (версия +1) или кризис (прежний не изменён). */
  async draftRegenerate(id: string, wish?: string): Promise<DraftResult> {
    let result: DraftResult | null = null;
    await this.call<unknown>("POST", `/drafts/${encodeURIComponent(id)}/regenerate`, {
      body: wish ? { wish } : {},
      op: "draftRegenerate",
      timeoutMs: this.timeouts.draft,
      check: (data, status) => {
        result = parseDraftOutcome(data, isHelpContact);
        return status === 200 && result !== null;
      },
    });
    return result as unknown as DraftResult;
  }

  /** «Не отправлять» (`POST /drafts/{id}/reject`): 204, повтор для отклонённого — тоже 204. */
  async draftReject(id: string): Promise<void> {
    await this.call<undefined>("POST", `/drafts/${encodeURIComponent(id)}/reject`, {
      op: "draftReject",
      allowNoContent: true,
      check: () => false,
    });
  }

  /**
   * «Отправить» (`POST /drafts/{id}/send`) — единственный путь сообщения к партнёру (ФТ-08). `expectedVersion` —
   * версия, которую автор видел на экране. Успех — только 202 `PartnerMessage` своего сообщения в `processing`.
   * Вызывает его только экран черновика по нажатию «Отправить».
   */
  draftSend(id: string, expectedVersion: number): Promise<PartnerMessage> {
    return this.call<PartnerMessage>("POST", `/drafts/${encodeURIComponent(id)}/send`, {
      body: { expected_version: expectedVersion },
      op: "draftSend",
      check: (data, status) =>
        status === 202 && isPartnerMessage(data) && data.direction === "outgoing" && data.status === "processing",
    });
  }

  /**
   * Страница ленты пары (`GET /couple/messages`, от новых к старым). 422 по курсору разбирает вызывающий: лента
   * начинается с первой страницы.
   */
  coupleMessages(opts: { cursor?: string | null; limit?: number } = {}): Promise<CoupleFeed> {
    const q = new URLSearchParams();
    if (opts.limit !== undefined) q.set("limit", String(opts.limit));
    if (opts.cursor) q.set("cursor", opts.cursor);
    const qs = q.toString();
    return this.call<CoupleFeed>("GET", "/couple/messages" + (qs ? `?${qs}` : ""), { op: "coupleMessages", check: isCoupleFeed });
  }

  /** «Показать оригинал» (`GET /couple/messages/{id}/original`, ФТ-10): получателю — с фиксацией просмотра. */
  async messageOriginal(id: string): Promise<ExportText> {
    const r = await this.call<{ text: ExportText }>("GET", `/couple/messages/${encodeURIComponent(id)}/original`, {
      op: "messageOriginal",
      check: isMessageOriginal,
    });
    return r.text;
  }

  /** Отметить полученное прочитанным (`POST /couple/messages/{id}/read`): 204, повтор — тоже 204. */
  async messageRead(id: string): Promise<void> {
    await this.call<undefined>("POST", `/couple/messages/${encodeURIComponent(id)}/read`, {
      op: "messageRead",
      allowNoContent: true,
      check: () => false,
    });
  }

  /**
   * Реплика владельца и потоковый ответ агента (`POST /chat/messages`, SSE). До потока — те же правила, что у
   * остальных запросов: 401 — один повторный вход и один повтор, 409 `conflict_retry` — один немедленный повтор
   * (тем же телом, с тем же ключом), прочие коды — `ApiError` (429 — с `Retry-After`). С ключом 409 значит и
   * «ответ на эту реплику ещё готовит другой запрос» (АР-51) — что делать дальше, решает `ChatSession`.
   *
   * После ответа 200 реплика владельца уже сохранена (контракт). Ошибки потока — `ApiError` со статусом 200:
   * `aborted` (остановил человек), `stream_broken` (соединение закрылось без завершающего события),
   * `stream_timeout` (тишина дольше `chatIdle`), `bad_response` (событие не по контракту). Отмена до ответа —
   * `ApiError(0, "aborted")`: сохранена ли реплика, неизвестно.
   */
  async chatSend(text: string, opts: ChatSendOptions): Promise<ChatOutcome> {
    const body: ChatSendRequest = opts.clientMessageId ? { text, client_message_id: opts.clientMessageId } : { text };
    const open = async () => {
      try {
        return await this.openStream("/chat/messages", body, opts.signal);
      } catch (e) {
        if (e instanceof ApiError && e.code === "conflict_retry") return this.openStream("/chat/messages", body, opts.signal);
        throw e;
      }
    };
    const tokenBefore = this.token;
    let opened: Opened;
    try {
      opened = await open();
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 401) throw e;
      if (this.loginInFlight) await this.loginInFlight;
      else if (this.token === null || this.token === tokenBefore) await this.login();
      opened = await open();
    }
    return this.readStream(opened, opts);
  }

  private async openStream(path: string, body: unknown, signal?: AbortSignal): Promise<Opened> {
    const op: Op = "chatSend";
    if (!this.token) throw new ApiError(401, "unauthorized", errorText("unauthorized", undefined, undefined, op));
    if (signal?.aborted) throw new ApiError(0, "aborted", errorText("aborted", undefined, undefined, op));
    const ctrl = new AbortController();
    const onAbort = () => ctrl.abort();
    signal?.addEventListener("abort", onAbort);
    const release = () => signal?.removeEventListener("abort", onAbort);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const deadline = new Promise<never>((_res, rej) => {
      timer = setTimeout(() => {
        ctrl.abort();
        rej(new Timeout());
      }, this.timeouts.chatStart);
      // Отмена человеком не ждёт, послушает ли реализация fetch сигнал.
      signal?.addEventListener("abort", () => rej(new Aborted()), { once: true });
    });
    deadline.catch(() => undefined);
    let res: Response;
    try {
      res = await Promise.race([
        this.fetchImpl(this.baseUrl + path, {
          method: "POST",
          headers: {
            Accept: "text/event-stream",
            "Content-Type": "application/json",
            Authorization: `Bearer ${this.token}`,
          },
          body: JSON.stringify(body),
          credentials: "omit",
          cache: "no-store",
          signal: ctrl.signal,
        }),
        deadline,
      ]);
      if (!res.ok) {
        const text = await Promise.race([res.text(), deadline]);
        clearTimeout(timer);
        release();
        throw this.statusError(res, text, op);
      }
    } catch (e) {
      clearTimeout(timer);
      release();
      if (e instanceof ApiError) throw e;
      const code = signal?.aborted ? "aborted" : e instanceof Timeout || ctrl.signal.aborted ? "timeout" : "network";
      throw new ApiError(0, code, errorText(code, undefined, undefined, op));
    }
    clearTimeout(timer);
    const type = res.headers.get("Content-Type") ?? "";
    if (!/^text\/event-stream\b/i.test(type.trim())) {
      release();
      ctrl.abort();
      throw new ApiError(res.status, "bad_response", errorText("bad_response", undefined, undefined, op));
    }
    return { res, ctrl, release };
  }

  private async readStream({ res, ctrl, release }: Opened, opts: ChatSendOptions): Promise<ChatOutcome> {
    const op: Op = "chatSend";
    const fail = (code: string) => new ApiError(200, code, errorText(code, undefined, undefined, op));
    let idle: ReturnType<typeof setTimeout> | undefined;
    let stop: (e: Error) => void = () => undefined;
    const stopped = new Promise<never>((_res, rej) => {
      stop = rej;
    });
    stopped.catch(() => undefined);
    const onAbort = () => stop(new Aborted());
    opts.signal?.addEventListener("abort", onAbort);
    const touch = () => {
      clearTimeout(idle);
      idle = setTimeout(() => stop(new Timeout()), this.timeouts.chatIdle);
    };
    const parser = new SseParser();
    const decoder = new TextDecoder();
    let outcome: ChatOutcome | null = null;
    const key = opts.clientMessageId;
    // С ключом `accepted` — первое и единственное (АР-51). Кризис принимается и без него: помощь — всегда (§9.3).
    let accepted = !key;
    const handle = (events: RawEvent[]) => {
      for (const raw of events) {
        if (outcome) return;
        // Без ключа события `accepted` по контракту нет — незнакомое для этого запроса, пропускается.
        if (raw.event === "accepted" && !key) continue;
        const ev = parseEvent(raw, isHelpContact);
        if (!ev) continue;
        if (ev.event === "accepted") {
          if (accepted || ev.data.clientMessageId !== key) throw new BadEvent();
          accepted = true;
          opts.onAccepted?.(ev.data);
          continue;
        }
        if (!accepted && (ev.event === "delta" || ev.event === "done")) throw new BadEvent();
        if (ev.event === "delta") opts.onDelta(ev.text);
        else if (ev.event === "done") outcome = { kind: "done", data: ev.data };
        else if (ev.event === "crisis") outcome = { kind: "crisis", data: ev.data };
        else outcome = { kind: "error", code: ev.code };
      }
    };
    const reader = res.body?.getReader() ?? null;
    try {
      if (opts.signal?.aborted) throw new Aborted();
      touch();
      if (reader) {
        while (!outcome) {
          const r = await Promise.race([reader.read(), stopped]);
          if (r.done) break;
          touch();
          handle(parser.push(decoder.decode(r.value, { stream: true })));
        }
        if (!outcome) handle([...parser.push(decoder.decode()), ...parser.end()]);
      } else {
        // Реализация fetch без потокового тела: ответ разбирается целиком, когда соединение закроется.
        const text = await Promise.race([res.text(), stopped]);
        handle([...parser.push(text), ...parser.end()]);
      }
      if (!outcome) throw fail("stream_broken");
      return outcome;
    } catch (e) {
      if (e instanceof ApiError) throw e;
      if (e instanceof BadEvent) throw fail("bad_response");
      if (e instanceof Timeout) throw fail("stream_timeout");
      if (e instanceof Aborted || opts.signal?.aborted) throw fail("aborted");
      throw fail("stream_broken");
    } finally {
      clearTimeout(idle);
      opts.signal?.removeEventListener("abort", onAbort);
      release();
      if (!outcome) ctrl.abort();
      if (reader) void reader.cancel().catch(() => undefined);
    }
  }

  private statusError(res: Response, text: string, op: Op): ApiError {
    let data: unknown;
    try {
      data = text.length > 0 ? JSON.parse(text) : undefined;
    } catch {
      data = undefined;
    }
    const retryAfter = parseRetryAfter(res.headers.get("Retry-After"));
    if (isErrorBody(data)) return new ApiError(res.status, data.code, errorText(data.code, data.message, retryAfter, op), retryAfter);
    const code =
      res.status === 401 ? "unauthorized" : res.status === 429 ? "rate_limited" : res.status >= 500 ? "internal" : "http_error";
    return new ApiError(res.status, code, errorText(code, undefined, retryAfter, op), retryAfter);
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
      // Пустое тело — только у 204 и только там, где контракт его объявляет; остальные 2xx обязаны прислать
      // JSON нужного вида.
      if (res.status === 204) {
        if (opts.allowNoContent) return undefined as T;
        throw new ApiError(res.status, "bad_response", errorText("bad_response", undefined, undefined, opts.op));
      }
      if (!parsed || (opts.check && !opts.check(data, res.status))) {
        throw new ApiError(res.status, "bad_response", errorText("bad_response", undefined, undefined, opts.op));
      }
      return data as T;
    }
    throw this.statusError(res, text, opts.op);
  }
}
