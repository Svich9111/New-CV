/**
 * Состояние личного чата с агентом (ФТ-05) на время сессии Mini App — только в памяти.
 *
 * Живёт в сессии, а не в экране: уход в «Помощь» посреди ответа (частый случай при кризисе) не обрывает поток,
 * а «Назад» возвращает к той же переписке и к недописанному сообщению. Ничего не пишется в хранилища браузера,
 * в адрес и в консоль (ТЗ §9.1, принцип 4).
 *
 * Повтор без дубля реплики. Контракт сохраняет реплику владельца до ответа модели, а повтор того же текста
 * создаёт новую (АР-32). Поэтому после любого сбоя, где неизвестно, что сервер успел сохранить, клиент сначала
 * перечитывает первую страницу истории: ответ агента сохранён (не дошло только `done`) — показывается он, и
 * реплика не отправляется снова; реплика не сохранена — повтор безопасен; сохранена без ответа — повтор
 * отправляет её снова тем же пузырём, а в истории подряд идущие одинаковые реплики владельца показываются
 * одной (`visibleItems`). Двойное нажатие «Отправить» не создаёт второй реплики: пока ответ идёт, новая не уходит.
 */
import { ApiError, type Api, type ChatOutcome } from "../api/client";
import { chatErrorText, errorText, limitUntilText } from "../api/messages";
import type { ChatHistory, ChatMessage, HelpContact } from "../api/types";

/** Сколько реплик в странице истории. */
export const PAGE = 30;
/** Предел длины реплики (контракт: `maxLength: 4000`). */
export const MAX_TEXT = 4000;

/** Что с репликой владельца последнего обмена — подписью под пузырём (ТЗ §8: состояние не только цветом). */
export type OwnerStatus =
  | { kind: "sending" }
  | { kind: "answered" }
  /** Сервер реплику не сохранил — повтор безопасен. */
  | { kind: "unsent"; note: string; retry: boolean }
  /** Реплика сохранена, ответа агента нет. */
  | { kind: "unanswered"; note: string; retry: boolean }
  /** Не удалось узнать, сохранилась ли реплика (нет связи и после сбоя). Повтор сначала проверит историю. */
  | { kind: "unknown"; note: string }
  /** Дневной лимит (429): реплика сохранена, агент ответит после окна. */
  | { kind: "limited" };

export interface ChatItem {
  /** id реплики сервера или локальный ключ (`local-N`) для ещё не подтверждённой. */
  key: string;
  id: string | null;
  author: "owner" | "agent";
  /** `null` — текст не удалось расшифровать на сервере (`UnavailableText`). */
  text: string | null;
  status?: OwnerStatus;
  /** Ответ агента идёт (`streaming`) или оборвался и не сохранён (`interrupted`, `stopped`). */
  partial?: "streaming" | "interrupted" | "stopped";
  /** Кризисный ответ: контакты помощи показываются прямо под ним (ТЗ §9.3). */
  crisis?: HelpContact[];
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
const fromServer = (m: ChatMessage): ChatItem => ({ key: m.id, id: m.id, author: m.author, text: textOf(m) });
/** Страница истории приходит от новых к старым; на экране — от старых к новым. */
const chronological = (page: ChatHistory): ChatItem[] => [...page.items].reverse().map(fromServer);

/**
 * Что показать: подряд идущие одинаковые реплики владельца без ответа между ними — это повторы одной реплики
 * после сбоя (сервер хранит каждую попытку отдельно, АР-32), на экране — один пузырь, последний.
 */
export function visibleItems(items: ChatItem[]): ChatItem[] {
  return items.filter((it, i) => {
    const next = items[i + 1];
    return !(it.author === "owner" && next?.author === "owner" && it.text !== null && it.text === next.text);
  });
}

export class ChatSession {
  private state: ChatState = initial();
  private readonly listeners = new Set<() => void>();
  private seq = 0;
  private ctrl: AbortController | null = null;
  /** Сессия Mini App закончилась (перезапуск): ответ отменён, проверок истории после сбоя не делаем. */
  private closed = false;
  private started = false;
  /** Последний обмен: локальный ключ реплики владельца и её текст. */
  private turn: { key: string; text: string } | null = null;

  constructor(private readonly api: Api) {}

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

  /**
   * Первая страница истории. `restart` — после 422 по курсору: прежние старые страницы сброшены, история
   * начинается заново (контракт, Б-62). Сохраняются локальные реплики, которых сервер не подтвердил, и
   * контакты помощи под кризисными ответами (по id).
   */
  private async loadFirst(restart: boolean): Promise<void> {
    this.set({ history: "loading", historyError: null, olderError: null });
    let page: ChatHistory;
    try {
      page = await this.api.chatHistory({ limit: PAGE });
    } catch (e) {
      this.set({ history: "failed", historyError: errText(e, "chatHistory") });
      return;
    }
    this.set((s) => {
      const crisisById = new Map(s.items.filter((it) => it.id && it.crisis).map((it) => [it.id, it.crisis]));
      const server = chronological(page).map((it) => (crisisById.has(it.id) ? { ...it, crisis: crisisById.get(it.id) } : it));
      const pendingLocal = s.items.filter(
        (it) => it.id === null && (it.status?.kind === "unsent" || it.status?.kind === "unknown"),
      );
      return {
        items: [...server, ...pendingLocal],
        cursor: page.next_cursor,
        history: "loaded",
        restarted: restart || s.restarted,
      };
    });
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
      const older = chronological(page).filter((it) => !have.has(it.id));
      return { items: [...older, ...s.items], cursor: page.next_cursor, olderLoading: false };
    });
  }

  /** Отправить текст из поля ввода. `false` — не отправлено (пусто, длиннее предела или ответ ещё идёт). */
  send(): boolean {
    const text = this.state.input;
    if (this.busy || text.trim().length === 0 || text.length > MAX_TEXT) return false;
    const key = `local-${++this.seq}`;
    this.turn = { key, text };
    this.set((s) => ({
      input: "",
      items: [...s.items, { key, id: null, author: "owner", text, status: { kind: "sending" } }],
    }));
    void this.exchange(key, text);
    return true;
  }

  /** «Остановить»: поток закрывается, сервер отменяет вызовы модели; недописанный ответ не сохраняется. */
  stop(): void {
    this.ctrl?.abort();
  }

  /**
   * Повтор последней реплики тем же пузырём. Если неизвестно, сохранилась ли она, — сначала история: ответ
   * уже есть — показывается он, без повторной отправки.
   */
  async retry(key: string): Promise<void> {
    const turn = this.turn;
    if (!turn || turn.key !== key || this.state.phase !== "idle") return;
    const item = this.state.items.find((it) => it.key === key);
    const st = item?.status;
    if (!st || !(st.kind === "unknown" || ((st.kind === "unsent" || st.kind === "unanswered") && st.retry))) return;
    if (st.kind === "unknown") {
      this.set({ phase: "checking" });
      const answered = await this.reconcile(key, turn.text, new Set(this.knownIds(key)));
      if (answered !== "unanswered" && answered !== "unsent") {
        this.set({ phase: "idle" });
        return;
      }
    }
    this.set((s) => ({
      items: s.items.filter((it) => !(it.partial && it.partial !== "streaming" && it.key === `${key}-reply`)),
    }));
    this.patchItem(key, { status: { kind: "sending" } });
    void this.exchange(key, turn.text);
  }

  private knownIds(exceptKey: string): string[] {
    return this.state.items.filter((it) => it.key !== exceptKey && it.id).map((it) => it.id as string);
  }

  private async exchange(key: string, text: string): Promise<void> {
    const known = new Set(this.knownIds(key));
    const replyKey = `${key}-reply`;
    const ctrl = new AbortController();
    this.ctrl = ctrl;
    this.set({ phase: "waiting", live: "Агент пишет…", limit: null });
    let reply = "";
    let outcome: ChatOutcome;
    try {
      outcome = await this.api.chatSend(text, {
        signal: ctrl.signal,
        onDelta: (chunk) => {
          if (reply === "") {
            this.set((s) => ({
              phase: "streaming",
              items: [
                ...s.items.filter((it) => it.key !== replyKey),
                { key: replyKey, id: null, author: "agent", text: chunk, partial: "streaming" },
              ],
            }));
          } else {
            this.patchItem(replyKey, { text: reply + chunk });
          }
          reply += chunk;
        },
      });
    } catch (e) {
      this.ctrl = null;
      await this.failed(key, text, reply, known, e);
      return;
    }
    this.ctrl = null;
    this.finish(key, replyKey, reply, outcome);
  }

  private finish(key: string, replyKey: string, shown: string, outcome: ChatOutcome): void {
    if (outcome.kind === "done") {
      this.patchItem(key, { status: { kind: "answered" } });
      this.patchItem(replyKey, { id: outcome.data.message_id, partial: undefined });
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
          ...s.items.filter((it) => it.key !== replyKey),
          { key: c.messageId ?? replyKey, id: c.messageId, author: "agent", text, crisis: c.contacts },
        ],
        live: `Ответ агента: ${text} Ниже — контакты помощи.`,
        outcome: this.next("crisis"),
      }));
      return;
    }
    // `error`: реплика сохранена (контракт), показанные куски не сохранены — помечаются прерванными.
    this.patchItem(replyKey, { partial: "interrupted" });
    const note = chatErrorText(outcome.code);
    this.patchItem(key, { status: { kind: "unanswered", note, retry: true } });
    this.set({ phase: "idle", live: "", outcome: this.next("failed") });
  }

  private async failed(key: string, text: string, shown: string, known: Set<string>, e: unknown): Promise<void> {
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
    // Ответы до сохранения реплики: ничего не сохранено, повтор безопасен.
    if (err.code === "conflict_retry" || err.status === 401 || err.status === 422) {
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
    // `unknown` остаётся и после 200: ответ мог сохраниться (не дошло только `done`) — повтор сначала проверит историю.
    const result = await this.reconcile(key, text, known, note);
    this.set({ phase: "idle", outcome: this.next(result === "answered" ? "done" : "failed") });
  }

  /**
   * Что сервер успел сохранить: первая страница истории, новые реплики (которых не было до отправки).
   * `answered` — ответ агента сохранён (показывается он); `unanswered` — реплика сохранена без ответа;
   * `unsent` — реплики нет; `unknown` — историю прочитать не удалось.
   */
  private async reconcile(
    key: string,
    text: string,
    known: Set<string>,
    note = chatErrorText("stream_broken"),
  ): Promise<"answered" | "unanswered" | "unsent" | "unknown"> {
    let page: ChatHistory;
    try {
      page = await this.api.chatHistory({ limit: PAGE });
    } catch {
      this.patchItem(key, {
        status: { kind: "unknown", note: `${note} Не удалось проверить, дошло ли сообщение.` },
      });
      return "unknown";
    }
    const unseen = chronological(page).filter((it) => it.id && !known.has(it.id));
    // Прежние реплики владельца этой сессии, сохранённые сервером, но без id у клиента (сервер id реплики
    // владельца не возвращает: ответ `done`, лимит 429 и т. п.), — это они же, а не новые: сопоставляются по
    // тексту по порядку и получают id, чтобы не показаться второй раз.
    const earlier = this.state.items.filter(
      (it) => it.author === "owner" && it.id === null && it.key !== key && it.status && it.status.kind !== "unsent" && it.status.kind !== "unknown",
    );
    const ids = new Map<string, string>();
    const fresh = unseen.filter((it) => {
      if (it.author !== "owner") return true;
      const j = earlier.findIndex((e) => e.text === it.text);
      if (j < 0) return true;
      ids.set(earlier[j].key, it.id as string);
      earlier.splice(0, j + 1);
      return false;
    });
    let ownerAt = -1;
    for (let i = fresh.length - 1; i >= 0; i--) {
      if (fresh[i].author === "owner" && fresh[i].text === text) {
        ownerAt = i;
        break;
      }
    }
    const answered = ownerAt >= 0 && fresh.slice(ownerAt + 1).some((it) => it.author === "agent");
    const result = ownerAt < 0 ? "unsent" : answered ? "answered" : "unanswered";
    const status: OwnerStatus =
      result === "answered"
        ? { kind: "answered" }
        : result === "unanswered"
          ? { kind: "unanswered", note, retry: true }
          : { kind: "unsent", note: `${note} Сообщение не сохранилось.`, retry: true };
    this.set((s) => {
      const keep = s.items
        .filter((it) => it.key !== key && !(answered && it.key === `${key}-reply`))
        .map((it) => (ids.has(it.key) ? { ...it, id: ids.get(it.key) as string } : it));
      const tail = fresh.map((it, i) => (i === ownerAt ? { ...it, key, status } : it));
      if (ownerAt < 0) {
        const own = s.items.find((it) => it.key === key);
        if (own) tail.push({ ...own, status });
      }
      // Недописанный ответ остаётся под своей репликой с пометкой «прерван», если ответа в истории нет.
      const partial = answered ? [] : keep.filter((it) => it.key === `${key}-reply`);
      const rest = keep.filter((it) => it.key !== `${key}-reply`);
      return {
        items: [...rest, ...tail, ...partial],
        live: answered ? `Ответ агента: ${fresh.slice(ownerAt + 1).find((it) => it.author === "agent")?.text ?? ""}` : s.live,
      };
    });
    return result;
  }

  private next(kind: "done" | "crisis" | "failed") {
    return { seq: (this.state.outcome?.seq ?? 0) + 1, kind };
  }
}

/** Кризисное событие пришло без текста: контакты помощи всё равно показываются (ТЗ §9.3). */
export const CRISIS_FALLBACK =
  "То, что ты описываешь, звучит серьёзно. Ты не один и не одна с этим. Пожалуйста, обратись за помощью прямо сейчас — контакты ниже.";

function errText(e: unknown, op: "chatHistory"): string {
  return e instanceof ApiError ? e.message : errorText("internal", undefined, undefined, op);
}
