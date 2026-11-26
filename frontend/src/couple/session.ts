import type { DraftResult } from "../api/couple";
import type { Draft, ExportText } from "../api/types";

/**
 * Черновик на экране (задача 3.4). Живёт только в памяти сессии Mini App — ни в localStorage, ни в sessionStorage,
 * ни в адресе (как `ChatSession` 2.2): после перезапуска приложения черновик не находится (предложение A1 —
 * `GET /drafts?status=draft`, карточка 3.4).
 */
export interface DraftMemory {
  /** Черновик сервера, который видел автор: текст и версия — то, что подтверждает «Отправить». */
  draft: Draft;
  /** Текст в поле правки (правка автора). Совпадает с `draft.text`, пока автор не правил. */
  text: string;
  /**
   * Правка автора, которую заменил актуальный текст после 409 `version_conflict` (ничего не отправлено):
   * автор может вернуть её одним нажатием — правка не теряется (§10).
   */
  lostEdit: string | null;
}

/**
 * Ответ на конкретное входящее («Ответить с агентом», АР-56, Б-30): id сообщения хранится у контекста ответа, а не
 * общим флагом — второе «Ответить с агентом» заменяет его id второго сообщения.
 */
export interface ReplyContext {
  id: string;
  /** Когда сообщение отправлено (для подписи «Ответ на сообщение от …»). */
  sentAt: string;
}

/** Состояние пары на время сессии Mini App: черновик, контекст ответа, показанные оригиналы, отметки «прочитано». */
export class CoupleSession {
  draft: DraftMemory | null = null;
  reply: ReplyContext | null = null;
  /**
   * Идущий `POST /drafts`: повторное открытие экрана (в том числе двойной запуск эффекта в StrictMode) ждёт этот же
   * запрос, а не создаёт второй черновик.
   */
  creating: { replyTo: string | null; promise: Promise<DraftResult> } | null = null;
  /** Оригиналы, уже открытые в этой сессии: повторное «Показать оригинал» — без нового запроса. */
  readonly originals = new Map<string, ExportText>();
  /** Полученные, для которых `read` уже принят сервером: одна отметка на сообщение. */
  readonly readDone = new Set<string>();
  /** Отметки «прочитано», которые идут прямо сейчас (второй показ не шлёт второй запрос). */
  readonly readPending = new Set<string>();
  private readonly listeners = new Set<() => void>();

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  /** Версия состояния — для `useSyncExternalStore`: меняется на каждое изменение. */
  private version = 0;
  getVersion = (): number => this.version;

  changed(): void {
    this.version += 1;
    for (const fn of [...this.listeners]) fn();
  }

  setReply(reply: ReplyContext | null): void {
    this.reply = reply;
    this.changed();
  }

  setDraft(draft: DraftMemory | null): void {
    this.draft = draft;
    this.changed();
  }
}
