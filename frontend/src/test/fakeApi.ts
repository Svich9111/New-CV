import type {
  ApiErrorBody,
  ChatHistory,
  ChatMessage,
  Couple,
  HelpContact,
  InviteCreated,
  InvitePreview,
  Me,
  OnboardingRequest,
  Session,
} from "../api/types";
import { INIT_DATA } from "./fakeTelegram";

/**
 * Заглушка сервера по контракту `api/openapi.yaml` (пути, коды, схемы — через сгенерированные типы).
 * Один пользователь «я» и выдуманный партнёр; данные выдуманные.
 */

export interface Logged {
  method: string;
  path: string;
  /** Строка запроса (`?limit=…&cursor=…`), если есть. */
  query: string;
  auth: string | null;
  body: unknown;
}

interface Injected {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
  times: number;
}

interface InviteRow {
  inviter_name: string | null;
  expires_at: string;
  status: "active" | "used" | "expired" | "revoked";
  own: boolean;
}

export const TOKEN_A = "A".repeat(43);
export const TOKEN_B = "B".repeat(43);
export const PARTNER_TOKEN = "Pk3_xY-".padEnd(43, "q");
const EXPIRES = "2026-12-03T12:00:00Z";

export const CONTACTS: HelpContact[] = [
  {
    name: "Единый номер экстренных служб",
    phone: "112",
    description: "Если есть угроза жизни или здоровью.",
    round_the_clock: true,
  },
];

function err(code: string, message: string): ApiErrorBody {
  return { code, message };
}

export class FakeServer {
  me: Me = {
    id: "00000000-0000-4000-8000-000000000001",
    name: null,
    address_form: null,
    partner_name: null,
    timezone: "Europe/Moscow",
    onboarding_completed: false,
    has_active_couple: false,
  };
  couple: Couple | null = null;
  invites = new Map<string, InviteRow>();
  log: Logged[] = [];
  token: string | null = null;
  private tokenSeq = 0;
  private inviteSeq = 0;
  private injected = new Map<string, Injected>();

  /** Пользователь уже прошёл онбординг. */
  onboarded(): this {
    Object.assign(this.me, { name: "Аня", address_form: "Аня", partner_name: "Игорь", onboarding_completed: true });
    return this;
  }

  /** Входящее приглашение от партнёра. */
  partnerInvite(status: InviteRow["status"] = "active", inviter: string | null = "Игорь"): this {
    this.invites.set(PARTNER_TOKEN, { inviter_name: inviter, expires_at: EXPIRES, status, own: false });
    return this;
  }

  /** Партнёр принял моё приглашение (сделал это на своём телефоне). */
  partnerAccepts(): void {
    if (!this.couple) throw new Error("no couple");
    this.couple = {
      ...this.couple,
      status: "active",
      members: [...this.couple.members, { role: "partner", name: "Игорь", is_me: false }],
    };
    this.me.has_active_couple = true;
  }

  pendingCouple(): this {
    this.couple = { id: "c0000000-0000-4000-8000-000000000001", status: "pending", members: [{ role: "initiator", name: this.me.name ?? null, is_me: true }] };
    return this;
  }

  /** Следующие `times` запросов `method path` получат этот ответ. */
  inject(method: string, path: string, status: number, body?: unknown, headers?: Record<string, string>, times = 1): this {
    this.injected.set(`${method} ${path}`, { status, body, headers, times });
    return this;
  }

  /** Следующие `times` запросов `method path` обрываются до ответа (нет связи); сервер их не получил. */
  failNetwork(method: string, path: string, times = 1): this {
    this.netFail.set(`${method} ${path}`, times);
    return this;
  }

  private netFail = new Map<string, number>();

  expireSession(): void {
    this.token = null;
  }

  calls(method: string, path: string): Logged[] {
    return this.log.filter((l) => l.method === method && l.path === path);
  }

  fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://app.test");
    const method = (init.method ?? "GET").toUpperCase();
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const headers = new Headers(init.headers);
    const body = typeof init.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
    this.log.push({ method, path, query: url.search, auth: headers.get("Authorization"), body });

    const nf = this.netFail.get(`${method} ${path}`) ?? 0;
    if (nf > 0) {
      this.netFail.set(`${method} ${path}`, nf - 1);
      throw new TypeError("Failed to fetch");
    }
    const inj = this.injected.get(`${method} ${path}`);
    if (inj) {
      if (--inj.times <= 0) this.injected.delete(`${method} ${path}`);
      return json(inj.status, inj.body, inj.headers);
    }
    return this.route(method, path, headers.get("Authorization"), body, url, init.signal ?? null);
  };

  private route(method: string, path: string, auth: string | null, body: unknown, url: URL, signal: AbortSignal | null): Response {
    if (method === "POST" && path === "/auth/telegram") {
      const b = body as { init_data?: unknown };
      if (b?.init_data !== INIT_DATA) return json(401, err("invalid_init_data", "Не удалось подтвердить вход через Telegram"));
      this.token = `test-session-${++this.tokenSeq}`;
      const s: Session = { access_token: this.token, expires_in: 3600, me: { ...this.me } };
      return json(200, s);
    }
    if (method === "GET" && path === "/help/contacts") return json(200, CONTACTS);

    if (!this.token || auth !== `Bearer ${this.token}`) return json(401, err("unauthorized", "Нет сессии"));

    if (method === "GET" && path === "/me") return json(200, { ...this.me });
    if (method === "PUT" && path === "/me/onboarding") return this.onboarding(body as OnboardingRequest);

    const inv = path.match(/^\/invites\/([^/]+)(\/accept)?$/);
    if (inv && method === "GET" && !inv[2]) return this.preview(decodeURIComponent(inv[1]));
    if (inv && method === "POST" && inv[2]) {
      if (!this.me.onboarding_completed) return json(403, err("onboarding_required", "Сначала завершите знакомство с сервисом"));
      return this.accept(decodeURIComponent(inv[1]));
    }

    if (!this.me.onboarding_completed) return json(403, err("onboarding_required", "Сначала завершите знакомство с сервисом"));
    if (method === "GET" && path === "/chat/messages") return this.chatHistory(url.searchParams);
    if (method === "POST" && path === "/chat/messages") return this.chatPost(body, signal);
    if (method === "GET" && path === "/couple") return this.couple ? json(200, this.couple) : json(404, err("not_found", "Не найдено"));
    if (method === "POST" && path === "/couple") {
      if (this.couple) return json(409, err("already_in_couple", "У вас уже есть пара"));
      this.pendingCouple();
      return json(201, this.issue());
    }
    if (method === "POST" && path === "/couple/invite") {
      if (!this.couple) return json(404, err("not_found", "Не найдено"));
      if (this.couple.status !== "pending") return json(409, err("couple_not_pending", "Пара уже не ожидает партнёра"));
      return json(201, this.issue());
    }
    return json(404, err("not_found", "Не найдено"));
  }

  // ------------------------------------------------------------------ личный чат (ФТ-05, контракт АР-32)

  /** Личный чат «меня»: от старых к новым (сервер отдаёт от новых к старым). */
  chat: ChatMessage[] = [];
  /** Сценарии ответов на `POST /chat/messages` по очереди; без сценария — короткий ответ из двух кусков. */
  chatReplies: ChatReply[] = [];
  /** Версия ключа курсора: смена — прежние курсоры дают 422 (ротация ключа сервера, Б-62). */
  cursorKey = 1;
  /** Сколько потоков закрыл клиент (отмена/обрыв со стороны клиента). */
  streamsAborted = 0;
  private chatSeq = 0;

  /** Добавить `n` реплик в историю (выдуманные тексты): чередуются владелец и агент. */
  seedChat(n: number): this {
    for (let i = 0; i < n; i++) this.addChat(i % 2 === 0 ? "owner" : "agent", `Реплика ${i + 1}`);
    return this;
  }

  /**
   * Запись истории. По умолчанию — как сохранённая до миграции 0018 (без ключа, без связи, `crisis: false`);
   * `extra` задаёт `client_message_id`, `reply_to`, `crisis` (АР-51).
   */
  addChat(
    author: ChatMessage["author"],
    text: ChatMessage["text"],
    id?: string,
    extra: Partial<Pick<ChatMessage, "client_message_id" | "reply_to" | "crisis">> = {},
  ): ChatMessage {
    const seq = ++this.chatSeq;
    const m: ChatMessage = {
      id: id ?? `00000000-0000-4000-9000-${String(seq).padStart(12, "0")}`,
      author,
      text,
      created_at: new Date(Date.UTC(2026, 10, 20, 10, 0, seq)).toISOString(),
      client_message_id: null,
      reply_to: null,
      crisis: false,
      ...extra,
    };
    this.chat.push(m);
    return m;
  }

  reply(r: ChatReply): this {
    this.chatReplies.push(r);
    return this;
  }

  /** Реплика владельца под ключом (АР-51). */
  ownerByKey(cmid: string): ChatMessage | undefined {
    return this.chat.find((m) => m.author === "owner" && m.client_message_id === cmid);
  }

  /** Ответ агента на реплику `ownerId` (`reply_to`). */
  answerTo(ownerId: string): ChatMessage | undefined {
    return this.chat.find((m) => m.author === "agent" && m.reply_to === ownerId);
  }

  /**
   * Ответ на реплику `ownerId` «готовит другой запрос» (аренда, АР-51): следующие `times` запросов с её ключом
   * получат 409 `conflict_retry` до потока.
   */
  leaseFor(ownerId: string, times: number): this {
    this.leases.set(ownerId, times);
    return this;
  }

  /** Аренда снята (другой запрос закончил). */
  releaseLease(ownerId: string): this {
    this.leases.delete(ownerId);
    return this;
  }

  /** Аренды ответа: идущие потоки (`-1` — до конца потока) и заданные тестом (`leaseFor`, счётчик 409). */
  private leases = new Map<string, number>();

  private chatHistory(q: URLSearchParams): Response {
    const limit = Number(q.get("limit") ?? 50);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) return json(422, err("validation_error", "Проверьте данные"));
    const newest = [...this.chat].reverse();
    let start = 0;
    const cursor = q.get("cursor");
    if (cursor !== null) {
      const m = /^c\.(\d+)\.(\d+)$/.exec(cursor);
      if (!m || Number(m[1]) !== this.cursorKey) return json(422, err("validation_error", "Проверьте данные"));
      const at = newest.findIndex((x) => x.id === this.chatIdBySeq(Number(m[2])));
      start = at < 0 ? newest.length : at + 1;
    }
    const items = newest.slice(start, start + limit);
    const lastItem = items[items.length - 1];
    const more = start + limit < newest.length;
    const page: ChatHistory = {
      items,
      next_cursor: more && lastItem ? `c.${this.cursorKey}.${this.seqOf(lastItem.id)}` : null,
    };
    return json(200, page, { "Cache-Control": "no-store" });
  }

  private seqOf(id: string): number {
    return this.chat.findIndex((x) => x.id === id);
  }

  private chatIdBySeq(i: number): string | undefined {
    return this.chat[i]?.id;
  }

  /**
   * `POST /chat/messages` по контракту АР-32 и АР-51. Без ключа — каждая попытка новая реплика, события
   * `accepted` нет. С ключом: реплики нет — сохраняется с ключом, поток с `accepted` (`replayed: false`);
   * под ключом другой текст — 422; ответ готовится (аренда) — 409; ответ сохранён — поток из сохранённого ответа
   * без сценария; реплика без ответа — ответ на неё по следующему сценарию (`replayed: true`).
   */
  private chatPost(body: unknown, signal: AbortSignal | null): Response {
    const b = body as { text?: unknown; client_message_id?: unknown };
    const extraKeys = Object.keys(b ?? {}).filter((k) => k !== "text" && k !== "client_message_id");
    const cmidOk = b?.client_message_id === undefined || b.client_message_id === null || (typeof b.client_message_id === "string" && UUID.test(b.client_message_id));
    if (typeof b?.text !== "string" || b.text.trim().length === 0 || b.text.length > 4000 || extraKeys.length > 0 || !cmidOk) {
      return json(422, err("validation_error", "Проверьте данные"));
    }
    const text = b.text;
    const cmid = typeof b.client_message_id === "string" ? b.client_message_id : null;
    let owner = cmid ? this.ownerByKey(cmid) : undefined;
    const replayed = owner !== undefined;
    if (owner) {
      if (owner.text !== text) return json(422, err("validation_error", "Проверьте данные"));
      const lease = this.leases.get(owner.id);
      if (lease !== undefined) {
        if (lease > 0) {
          if (lease === 1) this.leases.delete(owner.id);
          else this.leases.set(owner.id, lease - 1);
        }
        return json(409, err("conflict_retry", "Ответ на это сообщение ещё готовится — повторите через несколько секунд"));
      }
      const saved = this.answerTo(owner.id);
      if (saved) return this.stream(replay(saved), signal, owner, { cmid: cmid!, replayed: true });
    }
    const r = this.chatReplies.shift() ?? { steps: [delta("Слышу "), delta("тебя."), done()] };
    const saveOwner = (): ChatMessage => owner ?? (owner = this.addChat("owner", text, undefined, { client_message_id: cmid }));
    if (r.dropResponse) {
      // Сервер сохранил реплику, а ответ до клиента не дошёл (обрыв связи до заголовков).
      const o = saveOwner();
      if (r.dropResponse === "answered") this.addChat("agent", "Ответ, который не дошёл.", undefined, { reply_to: o.id });
      throw new TypeError("Failed to fetch");
    }
    if (r.status && r.status !== 200) {
      // 409 `conflict_retry` — ничего не сохранено; 429/402/5xx — реплика владельца уже сохранена.
      if (r.saveOwner ?? r.status !== 409) saveOwner();
      return json(r.status, r.body, r.headers);
    }
    const o = r.ownerNotSaved && !owner ? null : saveOwner();
    return this.stream(r, signal, o, cmid && !r.noAccepted ? { cmid, replayed } : null);
  }

  private stream(r: ChatReply, signal: AbortSignal | null, owner: ChatMessage | null, accepted: { cmid: string; replayed: boolean } | null): Response {
    const enc = new TextEncoder();
    const steps: SseStep[] = [
      ...(accepted ? [{ event: "accepted", data: { message_id: owner?.id ?? null, client_message_id: accepted.cmid, replayed: accepted.replayed } }] : []),
      ...(r.steps ?? []),
    ];
    const addChat = this.addChat.bind(this);
    const leases = this.leases;
    const replyTo = owner?.id ?? null;
    // Пока поток идёт, ответ на реплику «готовится»: повтор с её ключом получит 409 (АР-51).
    if (owner && !leases.has(owner.id)) leases.set(owner.id, -1);
    const release = () => {
      if (owner && leases.get(owner.id) === -1) leases.delete(owner.id);
    };
    const counter = { abort: () => (this.streamsAborted += 1) };
    let shown = "";
    let cancelled = false;
    // Закрытие клиентом после завершающего события — не отмена (контракт: после него соединение закрывается).
    let terminal = false;
    const onAbort = () => {
      if (!cancelled && !terminal) counter.abort();
      cancelled = true;
      release();
    };
    signal?.addEventListener("abort", onAbort, { once: true });

    // Текст шага; сохранение ответа агента — до отправки `done`/`crisis` (контракт).
    const segment = (st: SseStep): string | null => {
      if ("comment" in st) return `: ${st.comment}\n\n`;
      if ("raw" in st) return st.raw;
      if ("saveAgent" in st) {
        addChat("agent", st.saveAgent, st.id, { reply_to: replyTo });
        return null;
      }
      if ("event" in st) {
        const data = st.data as Record<string, unknown>;
        if (st.event === "delta" && typeof data?.text === "string") shown += data.text;
        if (st.event === "done" || st.event === "crisis" || st.event === "error") terminal = true;
        if (st.event === "done" && typeof data?.message_id === "string" && st.save !== false) addChat("agent", shown, data.message_id, { reply_to: replyTo });
        if (st.event === "crisis" && typeof data?.text === "string" && typeof data?.message_id === "string" && st.save !== false)
          addChat("agent", data.text, data.message_id, { reply_to: replyTo, crisis: true });
        return `event: ${st.event}\ndata: ${typeof st.data === "string" ? st.data : JSON.stringify(st.data)}\n\n`;
      }
      return null;
    };
    const pieces = (text: string): Uint8Array[] => {
      const bytes = enc.encode(text);
      if (typeof r.chunk !== "number") return [bytes];
      const out: Uint8Array[] = [];
      for (let i = 0; i < bytes.length; i += r.chunk) out.push(bytes.slice(i, i + r.chunk));
      return out;
    };

    const body = new ReadableStream<Uint8Array>({
      async start(controller) {
        const fail = () => {
          try {
            controller.error(new DOMException("The operation was aborted.", "AbortError"));
          } catch {
            /* поток уже закрыт */
          }
        };
        signal?.addEventListener("abort", fail, { once: true });
        if (r.chunk === "whole") {
          let all = "";
          for (const st of steps) {
            if ("wait" in st || "gate" in st) continue;
            all += segment(st) ?? "";
          }
          if (!cancelled) controller.enqueue(enc.encode(all));
        } else {
          for (const st of steps) {
            if (cancelled) return;
            if ("wait" in st) {
              await new Promise((res) => setTimeout(res, st.wait));
              continue;
            }
            if ("gate" in st) {
              await st.gate;
              continue;
            }
            const text = segment(st);
            if (text === null) continue;
            for (const p of pieces(text)) {
              if (cancelled) return;
              controller.enqueue(p);
              // Отдельные куски — отдельными чтениями у клиента.
              await new Promise((res) => setTimeout(res, 0));
            }
          }
        }
        if (cancelled) return;
        if (r.end === "hang") return;
        release();
        if (r.end === "error") {
          fail();
          return;
        }
        controller.close();
      },
      cancel() {
        onAbort();
      },
    });
    return new Response(body, {
      status: 200,
      headers: { "Content-Type": r.contentType ?? "text/event-stream; charset=utf-8", "Cache-Control": "no-store" },
    });
  }

  private onboarding(b: OnboardingRequest): Response {
    const okText = (v: unknown) => typeof v === "string" && v.length >= 1 && v.length <= 64;
    if (!okText(b.name) || !okText(b.address_form) || !okText(b.partner_name)) return json(422, err("validation_error", "Проверьте данные"));
    if (b.consent_terms !== true || b.consent_data !== true || b.consent_sensitive !== true) return json(422, err("validation_error", "Проверьте данные"));
    if (b.age_confirmed !== true) {
      this.me.onboarding_completed = false;
      return json(403, err("underage", "Сервис доступен только с 18 лет"));
    }
    Object.assign(this.me, { name: b.name, address_form: b.address_form, partner_name: b.partner_name, onboarding_completed: true });
    if (b.timezone) this.me.timezone = b.timezone;
    return json(200, { ...this.me });
  }

  private issue(): InviteCreated {
    for (const row of this.invites.values()) if (row.own && row.status === "active") row.status = "revoked";
    const token = ++this.inviteSeq === 1 ? TOKEN_A : TOKEN_B;
    this.invites.set(token, { inviter_name: this.me.name ?? null, expires_at: EXPIRES, status: "active", own: true });
    return { couple_id: this.couple!.id, link: `https://t.me/test_bot?startapp=${token}`, expires_at: EXPIRES };
  }

  private preview(token: string): Response {
    const row = this.invites.get(token);
    if (!row) return json(404, err("not_found", "Не найдено"));
    if (row.status === "expired") return json(410, err("invite_expired", "Срок приглашения истёк — попросите новую ссылку"));
    if (row.status !== "active") return json(410, err("invite_used", "Приглашение уже использовано — попросите новую ссылку"));
    const p: InvitePreview = { inviter_name: row.inviter_name, expires_at: row.expires_at };
    return json(200, p);
  }

  private accept(token: string): Response {
    const row = this.invites.get(token);
    if (!row) return json(404, err("not_found", "Не найдено"));
    if (row.status === "expired") return json(410, err("invite_expired", "Срок приглашения истёк — попросите новую ссылку"));
    if (row.status !== "active") return json(410, err("invite_used", "Приглашение уже использовано — попросите новую ссылку"));
    if (row.own) return json(409, err("own_invite", "Это ваше приглашение — отправьте ссылку партнёру"));
    if (this.couple) return json(409, err("already_in_couple", "У вас уже есть пара"));
    row.status = "used";
    this.couple = {
      id: "c0000000-0000-4000-8000-000000000002",
      status: "active",
      members: [
        { role: "initiator", name: row.inviter_name, is_me: false },
        { role: "partner", name: this.me.name ?? null, is_me: true },
      ],
    };
    this.me.has_active_couple = true;
    return json(200, this.couple);
  }
}

export function json(status: number, body?: unknown, headers: Record<string, string> = {}): Response {
  if (status === 204 || body === undefined) return new Response(null, { status, headers });
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

// ------------------------------------------------------------------ сценарии потока чата

export type SseStep =
  | { event: string; data: unknown; save?: boolean }
  | { comment: string }
  | { raw: string }
  | { wait: number }
  | { gate: Promise<void> }
  /** Сервер сохранил ответ агента, но событие до клиента не дошло. */
  | { saveAgent: string; id?: string };

export interface ChatReply {
  /** Реплика сохранена (и, если `answered`, — ответ агента), но ответ до клиента не дошёл. */
  dropResponse?: true | "answered";
  /** Ответ до потока (JSON `Error`): 409/422/429/402/5xx. */
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
  /** Сохраняет ли сервер реплику владельца при ответе `status` (по умолчанию — да, кроме 409). */
  saveOwner?: boolean;
  /** Реплику сохранить не удалось: `accepted` с `message_id: null` (дальше по контракту — `crisis` или `error`). */
  ownerNotSaved?: true;
  /** Поток без `accepted`, хотя запрос был с ключом (не по контракту). */
  noAccepted?: true;
  steps?: SseStep[];
  /** Как резать поток: по шагам (по умолчанию), целиком одним куском или по N байт. */
  chunk?: "whole" | number;
  /** После шагов: закрыть (по умолчанию), держать открытым или оборвать с ошибкой. */
  end?: "close" | "hang" | "error";
  contentType?: string;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Повтор по ключу, ответ уже сохранён: поток из сохранённого ответа (АР-51), без нового сохранения. */
function replay(m: ChatMessage): ChatReply {
  if (typeof m.text !== "string") return { steps: [errorEvent("internal")] };
  if (m.crisis) return { steps: [{ ...crisis(m.text, { id: m.id, replace: false }), save: false }] };
  return { steps: [delta(m.text), { ...done(m.id), save: false }] };
}

let agentSeq = 0;
export const delta = (text: string): SseStep => ({ event: "delta", data: { text } });
export function done(id?: string, suggest_draft = false): SseStep {
  return { event: "done", data: { message_id: id ?? `a0000000-0000-4000-8000-${String(++agentSeq).padStart(12, "0")}`, suggest_draft } };
}
export function crisis(text: string, opts: { replace?: boolean; contacts?: unknown; id?: string | null } = {}): SseStep {
  return {
    event: "crisis",
    data: {
      message_id: opts.id === undefined ? `c0000000-0000-4000-8000-${String(++agentSeq).padStart(12, "0")}` : opts.id,
      text,
      replace: opts.replace ?? false,
      contacts: opts.contacts ?? CONTACTS,
    },
  };
}
export const errorEvent = (code: string, message = "Модель недоступна"): SseStep => ({ event: "error", data: { code, message } });

/** Шлюз: поток ждёт, пока тест не откроет его (`open()`). */
export function gate(): { step: SseStep; open: () => void } {
  let open: () => void = () => undefined;
  const p = new Promise<void>((res) => {
    open = res;
  });
  return { step: { gate: p }, open };
}
