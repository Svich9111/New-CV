import { useEffect, useId, useLayoutEffect, useRef, useSyncExternalStore, type KeyboardEvent } from "react";
import { useApp } from "../app/store";
import { MAX_TEXT, visibleItems, type ChatItem, type ChatSession, type ChatState } from "../chat/session";
import { DOUBLE_TAP_MS } from "../telegram/MainButton";
import { useTelegram } from "../telegram/context";
import { Heading } from "../ui/Heading";
import { Loading, Notice } from "../ui/Notice";
import { HelpContacts } from "./Help";

/** С какой длины показывать счётчик символов. */
const COUNTER_FROM = 3500;

const SEND_ICON = (
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path d="M3 20.5 21 12 3 3.5 3 10l12 2-12 2z" />
  </svg>
);

function useChat(chat: ChatSession): ChatState {
  return useSyncExternalStore(chat.subscribe, chat.getSnapshot);
}

/** Подпись под репликой владельца: что с ней (ТЗ §8 — состояние словами, не цветом) и повтор. */
function OwnerCaption({ item, chat, phase }: { item: ChatItem; chat: ChatSession; phase: ChatState["phase"] }) {
  const st = item.status;
  if (!st || st.kind === "answered") return null;
  if (st.kind === "sending") {
    if (phase !== "waiting") return null;
    return <span className="msg__meta">Отправляем…</span>;
  }
  if (st.kind === "limited") return <span className="msg__meta">Сохранено. Ответа пока нет — лимит на сегодня.</span>;
  const saved =
    st.kind === "unsent" ? "Не отправлено." : st.kind === "unanswered" ? "Сообщение сохранено, ответа нет." : "";
  const retry = st.kind === "unknown" || st.retry;
  return (
    <div className="msg__fail" role="alert">
      <span>
        <span className="visually-hidden">Ошибка: </span>
        {st.note} {saved}
      </span>
      {retry && (
        <button
          type="button"
          className="btn btn--ghost msg__retry"
          disabled={phase !== "idle"}
          onClick={() => void chat.retry(item.key)}
        >
          {st.kind === "unsent" ? "Отправить ещё раз" : st.kind === "unknown" ? "Проверить и повторить" : "Повторить"}
        </button>
      )}
    </div>
  );
}

function AgentCaption({ item }: { item: ChatItem }) {
  if (item.partial === "streaming") return <span className="msg__meta">Агент пишет…</span>;
  if (item.partial === "interrupted") return <span className="msg__meta">Ответ прервался и не сохранён.</span>;
  if (item.partial === "stopped") return <span className="msg__meta">Ответ остановлен и не сохранён.</span>;
  return null;
}

/** Текст реплики — только текстом React (без разметки): переносы строк сохраняет CSS `pre-wrap`. */
function MessageText({ text }: { text: string | null }) {
  if (text === null) return <span className="msg__text muted">Текст недоступен: его не удалось расшифровать.</span>;
  return <span className="msg__text">{text}</span>;
}

/**
 * Чат с личным агентом (ФТ-05, ТЗ §4.2, §8): история с подгрузкой по курсору, реплика, потоковый ответ,
 * «Агент пишет…», остановка и повтор без дубля, кризисный ответ с контактами помощи прямо на экране (§9.3),
 * дневной лимит со временем. Разговор приватен — это сказано на экране (принцип 4).
 */
export function Chat() {
  const { chat, push } = useApp();
  const tg = useTelegram();
  const state = useChat(chat);
  const inputId = useId();
  const counterId = useId();
  const end = useRef<HTMLDivElement>(null);
  const lastOutcome = useRef(state.outcome?.seq ?? 0);
  // «Остановить» появляется на месте «Отправить»: второе касание двойного нажатия не должно остановить ответ.
  const sentAt = useRef(0);

  useEffect(() => {
    chat.start();
  }, [chat]);

  // Тактильный отклик на итог обмена: ошибка — `error`; успешный ответ и кризис — без вибрации.
  useEffect(() => {
    const o = state.outcome;
    if (!o || o.seq === lastOutcome.current) return;
    lastOutcome.current = o.seq;
    if (o.kind === "failed") tg.notify("error");
  }, [state.outcome, tg]);

  const items = visibleItems(state.items);
  const last = items[items.length - 1];
  // Новая реплика и куски ответа — лента прокручивается к концу (если человек не листает историю выше).
  useLayoutEffect(() => {
    const doc = document.documentElement;
    const nearBottom = window.innerHeight + window.scrollY >= doc.scrollHeight - 160;
    if (nearBottom) end.current?.scrollIntoView?.({ block: "end" });
  }, [items.length, last?.text, state.phase]);

  const inFlight = state.phase === "waiting" || state.phase === "streaming";
  const canSend =
    !chat.busy && state.input.trim().length > 0 && state.input.length <= MAX_TEXT && state.history !== "loading";

  const send = () => {
    if (!canSend) return;
    tg.impact("light");
    if (chat.send()) sentAt.current = Date.now();
  };

  const stop = () => {
    if (Date.now() - sentAt.current < DOUBLE_TAP_MS) return;
    tg.impact("light");
    chat.stop();
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter — отправить, Shift+Enter — новая строка (как в прототипе); во время набора IME — не отправлять.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  return (
    <section className="chat-screen" data-screen="chat">
      <Heading>Твой агент</Heading>
      <p className="chat-privacy small muted">
        Этот разговор видишь только ты. Партнёр и его агент его не видят — им уходит только то, что ты отправишь
        кнопкой «Отправить».
      </p>

      {state.history === "loading" && <Loading>Загружаем переписку…</Loading>}
      {state.history === "failed" && (
        <>
          <Notice kind="error">{state.historyError}</Notice>
          <button
            type="button"
            className="btn btn--block btn--ghost"
            disabled={state.phase !== "idle"}
            onClick={() => chat.retryHistory()}
          >
            Загрузить переписку снова
          </button>
        </>
      )}
      {state.restarted && (
        <p className="small muted" role="status">
          Переписку пришлось загрузить заново — более ранние сообщения можно открыть снова.
        </p>
      )}
      {state.history === "loaded" && state.cursor && (
        <button
          type="button"
          className="btn btn--block btn--text"
          disabled={state.olderLoading || state.phase !== "idle"}
          aria-busy={state.olderLoading || undefined}
          onClick={() => void chat.loadOlder()}
        >
          {state.olderLoading ? "Загружаем более ранние…" : "Показать более ранние сообщения"}
        </button>
      )}
      {state.olderError && <Notice kind="error">{state.olderError}</Notice>}
      {state.history === "loaded" && items.length === 0 && (
        <p className="chat-empty muted">
          Расскажи, что тебя беспокоит. Агент поможет разобраться в чувствах и, если захочешь, подготовить сообщение
          партнёру.
        </p>
      )}

      <ol className="chat" aria-label="Переписка с агентом">
        {items.map((it) => (
          <li
            key={it.key}
            className={`msg msg--${it.author === "owner" ? "own" : "agent"}${it.partial && it.partial !== "streaming" ? " msg--partial" : ""}`}
            data-author={it.author}
            aria-busy={it.partial === "streaming" || undefined}
          >
            <span className="visually-hidden">{it.author === "owner" ? "Ты: " : "Агент: "}</span>
            <MessageText text={it.text} />
            {it.author === "owner" ? <OwnerCaption item={it} chat={chat} phase={state.phase} /> : <AgentCaption item={it} />}
            {it.crisis && (
              <section className="crisis" aria-label="Контакты помощи">
                <h2 className="crisis__title">Помощь рядом</h2>
                <HelpContacts contacts={it.crisis} />
                <button type="button" className="btn btn--block btn--ghost" onClick={() => push({ name: "help" })}>
                  Все контакты помощи
                </button>
              </section>
            )}
          </li>
        ))}
        {state.phase === "waiting" && (
          <li className="msg msg--agent msg--typing" data-typing>
            Агент пишет…
          </li>
        )}
        {state.phase === "checking" && (
          <li className="msg msg--agent msg--typing" data-checking>
            Проверяем, дошло ли сообщение…
          </li>
        )}
      </ol>

      {state.limit !== null && (
        <Notice>
          <b>На сегодня лимит сообщений агенту исчерпан.</b>{" "}
          {state.limit ? `Агент снова ответит ${state.limit}.` : "Агент снова ответит завтра."} Твоё сообщение
          сохранено. Если тебе сейчас опасно или очень тяжело — всё равно напиши: в этом случае агент ответит и сверх
          лимита, а контакты помощи доступны всегда.
        </Notice>
      )}

      {/* Объявления для экранного диктора: «Агент пишет…», затем ответ целиком — куски потока не зачитываются. */}
      <div className="visually-hidden" role="status" aria-live="polite" data-live>
        {state.live}
      </div>
      <div ref={end} />

      <div className="chat-bottom">
        <div className="composer">
          <label htmlFor={inputId} className="visually-hidden">
            Сообщение агенту
          </label>
          <textarea
            id={inputId}
            rows={1}
            value={state.input}
            maxLength={MAX_TEXT}
            placeholder="Что тебя беспокоит?"
            aria-describedby={state.input.length >= COUNTER_FROM ? counterId : undefined}
            onChange={(e) => chat.setInput(e.target.value)}
            onKeyDown={onKey}
          />
          {inFlight ? (
            <button type="button" className="btn btn--ghost composer__stop" onClick={stop}>
              Остановить
            </button>
          ) : (
            <button
              type="button"
              className="icon-btn icon-btn--fill"
              aria-label="Отправить агенту"
              disabled={!canSend}
              onClick={send}
            >
              {SEND_ICON}
            </button>
          )}
        </div>
        {state.input.length >= COUNTER_FROM && (
          <p className="composer__counter small muted" id={counterId}>
            {state.input.length} из {MAX_TEXT} символов
          </p>
        )}
        <button type="button" className="help-link" onClick={() => push({ name: "help" })}>
          Помощь и контакты служб поддержки
        </button>
      </div>
    </section>
  );
}
