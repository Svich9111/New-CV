import type {
  ApiErrorBody,
  ChatHistory,
  ChatMessage,
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

    // Память — без требования онбординга (принцип 6, АР-17, АР-25): 403 этот путь не отдаёт.
    if (method === "GET" && path === "/memory") return this.memoryList(url.searchParams);
    const mem = path.match(/^\/memory\/([^/]+)$/);
    if (mem && method === "DELETE") return this.memoryDelete(decodeURIComponent(mem[1]));

    if (!this.me.onboarding_completed) return json(403, err("onboarding_required", "Сначала завершите знакомство с сервисом"));
    if (method === "GET" && path === "/chat/messages") return this.chatHistory(url.searchParams);
    if (method === "POST" && path === "/chat/messages") return this.chatPost(body, signal);
    if (path === "/drafts" || path.startsWith("/drafts/")) return this.drafts(method, path, body);
    if (method === "GET" && path === "/couple/messages") return this.feedPage(url.searchParams);
    const cm = path.match(/^\/couple\/messages\/([^/]+)\/(original|read)$/);
    if (cm && method === "GET" && cm[2] === "original") return this.original(decodeURIComponent(cm[1]));
    if (cm && method === "POST" && cm[2] === "read") return this.markRead(decodeURIComponent(cm[1]));
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

  // ------------------------------------------------------------------ память (ФТ-13, ТЗ §6.3)

  /** Записи памяти «меня» (неудалённые), в порядке создания. */
  memory: MemoryItem[] = [];
  /** id удалённых записей: повторный `GET` их не возвращает, повторный `DELETE` — 404 (функция БД, АР-36). */
  deletedMemory = new Set<string>();
  /**
   * Записи партнёра — в том же хранилище заглушки, что и мои, но с другим владельцем (как строки одной таблицы на
   * сервере): «мне» не видны, удалить нельзя — 404 из проверки владельца, неотличимый от несуществующей записи
   * (контракт). Запись при этом цела (MINOR-3 ревью 2.5).
   */
  partnerMemoryItems: MemoryItem[] = [
    {
      id: "30000000-0000-4000-8000-0000000000ff",
      type: "fact",
      text: "Запись партнёра: выдуманный факт",
      event_date: null,
      source: "owner_dialog",
      confidence: 0.9,
      created_at: "2026-11-01T09:00:00.000Z",
    },
  ];
  /** id записей партнёра. */
  get partnerMemory(): Set<string> {
    return new Set(this.partnerMemoryItems.map((m) => m.id));
  }
  private memorySeq = 0;

  /** Запись памяти (выдуманный текст); по умолчанию — факт из диалога с владельцем. */
  addMemory(m: Partial<MemoryItem> = {}): MemoryItem {
    const seq = ++this.memorySeq;
    const item: MemoryItem = {
      id: `30000000-0000-4000-8000-${String(seq).padStart(12, "0")}`,
      type: "fact",
      text: `Запись ${seq}`,
      event_date: null,
      source: "owner_dialog",
      confidence: 0.9,
      created_at: new Date(Date.UTC(2026, 10, seq, 9, 0, 0)).toISOString(),
      ...m,
    };
    this.memory.push(item);
    return item;
  }

  /** Записи всех шести типов ТЗ §6.3 и запись, текст которой не расшифровался (`UnavailableText`). */
  seedMemory(): this {
    this.addMemory({ type: "fact", text: "Работает дизайнером, по вторникам часто задерживается" });
    this.addMemory({ type: "date", text: "Годовщина знакомства", event_date: "2027-06-14" });
    this.addMemory({ type: "preference", text: "Не любит, когда планы меняются в последний момент" });
    this.addMemory({ type: "sensitive_topic", text: "Тяжело переживает ощущение, что она не на первом месте" });
    this.addMemory({ type: "observation", text: "Чаще пишет поздно вечером", source: "agent_inference", confidence: 0.4 });
    this.addMemory({ type: "from_partner", text: "Игорь просил предупреждать об отменах заранее", source: "partner_message", confidence: 0.8 });
    this.addMemory({ type: "fact", text: { unavailable: true, reason: "unknown_key" } });
    return this;
  }

  private memoryList(q: URLSearchParams): Response {
    const type = q.get("type");
    const types = ["fact", "date", "preference", "sensitive_topic", "observation", "from_partner"];
    if (type !== null && !types.includes(type)) return json(422, err("validation_error", "Проверьте данные"));
    const items = this.memory.filter((m) => !this.deletedMemory.has(m.id) && (type === null || m.type === type));
    const body: MemoryList = { items };
    return json(200, body, { "Cache-Control": "no-store" });
  }

  private memoryDelete(id: string): Response {
    // Поиск по всему хранилищу, затем проверка владельца — как `delete_memory_item` на сервере.
    const row =
      this.memory.find((m) => m.id === id) !== undefined
        ? { owner: "me" as const }
        : this.partnerMemoryItems.find((m) => m.id === id) !== undefined
          ? { owner: "partner" as const }
          : null;
    if (row === null || row.owner !== "me" || this.deletedMemory.has(id)) return json(404, err("not_found", "Не найдено"));
    this.deletedMemory.add(id);
    this.memory = this.memory.filter((m) => m.id !== id);
    return json(204, undefined, { "Cache-Control": "no-store" });
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

  // ------------------------------------------------------------------ черновики (ФТ-07, ФТ-08, контракт АР-40)

  /** Черновики «меня» по id. */
  draftRows = new Map<string, Draft>();
  /** Сценарии ответов `POST /drafts` и `regenerate` по очереди; без сценария — выдуманный текст черновика. */
  draftReplies: DraftReply[] = [];
  private draftSeq = 0;

  /** Следующий ответ модели на черновик или «другой вариант». */
  draftReply(r: DraftReply): this {
    this.draftReplies.push(r);
    return this;
  }

  /** Активна ли пара «меня» (иначе черновики и отправка — 409 `couple_not_active`, лента пуста). */
  get coupleActive(): boolean {
    return this.couple?.status === "active" && this.me.has_active_couple;
  }

  /** Черновик «меня» в любом статусе (например, правка «из второй вкладки» — тест меняет текст и версию). */
  addDraft(d: Partial<Draft> = {}): Draft {
    const seq = ++this.draftSeq;
    const draft: Draft = {
      kind: "draft",
      id: `d0000000-0000-4000-8000-${String(seq).padStart(12, "0")}`,
      text: `Черновик ${seq}: мне грустно, когда планы меняются в последний момент. Давай договоримся предупреждать заранее?`,
      reply_to_id: null,
      version: 1,
      status: "draft",
      ...d,
    };
    this.draftRows.set(draft.id, draft);
    return draft;
  }

  private drafts(method: string, path: string, body: unknown): Response {
    const noStore = { "Cache-Control": "no-store" };
    const m = path.match(/^\/drafts(?:\/([^/]+))?(?:\/(regenerate|reject|send))?$/);
    if (!m) return json(404, err("not_found", "Не найдено"));
    const id = m[1] ? decodeURIComponent(m[1]) : null;
    const action = m[2] ?? null;
    const b = (body ?? {}) as Record<string, unknown>;
    const keysOnly = (...allowed: string[]) => Object.keys(b).every((k) => allowed.includes(k));

    if (id === null) {
      if (method !== "POST") return json(404, err("not_found", "Не найдено"));
      if (!keysOnly("reply_to_id") || !(b.reply_to_id === undefined || b.reply_to_id === null || (typeof b.reply_to_id === "string" && UUID.test(b.reply_to_id))))
        return json(422, err("validation_error", "Проверьте данные"));
      if (!this.coupleActive) return json(409, err("couple_not_active", "Пара не активна — отправить сообщение некому"));
      const replyTo = typeof b.reply_to_id === "string" ? b.reply_to_id : null;
      if (replyTo !== null && !this.replyTarget(replyTo)) return json(422, err("bad_reply_target", "Нельзя ответить на это сообщение"));
      if (!this.chat.some((c) => c.author === "owner")) return json(422, err("no_turns", "Сначала расскажите агенту, что хотите передать"));
      const r = this.draftReplies.shift() ?? { kind: "draft" };
      if (r.kind === "status") return json(r.status, r.body);
      if (r.kind === "crisis") {
        return json(200, { kind: "crisis", text: r.text ?? CRISIS_TEXT, contacts: r.contacts ?? CONTACTS, draft_id: null, ...(r.extra ?? {}) }, noStore);
      }
      const d = this.addDraft({ reply_to_id: replyTo, ...(r.text !== undefined ? { text: r.text } : {}) });
      return json(201, d, noStore);
    }

    const row = this.draftRows.get(id);
    if (!row) return json(404, err("not_found", "Не найдено"));
    if (method === "GET" && action === null) return json(200, row, noStore);

    if (method === "PATCH" && action === null) {
      const v = b.expected_version;
      if (!keysOnly("text", "expected_version") || typeof b.text !== "string" || b.text.trim().length === 0 || b.text.length > 4000 || typeof v !== "number" || !Number.isInteger(v) || v < 1)
        return json(422, err("validation_error", "Проверьте данные"));
      if (row.status !== "draft") return json(409, err("not_editable", "Этот черновик уже нельзя изменить"));
      if (v !== row.version) return json(409, err("version_conflict", "Черновик изменился — проверьте текст ещё раз"));
      const next: Draft = { ...row, text: b.text, version: row.version + 1 };
      this.draftRows.set(id, next);
      return json(200, next, noStore);
    }

    if (method !== "POST") return json(404, err("not_found", "Не найдено"));

    if (action === "regenerate") {
      if (!keysOnly("wish") || !(b.wish === undefined || (typeof b.wish === "string" && b.wish.length <= 500)))
        return json(422, err("validation_error", "Проверьте данные"));
      if (row.status !== "draft") return json(409, err("not_editable", "Этот черновик уже нельзя изменить"));
      const r = this.draftReplies.shift() ?? { kind: "draft" };
      if (r.kind === "status") return json(r.status, r.body);
      if (r.kind === "crisis") {
        return json(200, { kind: "crisis", text: r.text ?? CRISIS_TEXT, contacts: r.contacts ?? CONTACTS, draft_id: id, ...(r.extra ?? {}) }, noStore);
      }
      const text = r.text ?? `Другой вариант ${row.version + 1}: мне важно, чтобы мы предупреждали друг друга об изменениях планов.`;
      if (text === row.text) return json(422, err("same_variant", "Другой вариант не получился — попробуйте уточнить пожелание"));
      const next: Draft = { ...row, text, version: row.version + 1 };
      this.draftRows.set(id, next);
      return json(200, next, noStore);
    }

    if (action === "reject") {
      if (row.status === "rejected") return json(204);
      if (row.status !== "draft") return json(409, err("not_editable", "Этот черновик уже нельзя изменить"));
      this.draftRows.set(id, { ...row, status: "rejected" });
      return json(204);
    }

    if (action === "send") {
      const v = b.expected_version;
      if (!keysOnly("expected_version") || typeof v !== "number" || !Number.isInteger(v) || v < 1) return json(422, err("validation_error", "Проверьте данные"));
      if (row.status === "blocked") return json(422, err("blocked_by_safety", "Черновик остановлен проверкой безопасности"));
      if (row.status !== "draft" && row.status !== "approved") return json(409, err("not_editable", "Этот черновик уже нельзя изменить"));
      if (v !== row.version) return json(409, err("version_conflict", "Черновик изменился — проверьте текст ещё раз"));
      if (!this.coupleActive) return json(409, err("couple_not_active", "Пара не активна — отправить сообщение некому"));
      if (row.reply_to_id && !this.replyTarget(row.reply_to_id)) return json(422, err("bad_reply_target", "Нельзя ответить на это сообщение"));
      this.draftRows.set(id, { ...row, status: "sent" });
      const msg = this.addFeed({ author: "me", original: row.text, status: "processing", reply_to_id: row.reply_to_id ?? null, draft_id: id });
      return json(202, this.view(msg));
    }
    return json(404, err("not_found", "Не найдено"));
  }

  /** Ответить можно только на доставленное «мне» сообщение партнёра текущей пары. */
  private replyTarget(id: string): boolean {
    const row = this.feed.find((f) => f.id === id);
    return row !== undefined && row.author === "partner" && ["delivered", "read", "answered"].includes(row.status);
  }

  // ------------------------------------------------------------------ лента пары (ФТ-09…ФТ-11, контракт АР-40, АР-44)

  /** Все сообщения пары обеих сторон (как строки одной таблицы): что видит «я», решает `view`. */
  feed: FeedRow[] = [];
  /** Версия ключа курсора ленты: смена — прежние курсоры дают 422. */
  feedCursorKey = 1;
  private feedSeq = 0;

  /**
   * Сообщение пары. Автор `me` — «моё» отправленное; `partner` — сообщение партнёра «мне». `forRecipient` — версия для
   * получателя (по умолчанию копия оригинала, при `is_adapted` — выдуманная адаптация).
   */
  addFeed(r: Partial<FeedRow> & { author: FeedRow["author"] }): FeedRow {
    const seq = ++this.feedSeq;
    const original: ExportText = r.original ?? `Сообщение ${seq}: давай обсудим выходные.`;
    const row: FeedRow = {
      id: `e0000000-0000-4000-8000-${String(seq).padStart(12, "0")}`,
      original,
      forRecipient: r.is_adapted && typeof original === "string" ? `Адаптировано: ${original}` : original,
      is_adapted: false,
      harsh_tone_warning: false,
      status: "delivered",
      withheld_reason: null,
      withheld_crisis: null,
      withheld_crisis_blocks: null,
      reply_to_id: null,
      draft_id: null,
      sent_at: new Date(Date.UTC(2026, 10, 25, 19, 0, seq)).toISOString(),
      read_at: null,
      originalViewedAt: null,
      ...r,
    };
    this.feed.push(row);
    return row;
  }

  /** Конвейер доставил моё сообщение (A3): версия для получателя, пометка, резкий тон; ответ — исходное `answered`. */
  deliver(id: string, opts: { adapted?: boolean; harsh?: boolean } = {}): void {
    const row = this.feed.find((f) => f.id === id)!;
    Object.assign(row, {
      status: "delivered",
      is_adapted: opts.adapted ?? false,
      harsh_tone_warning: opts.harsh ?? false,
      forRecipient: opts.adapted && typeof row.original === "string" ? `Адаптировано: ${row.original}` : row.original,
    });
    if (row.reply_to_id) {
      const src = this.feed.find((f) => f.id === row.reply_to_id);
      if (src) src.status = "answered";
    }
  }

  /** Конвейер остановил моё сообщение. */
  withhold(
    id: string,
    reason: FeedRow["withheld_reason"],
    crisis: FeedRow["withheld_crisis"] = null,
    blocks: FeedRow["withheld_crisis_blocks"] = null,
  ): void {
    Object.assign(this.feed.find((f) => f.id === id)!, {
      status: "withheld",
      withheld_reason: reason,
      withheld_crisis: crisis,
      withheld_crisis_blocks: blocks,
    });
  }

  /** Оба направления, все статусы, адаптированное и нет, нерасшифрованное (выдуманные тексты). */
  seedFeed(): this {
    this.addFeed({ author: "partner", original: "Ты опять забыла про ужин. Сколько можно.", is_adapted: true, harsh_tone_warning: false, status: "read" });
    this.addFeed({ author: "me", original: "Прости, я правда забыла. Давай в субботу?", status: "answered" });
    this.addFeed({ author: "partner", original: "Ок, суббота подходит.", status: "delivered" });
    return this;
  }

  /** Что видит «я»: своё — оригинал и статус (с причиной недоставки); партнёра — версию для получателя. */
  view(r: FeedRow): PartnerMessage {
    const mine = r.author === "me";
    return {
      id: r.id,
      direction: mine ? "outgoing" : "incoming",
      text: mine ? r.original : r.forRecipient,
      is_adapted: r.is_adapted,
      harsh_tone_warning: r.harsh_tone_warning,
      status: r.status,
      withheld_reason: mine && r.status === "withheld" ? r.withheld_reason : null,
      withheld_crisis: mine && r.status === "withheld" && r.withheld_reason === "safety" ? r.withheld_crisis : null,
      // Набор блоков протокола (АР-57) — только автору при `safety`; null — сообщение до 0021.
      withheld_crisis_blocks: mine && r.status === "withheld" && r.withheld_reason === "safety" ? r.withheld_crisis_blocks : null,
      reply_to_id: r.reply_to_id,
      sent_at: r.sent_at,
      read_at: r.read_at,
      ...(r.extra ?? {}),
    } as PartnerMessage;
  }

  /** Видно ли «мне» сообщение в ленте текущей пары (АР-11, АР-44). */
  private visible(r: FeedRow): boolean {
    if (!this.coupleActive) return false;
    if (r.author === "me") return r.withheld_reason !== "couple_ended";
    return ["delivered", "read", "answered"].includes(r.status);
  }

  private feedPage(q: URLSearchParams): Response {
    const limit = Number(q.get("limit") ?? 50);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) return json(422, err("validation_error", "Проверьте данные"));
    const rows = this.feed
      .filter((r) => this.visible(r))
      .sort((a, b) => (a.sent_at === b.sent_at ? b.id.localeCompare(a.id) : b.sent_at.localeCompare(a.sent_at)));
    let start = 0;
    const cursor = q.get("cursor");
    if (cursor !== null) {
      const m = /^f\.(\d+)\.([0-9a-f-]+)$/.exec(cursor);
      if (!m || Number(m[1]) !== this.feedCursorKey) return json(422, err("validation_error", "Проверьте данные"));
      const at = rows.findIndex((r) => r.id === m[2]);
      start = at < 0 ? rows.length : at + 1;
    }
    const items = rows.slice(start, start + limit);
    const last = items[items.length - 1];
    const page: CoupleFeed = {
      items: items.map((r) => this.view(r)),
      next_cursor: start + limit < rows.length && last ? `f.${this.feedCursorKey}.${last.id}` : null,
    };
    return json(200, page, { "Cache-Control": "no-store" });
  }

  private original(id: string): Response {
    const r = this.feed.find((f) => f.id === id);
    if (!r || !this.visible(r)) return json(404, err("not_found", "Не найдено"));
    // Получателю — с фиксацией первого просмотра; нерасшифрованный текст просмотром не считается; автору — без фиксации.
    if (r.author === "partner" && typeof r.original === "string" && r.originalViewedAt === null) r.originalViewedAt = "2026-11-26T12:00:00Z";
    return json(200, { text: r.original }, { "Cache-Control": "no-store" });
  }

  private markRead(id: string): Response {
    const r = this.feed.find((f) => f.id === id);
    if (!r || r.author !== "partner" || !this.visible(r)) return json(404, err("not_found", "Не найдено"));
    if (r.status === "delivered") Object.assign(r, { status: "read", read_at: "2026-11-26T12:00:00Z" });
    return json(204);
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

// ------------------------------------------------------------------ сценарии черновиков и строки ленты

/** Текст кризисного протокола заглушки (выдуманный, как ответ сервера по шаблону). */
export const CRISIS_TEXT =
  "Сообщение не отправлено: в нём есть признаки, при которых я не передаю текст партнёру. Если тебе опасно — звони в экстренные службы.";

/** Ответ модели на `POST /drafts` или `regenerate`: черновик (текст), кризис или ошибка до ответа. */
export type DraftReply =
  | { kind: "draft"; text?: string }
  | { kind: "crisis"; text?: string; contacts?: unknown; extra?: Record<string, unknown> }
  | { kind: "status"; status: number; body: unknown };

export interface FeedRow {
  id: string;
  author: "me" | "partner";
  original: ExportText;
  forRecipient: ExportText;
  is_adapted: boolean;
  harsh_tone_warning: boolean;
  status: PartnerMessage["status"];
  withheld_reason: NonNullable<PartnerMessage["withheld_reason"]> | null;
  withheld_crisis: NonNullable<PartnerMessage["withheld_crisis"]> | null;
  /** Имена блоков протокола сервера (`crisis.plan` по всему набору признаков, АР-57); null — до 0021. */
  withheld_crisis_blocks: NonNullable<PartnerMessage["withheld_crisis_blocks"]> | null;
  /** Поля ответа поверх вида по контракту — для проверок нарушений контракта сервером. */
  extra?: Record<string, unknown>;
  reply_to_id: string | null;
  draft_id: string | null;
  sent_at: string;
  read_at: string | null;
  /** Когда получатель впервые открыл оригинал (ТЗ §7 `original_viewed_at`). */
  originalViewedAt: string | null;
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
