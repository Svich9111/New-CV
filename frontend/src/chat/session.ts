/**
 * Состояние личного чата с агентом (ФТ-05) на время сессии Mini App — только в памяти.
 *
 * Живёт в сессии, а не в экране: уход в «Помощь» посреди ответа (частый случай при кризисе) не обрывает поток,
 * а «Назад» возвращает к той же переписке и к недописанному сообщению. Ничего не пишется в хранилища браузера,
 * в адрес и в консоль (ТЗ §9.1, принцип 4).
 *
 * Повтор без дубля реплики (АР-51). У каждой новой реплики — свой ключ `client_message_id` (uuid клиента);
 * «Повторить», «Отправить ещё раз» и автоповтор после 409 шлют её с тем же ключом, и сервер новой реплики не
 * создаёт: ответ уже сохранён — отдаёт его, нет — отвечает на неё. Первое событие потока `accepted` даёт id
 * реплики — пузырь привязан к записи сервера. После сбоя с неизвестным исходом клиент перечитывает первую
 * страницу истории и ищет свою реплику по ключу, а ответ на неё — по `reply_to`, а не по тексту: одинаковые
 * реплики подряд — разные реплики, реплика с другого устройства не принимается за свою. Двойное нажатие
 * «Отправить» не создаёт второй реплики: пока ответ идёт, новая не уходит.
 */
import { ApiError, type Api, type ChatOutcome } from "../api/client";
import type { AcceptedEvent } from "../api/chat";
import { chatErrorText, errorText, limitUntilText } from "../api/messages";
import type { ChatHistory, ChatMessage, HelpContact } from "../api/types";

/** Сколько реплик в странице истории. */
export const PAGE = 30;
/** Предел длины реплики (контракт: `maxLength: 4000`). */
export const MAX_TEXT = 4000;
/**
 * Ответ на реплику ещё готовит другой запрос с тем же ключом (409 `conflict_retry`, АР-51; например, прежняя
 * попытка после «Остановить» или обрыва ещё не закончилась на сервере): паузы перед автоповторами с тем же
 * ключом. Потом — «Повторить» вручную (сбойный процесс сервер отпускает не позже чем через 5 минут).
 */
export const REPLY_WAIT_MS: readonly number[] = [3000, 6000, 10000];

/** Что с репликой владельца последнего обмена — подписью под пузырём (ТЗ §8: состояние не только цветом). */
export type OwnerStatus =
  | { kind: "sending" }
  | { kind: "answered" }
  /**
   * Сервер реплику не сохранил — повтор безопасен. `help` — ответа модели нет и реплику сохранить не удалось
   * (`accepted` без id, затем `error`): строка о помощи со 112, как у `unanswered` (АР-34 п.2).
   */
  | { kind: "unsent"; note: string; retry: boolean; help?: boolean }
  /**
   * Реплика сохранена, ответа агента нет. `help` — ответ модели не получен событием `error` (в том числе отказ):
   * под подписью строка о помощи со 112 (АР-34 п.2) — это могла быть нераспознанная реплика человека в кризисе.
   */
  | { kind: "unanswered"; note: string; retry: boolean; help?: boolean }
  /** Не удалось узнать, сохранилась ли реплика (нет связи и после сбоя). Повтор сначала проверит историю. */
  | { kind: "unknown"; note: string }
  /** Дневной лимит (429): реплика сохранена, агент ответит после окна. */
  | { kind: "limited" };

export interface ChatItem {
  /** id реплики сервера или локальный ключ (`local-N`) для реплики этой сессии. */
  key: string;
  id: string | null;
  author: "owner" | "agent";
  /** `null` — текст не удалось расшифровать на сервере (`UnavailableText`). */
  text: string | null;
  status?: OwnerStatus;
  /** Ответ агента идёт (`streaming`) или оборвался и не сохранён (`interrupted`, `stopped`). */
  partial?: "streaming" | "interrupted" | "stopped";
  /** Кризисный ответ: контакты помощи показываются прямо под ним (ТЗ §9.3), и после нового открытия чата. */
  crisis?: HelpContact[];
  /** Ключ идемпотентности реплики владельца (АР-51); `null` — запись без ключа (сохранена до миграции 0018). */
  cmid?: string | null;
  /** У ответа агента — id реплики владельца, на которую он отвечает (`reply_to`). */
  replyTo?: string | null;
}

export type Phase = "idle" | "waiting" | "streaming" | "checking";

export interface ChatState {
  items: ChatItem[];
  /** Первая страница истории. */
  history: "loading" | "loaded" | "failed";
  historyError: string | null;
  /** Курсор более старой страницы; `null` — старше нет. */
  cursor: string | null;
  olderLoading: boolean;
  olderError: string | null;
  /** «Сообщения пришлось загрузить заново» — после 422 по курсору (смена ключа сервера, Б-62). */
  restarted: boolean;
  phase: Phase;
  /** Неотправленный текст в поле ввода. */
  input: string;
  /** Дневной лимит: когда агент снова ответит (текст). */
  limit: string | null;
  /** Объявление для экранного диктора (aria-live): «Агент пишет…», затем ответ целиком — без кусков потока. */
  live: string;
  /** Последний итог обмена — для тактильного отклика экрана. */
  outcome: { seq: number; kind: "done" | "crisis" | "failed" } | null;
}

export interface ChatSessionOptions {
  /** Ключ новой реплики (по умолчанию — `newMessageKey`). */
  newKey?: () => string;
  /** Паузы автоповтора при 409 «ответ ещё готовится» (по умолчанию — `REPLY_WAIT_MS`). */
  replyWaitMs?: readonly number[];
}

/**
 * Ключ реплики — uuid v4 (`crypto.randomUUID`; в старых WebView без него — из `getRandomValues`). Ключ не
 * секрет: он уникален в пределах владельца и не открывает ничего чужого (АР-51).
 */
export function newMessageKey(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (typeof c?.randomUUID === "function") return c.randomUUID();
  const b = new Uint8Array(16);
  if (typeof c?.getRandomValues === "function") c.getRandomValues(b);
  else for (let i = 0; i < b.length; i++) b[i] = Math.floor(Math.random() * 256);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

const initial = (): ChatState => ({
  items: [],
  history: "loading",
  historyError: null,
  cursor: null,
  olderLoading: false,
  olderError: null,
  restarted: false,
  phase: "idle",
  input: "",
  limit: null,
  live: "",
  outcome: null,
});

const textOf = (m: ChatMessage): string | null => (typeof m.text === "string" ? m.text : null);
const fromServer = (m: ChatMessage): ChatItem => ({
  key: m.id,
  id: m.id,
  author: m.author,
  text: textOf(m),
  cmid: m.client_message_id,
  replyTo: m.reply_to,
});
/** Страница истории приходит от новых к старым; на экране — от старых к новым. */
const chronological = (page: ChatHistory): ChatMessage[] => [...page.items].reverse();

/** Запись истории без ключа (до миграции 0018): только для них работает свёртка повторов. */
const legacyOwner = (it: ChatItem | undefined): it is ChatItem =>
  it !== undefined && it.author === "owner" && it.id !== null && !it.cmid && !it.status;

/**
 * Что показать и в каком порядке.
 * - Ответ агента — сразу под своей репликой (`reply_to`): ответ на реплику, повторённую по ключу после других
 *   реплик, сервер ставит в ленту по времени сохранения (АР-51), а на экране он под вопросом.
 * - Подряд идущие одинаковые реплики владельца **без ключа** (сохранены до миграции 0018) — повторы одной
 *   реплики после сбоя (сервер тогда хранил каждую попытку, АР-32), на экране — один пузырь, последний. Реплики
 *   с ключом не сворачиваются никогда: одинаковые подряд — разные реплики.
 */
export function visibleItems(items: ChatItem[]): ChatItem[] {
  const unique = items.filter((it, i) => {
    const next = items[i + 1];
    return !(legacyOwner(it) && legacyOwner(next) && it.text !== null && it.text === next.text);
  });
  const owners = new Set(unique.filter((it) => it.author === "owner" && it.id).map((it) => it.id as string));
  const replies = new Map<string, ChatItem[]>();
  const out: ChatItem[] = [];
  for (const it of unique) {
    if (it.author === "agent" && it.replyTo && owners.has(it.replyTo)) {
      replies.set(it.replyTo, [...(replies.get(it.replyTo) ?? []), it]);
    } else {
      out.push(it);
    }
  }
  return out.flatMap((it) => {
    const r = it.author === "owner" && it.id ? replies.get(it.id) : undefined;
    if (!r) return [it];
    replies.delete(it.id as string);
    return [it, ...r];
  });
}

/** Последний обмен: локальный ключ пузыря, текст, ключ идемпотентности и сколько раз ждали чужой ответ (409). */
interface Turn {
  key: string;
  text: string;
  cmid: string;
  waits: number;
}

type Reconciled = "answered" | "unanswered" | "unsent" | "unknown";

export class ChatSession {
  private state: ChatState = initial();
  private readonly listeners = new Set<() => void>();
  private seq = 0;
  private ctrl: AbortController | null = null;
  /** Сессия Mini App закончилась (перезапуск): ответ отменён, проверок истории после сбоя не делаем. */
  private closed = false;
  private started = false;
  private turn: Turn | null = null;
  /** Нужен перезапуск истории после 422 по курсору; подпись «загрузить заново» — когда он удастся (Б-62). */
  private restartPending = false;
  /** Контакты помощи для кризисных ответов из истории (`crisis: true`; в истории их нет — `GET /help/contacts`). */
  private contacts: HelpContact[] | null = null;
  private contactsLoading = false;
  /** id кризисных ответов из истории, которые ждут контактов (до этого — пустой список: экран покажет 112). */
  private readonly crisisWaiting = new Set<string>();
  private readonly newKey: () => string;
  private readonly replyWaitMs: readonly number[];

  constructor(
    private readonly api: Api,
    opts: ChatSessionOptions = {},
  ) {
    this.newKey = opts.newKey ?? newMessageKey;
    this.replyWaitMs = opts.replyWaitMs ?? REPLY_WAIT_MS;
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getSnapshot = (): ChatState => this.state;

  private set(patch: Partial<ChatState> | ((s: ChatState) => Partial<ChatState>)): void {
    const p = typeof patch === "function" ? patch(this.state) : patch;
    this.state = { ...this.state, ...p };
    for (const fn of [...this.listeners]) fn();
  }

  private patchItem(key: string, patch: Partial<ChatItem>): void {
    this.set((s) => ({ items: s.items.map((it) => (it.key === key ? { ...it, ...patch } : it)) }));
  }

  /** Экран открыт: первая страница истории — один раз за сессию (и снова — по «Попробовать снова»). */
  start(): void {
    if (this.started) return;
    this.started = true;
    void this.loadFirst(false);
  }

  /** Сессия Mini App началась (и снова — после повторного монтирования в StrictMode). */
  open(): void {
    this.closed = false;
  }

  /** Сессия закончилась: идущий ответ отменяется, после сбоя история не перечитывается. */
  close(): void {
    this.closed = true;
    this.ctrl?.abort();
  }

  setInput(input: string): void {
    this.set({ input: input.slice(0, MAX_TEXT) });
  }

  get busy(): boolean {
    return this.state.phase !== "idle" || this.state.history === "loading";
  }

  retryHistory(): void {
    if (this.state.history === "loading" || this.state.phase !== "idle") return;
    void this.loadFirst(false);
  }

  /** Запись истории для ленты; кризисному ответу — контакты помощи (или пустой список, пока они грузятся). */
  private item(m: ChatMessage): ChatItem {
    const it = fromServer(m);
    if (!m.crisis || m.author !== "agent") return it;
    if (this.contacts) return { ...it, crisis: this.contacts };
    this.crisisWaiting.add(m.id);
    return { ...it, crisis: [] };
  }

  /** Кризисные ответы из истории ждут контактов — один запрос `GET /help/contacts`; не вышло — на экране 112. */
  private needContacts(): void {
    if (this.contacts || this.contactsLoading || this.crisisWaiting.size === 0) return;
    this.contactsLoading = true;
    this.api.helpContacts().then(
      (list) => {
        this.contactsLoading = false;
        this.contacts = list;
        const waiting = new Set(this.crisisWaiting);
        this.crisisWaiting.clear();
        this.set((s) => ({ items: s.items.map((it) => (it.id && waiting.has(it.id) ? { ...it, crisis: list } : it)) }));
      },
      () => {
        this.contactsLoading = false;
      },
    );
  }

  /**
   * Первая страница истории. `restart` — после 422 по курсору: прежние старые страницы сброшены, история
   * начинается заново (контракт, Б-62). Записи сервера, которые есть в ленте (по id, а реплики этой сессии — по
   * ключу), сохраняют локальное состояние: подпись, контакты под кризисным ответом. Из локального, которого на
   * странице нет, остаются реплики без id (не отправлено, неизвестно, сохранить не удалось), кризисные ответы без
   * id (ТЗ §9.3 «всегда») и пузыри идущего обмена. Если перезапуск сам не удался, подпись появится после удачного
   * повтора.
   */
  private async loadFirst(restart: boolean): Promise<void> {
    if (restart) this.restartPending = true;
    this.set({ history: "loading", historyError: null, olderError: null });
    let page: ChatHistory;
    try {
      page = await this.api.chatHistory({ limit: PAGE });
    } catch (e) {
      this.set({ history: "failed", historyError: errText(e, "chatHistory") });
      return;
    }
    this.set((s) => {
      const restarted = restart || this.restartPending || s.restarted;
      this.restartPending = false;
      return { items: this.mergeFirst(s.items, page), cursor: page.next_cursor, history: "loaded", restarted };
    });
    this.needContacts();
  }

  private mergeFirst(local: ChatItem[], page: ChatHistory): ChatItem[] {
    const byId = new Map(local.filter((it) => it.id).map((it) => [it.id as string, it]));
    const byCmid = new Map(local.filter((it) => it.author === "owner" && it.cmid).map((it) => [it.cmid as string, it]));
    const used = new Set<string>();
    const messages = chronological(page);
    // Реплики, на которые в истории уже есть ответ: подпись «нет ответа» под ними больше не верна.
    const answered = new Set(messages.filter((m) => m.author === "agent" && m.reply_to).map((m) => m.reply_to as string));
    const server = messages.map((m) => {
      const sv = this.item(m);
      const mine = byId.get(m.id) ?? (m.client_message_id ? byCmid.get(m.client_message_id) : undefined);
      if (!mine || used.has(mine.key)) return sv;
      used.add(mine.key);
      const merged: ChatItem = { ...sv, key: mine.key };
      if (mine.status) merged.status = mine.status.kind !== "sending" && answered.has(m.id) ? { kind: "answered" } : mine.status;
      if (mine.crisis && mine.crisis.length > 0) merged.crisis = mine.crisis;
      return merged;
    });
    const t = this.turn;
    const turnOwner = t ? server.find((it) => it.key === t.key)?.id : undefined;
    const ofTurn = (it: ChatItem) =>
      t !== null &&
      (it.key === t.key ||
        // Недописанный ответ обмена не нужен, если сохранённый ответ на эту реплику пришёл с историей.
        (it.key === `${t.key}-reply` && !(it.partial !== "streaming" && turnOwner && answered.has(turnOwner))));
    const keep = local.filter(
      (it) => !used.has(it.key) && (ofTurn(it) || (it.id === null && (it.author === "owner" || it.crisis !== undefined))),
    );
    return [...server, ...keep];
  }

  /** Более старая страница. 422 по курсору — история с первой страницы, прежний курсор не повторяется. */
  async loadOlder(): Promise<void> {
    const { cursor, olderLoading, phase, history } = this.state;
    if (!cursor || olderLoading || phase !== "idle" || history !== "loaded") return;
    this.set({ olderLoading: true, olderError: null });
    let page: ChatHistory;
    try {
      page = await this.api.chatHistory({ cursor, limit: PAGE });
    } catch (e) {
      this.set({ olderLoading: false });
      if (e instanceof ApiError && e.status === 422) {
        await this.loadFirst(true);
        return;
      }
      this.set({ olderError: errText(e, "chatHistory") });
      return;
    }
    this.set((s) => {
      const have = new Set(s.items.map((it) => it.id));
      const keys = new Set(s.items.map((it) => it.cmid).filter(Boolean));
      const older = chronological(page)
        .filter((m) => !have.has(m.id) && !(m.client_message_id && keys.has(m.client_message_id)))
        .map((m) => this.item(m));
      return { items: [...older, ...s.items], cursor: page.next_cursor, olderLoading: false };
    });
    this.needContacts();
  }

  /** Отправить текст из поля ввода. `false` — не отправлено (пусто, длиннее предела или ответ ещё идёт). */
  send(): boolean {
    const text = this.state.input;
    if (this.busy || text.trim().length === 0 || text.length > MAX_TEXT) return false;
    const key = `local-${++this.seq}`;
    const cmid = this.newKey();
    this.turn = { key, text, cmid, waits: 0 };
    this.set((s) => ({
      input: "",
      items: [...s.items, { key, id: null, author: "owner", text, cmid, status: { kind: "sending" } }],
    }));
    void this.exchange(key);
    return true;
  }

  /** «Остановить»: поток закрывается, сервер отменяет вызовы модели; недописанный ответ не сохраняется. */
  stop(): void {
    this.ctrl?.abort();
  }

  /**
   * Повтор последней реплики тем же пузырём и с тем же ключом (АР-51): новой реплики сервер не создаёт. Если
   * неизвестно, сохранилась ли она, — сначала история: ответ уже есть — показывается он, без повторной отправки.
   */
  async retry(key: string): Promise<void> {
    const turn = this.turn;
    if (!turn || turn.key !== key || this.state.phase !== "idle") return;
    const item = this.state.items.find((it) => it.key === key);
    const st = item?.status;
    if (!st || !(st.kind === "unknown" || ((st.kind === "unsent" || st.kind === "unanswered") && st.retry))) return;
    turn.waits = 0;
    if (st.kind === "unknown") {
      this.set({ phase: "checking" });
      const answered = await this.reconcile(key);
      if (answered !== "unanswered" && answered !== "unsent") {
        this.set({ phase: "idle" });
        return;
      }
    }
    this.set((s) => ({
      items: s.items.filter((it) => !(it.partial && it.partial !== "streaming" && it.key === `${key}-reply`)),
    }));
    this.patchItem(key, { status: { kind: "sending" } });
    void this.exchange(key);
  }

  private ownerId(key: string): string | null {
    return this.state.items.find((it) => it.key === key)?.id ?? null;
  }

  /** `accepted`: пузырь реплики привязан к записи сервера; её копия, пришедшая с историей, не показывается дважды. */
  private bindOwner(key: string, id: string): void {
    this.set((s) => ({
      items: s.items
        .filter((it) => it.key === key || it.id !== id)
        .map((it) => (it.key === key ? { ...it, id } : it.key === `${key}-reply` ? { ...it, replyTo: id } : it)),
    }));
  }

  private async exchange(key: string): Promise<void> {
    const turn = this.turn as Turn;
    const replyKey = `${key}-reply`;
    const ctrl = new AbortController();
    this.ctrl = ctrl;
    this.set({ phase: "waiting", live: "Агент пишет…", limit: null });
    let reply = "";
    /** `accepted`: сохранена ли реплика; `null` — события не было (сбой до него). */
    let saved: boolean | null = null;
    let outcome: ChatOutcome;
    try {
      outcome = await this.api.chatSend(turn.text, {
        signal: ctrl.signal,
        clientMessageId: turn.cmid,
        onAccepted: (a: AcceptedEvent) => {
          saved = a.messageId !== null;
          if (a.messageId) this.bindOwner(key, a.messageId);
        },
        onDelta: (chunk) => {
          if (reply === "") {
            const replyTo = this.ownerId(key);
            this.set((s) => ({
              phase: "streaming",
              items: [
                ...s.items.filter((it) => it.key !== replyKey),
                { key: replyKey, id: null, author: "agent", text: chunk, partial: "streaming", replyTo },
              ],
            }));
          } else {
            this.patchItem(replyKey, { text: reply + chunk });
          }
          reply += chunk;
        },
      });
    } catch (e) {
      if (this.ctrl === ctrl) this.ctrl = null;
      await this.failed(key, reply, e);
      return;
    }
    if (this.ctrl === ctrl) this.ctrl = null;
    this.finish(key, replyKey, reply, outcome, saved);
  }

  private finish(key: string, replyKey: string, shown: string, outcome: ChatOutcome, saved: boolean | null): void {
    const replyTo = this.ownerId(key);
    if (outcome.kind === "done") {
      const id = outcome.data.message_id;
      this.patchItem(key, { status: { kind: "answered" } });
      // Повтор по ключу отдал сохранённый ответ, а он уже есть в ленте (пришёл с историей) — не дважды.
      this.set((s) => ({
        items: s.items
          .filter((it) => it.key === replyKey || it.id !== id)
          .map((it) => (it.key === replyKey ? { ...it, id, replyTo, partial: undefined } : it)),
      }));
      this.set({ phase: "idle", live: shown ? `Ответ агента: ${shown}` : "Агент ответил.", outcome: this.next("done") });
      return;
    }
    if (outcome.kind === "crisis") {
      // Ничего не смягчается и не сокращается: `text` показывается целиком; `replace` — показанные куски
      // заменяются им (при `replace: false` `text` начинается с показанного — итог тот же).
      const c = outcome.data;
      const text = c.text ?? CRISIS_FALLBACK;
      this.patchItem(key, { status: { kind: "answered" } });
      this.set((s) => ({
        phase: "idle",
        items: [
          ...s.items.filter((it) => it.key !== replyKey && !(c.messageId && it.id === c.messageId)),
          { key: c.messageId ?? replyKey, id: c.messageId, author: "agent", text, crisis: c.contacts, replyTo },
        ],
        live: `Ответ агента: ${text} Ниже — контакты помощи.`,
        outcome: this.next("crisis"),
      }));
      return;
    }
    // `error`: реплика сохранена (контракт), если `accepted` не сказал обратного; показанные куски не
    // сохранены — помечаются прерванными. Повтор — с тем же ключом: ответ будет на эту же реплику.
    this.patchItem(replyKey, { partial: "interrupted" });
    const note = chatErrorText(outcome.code);
    this.patchItem(
      key,
      saved === false
        ? { status: { kind: "unsent", note: `${note} Сообщение не сохранилось.`, retry: true, help: true } }
        : { status: { kind: "unanswered", note, retry: true, help: true } },
    );
    this.set({ phase: "idle", live: "", outcome: this.next("failed") });
  }

  private async failed(key: string, shown: string, e: unknown): Promise<void> {
    const turn = this.turn as Turn;
    const replyKey = `${key}-reply`;
    const err = e instanceof ApiError ? e : new ApiError(0, "network", errorText("network"));
    const markPartial = (p: "interrupted" | "stopped") => {
      if (shown) this.patchItem(replyKey, { partial: p });
    };
    const fail = (status: OwnerStatus) => {
      this.patchItem(key, { status });
      this.set({ phase: "idle", live: "", outcome: this.next("failed") });
    };

    if (err.status === 429) {
      const until = err.retryAfter ? limitUntilText(err.retryAfter) : null;
      this.patchItem(key, { status: { kind: "limited" } });
      this.set({ phase: "idle", live: "", limit: until ?? "", outcome: this.next("failed") });
      return;
    }
    // 409 с ключом: транзакция сохранения отменена (ничего не сохранено) или ответ на реплику ещё готовит другой
    // запрос (АР-51) — различает только история.
    if (err.code === "conflict_retry") {
      await this.conflict(key);
      return;
    }
    // 422: текст проверен до отправки, значит, сервер не принял ключ (под ним другая реплика, АР-51). Под этим
    // ключом наша реплика не сохранена — повтор уйдёт с новым ключом как новая реплика.
    if (err.status === 422) {
      turn.cmid = this.newKey();
      fail({ kind: "unsent", note: KEY_REJECTED, retry: true });
      return;
    }
    // Ответы до сохранения реплики: ничего не сохранено, повтор безопасен.
    if (err.status === 401) {
      fail({ kind: "unsent", note: err.message, retry: true });
      return;
    }
    if (err.status === 403) {
      fail({ kind: "unsent", note: err.message, retry: false });
      return;
    }
    // 402 — после классификатора: реплика сохранена, ответа нет до продления подписки (этап 4).
    if (err.status === 402) {
      fail({ kind: "unanswered", note: err.message, retry: false });
      return;
    }
    // Остальное — обрыв, срок, сбой сервера, остановка, испорченный поток: сохранено ли что-то, знает только
    // история. После 200 реплика сохранена наверняка; ответ агента — если `done` не дошёл.
    markPartial(err.code === "aborted" ? "stopped" : "interrupted");
    if (this.closed) {
      fail({ kind: "unknown", note: err.message });
      return;
    }
    this.set({ phase: "checking", live: "" });
    const note = err.code === "aborted" ? chatErrorText("aborted") : err.message;
    const result = await this.reconcile(key, note);
    this.set({ phase: "idle", outcome: this.next(result === "answered" ? "done" : "failed") });
  }

  /**
   * 409 после немедленного повтора (`Api.chatSend`). История: реплики под ключом нет — ничего не сохранено,
   * «Отправить ещё раз»; ответ уже сохранён — показан он; реплика без ответа — ответ ещё готовится, автоповтор с
   * тем же ключом через паузы `replyWaitMs`, потом «Повторить».
   */
  private async conflict(key: string): Promise<void> {
    const turn = this.turn as Turn;
    if (this.closed) {
      this.patchItem(key, { status: { kind: "unknown", note: chatErrorText("conflict_retry") } });
      this.set({ phase: "idle", live: "", outcome: this.next("failed") });
      return;
    }
    this.set({ phase: "checking", live: "" });
    const result = await this.reconcile(key, REPLY_BUSY);
    if (result === "unanswered" && turn.waits < this.replyWaitMs.length && !this.closed) {
      const ms = this.replyWaitMs[turn.waits++];
      this.patchItem(key, { status: { kind: "sending" } });
      this.set({ phase: "waiting", live: "Агент пишет…" });
      if (await this.pause(ms)) {
        void this.exchange(key);
        return;
      }
      this.patchItem(key, { status: { kind: "unanswered", note: REPLY_BUSY, retry: true } });
    } else if (result === "unsent") {
      this.patchItem(key, { status: { kind: "unsent", note: chatErrorText("conflict_retry"), retry: true } });
    }
    this.set({ phase: "idle", live: result === "answered" ? this.state.live : "", outcome: this.next(result === "answered" ? "done" : "failed") });
  }

  /** Пауза перед автоповтором; «Остановить» и конец сессии её прерывают (`false`). */
  private pause(ms: number): Promise<boolean> {
    const ctrl = new AbortController();
    this.ctrl = ctrl;
    return new Promise((resolve) => {
      const end = (ok: boolean) => {
        clearTimeout(timer);
        ctrl.signal.removeEventListener("abort", onAbort);
        if (this.ctrl === ctrl) this.ctrl = null;
        resolve(ok);
      };
      const onAbort = () => end(false);
      const timer = setTimeout(() => end(true), ms);
      ctrl.signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  /**
   * Что сервер успел сохранить: первая страница истории. Своя реплика — по ключу `client_message_id` (или по id
   * из `accepted`), ответ на неё — по `reply_to`; тексты не сравниваются. `answered` — ответ агента сохранён
   * (показывается он); `unanswered` — реплика сохранена без ответа; `unsent` — реплики нет; `unknown` — историю
   * прочитать не удалось. Записи страницы, которых в ленте нет (другое устройство, история не загрузилась),
   * встают в ленту по порядку сервера; если первая страница при открытии не загрузилась, это она и есть.
   */
  private async reconcile(key: string, note = chatErrorText("stream_broken")): Promise<Reconciled> {
    let page: ChatHistory;
    try {
      page = await this.api.chatHistory({ limit: PAGE });
    } catch {
      this.patchItem(key, {
        status: { kind: "unknown", note: `${note} Не удалось проверить, дошло ли сообщение.` },
      });
      return "unknown";
    }
    const replyKey = `${key}-reply`;
    const server = chronological(page);
    const byCmid = new Map(server.filter((m) => m.author === "owner" && m.client_message_id).map((m) => [m.client_message_id as string, m]));
    let result: Reconciled = "unsent";
    this.set((s) => {
      // Реплики этой сессии без id (429, обрыв до ответа) находят свою запись по ключу.
      const local = s.items.map((it) =>
        it.author === "owner" && it.id === null && it.cmid && byCmid.has(it.cmid) ? { ...it, id: byCmid.get(it.cmid)!.id } : it,
      );
      const own = local.find((it) => it.key === key);
      const ownerId = own?.id ?? null;
      const reply = ownerId ? server.find((m) => m.author === "agent" && m.reply_to === ownerId) : undefined;
      result = reply ? "answered" : ownerId ? "unanswered" : "unsent";
      const status: OwnerStatus =
        result === "answered"
          ? { kind: "answered" }
          : result === "unanswered"
            ? { kind: "unanswered", note, retry: true }
            : { kind: "unsent", note: `${note} Сообщение не сохранилось.`, retry: true };
      const have = new Set(local.map((it) => it.id).filter(Boolean));
      const at = ownerId ? server.findIndex((m) => m.id === ownerId) : -1;
      const fresh = server.map((m, i) => ({ m, i })).filter(({ m }) => !have.has(m.id));
      const before = fresh.filter(({ i }) => at < 0 || i < at).map(({ m }) => this.item(m));
      const after = fresh.filter(({ i }) => at >= 0 && i > at).map(({ m }) => this.item(m));
      // Недописанный ответ остаётся под своей репликой с пометкой «прерван», если ответа в истории нет.
      const partial = reply ? [] : local.filter((it) => it.key === replyKey).map((it) => ({ ...it, replyTo: ownerId }));
      const rest = local.filter((it) => it.key !== key && it.key !== replyKey);
      const firstPage = s.history === "failed" ? { history: "loaded" as const, historyError: null, cursor: page.next_cursor } : {};
      return {
        ...firstPage,
        items: [...rest, ...before, ...(own ? [{ ...own, status }] : []), ...partial, ...after],
        live: reply ? `Ответ агента: ${textOf(reply) ?? ""}` : s.live,
      };
    });
    this.needContacts();
    return result;
  }

  private next(kind: "done" | "crisis" | "failed") {
    return { seq: (this.state.outcome?.seq ?? 0) + 1, kind };
  }
}

/** 422 на реплику с ключом: под ключом сохранена другая реплика (АР-51). */
const KEY_REJECTED = "Сервер не принял это сообщение. Отправь его ещё раз — оно уйдёт как новое.";
/** 409, а реплика сохранена без ответа: ответ на неё готовит другой запрос (АР-51). */
const REPLY_BUSY = "Ответ на это сообщение ещё готовится. Повтори чуть позже — он придёт к этому же сообщению.";

/** Кризисное событие пришло без текста: контакты помощи всё равно показываются (ТЗ §9.3). */
export const CRISIS_FALLBACK =
  "То, что ты описываешь, звучит серьёзно. Ты не один и не одна с этим. Пожалуйста, обратись за помощью прямо сейчас — контакты ниже.";

function errText(e: unknown, op: "chatHistory"): string {
  return e instanceof ApiError ? e.message : errorText("internal", undefined, undefined, op);
}
