import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore, type KeyboardEvent } from "react";
import { useApp } from "../app/store";
import { MAX_TEXT, isBlank, visibleItems, type ChatItem, type ChatSession, type ChatState } from "../chat/session";
import { DOUBLE_TAP_MS } from "../telegram/MainButton";
import { useTelegram } from "../telegram/context";
import { Heading } from "../ui/Heading";
import { Loading, Notice } from "../ui/Notice";
import { HelpContacts } from "./Help";

/** С какой длины показывать счётчик символов. */
const COUNTER_FROM = 3500;
/** Окно ближе этого к низу ленты — лента «прилипла» и следует за новыми репликами. */
const STICK_PX = 48;
/** Зазор между последним пузырём и закреплённым полем ввода при прокрутке. */
const GAP_PX = 12;

/**
 * Строка о помощи под ошибкой ответа агента (АР-34 п.2, ТЗ §9.3): ответа модели нет — это могла быть
 * нераспознанная реплика человека в кризисе, поэтому номер и переход в «Помощь» — прямо здесь.
 */
function HelpLine({ onHelp }: { onHelp: () => void }) {
  return (
    <span className="help-line">
      Если тебе сейчас опасно или очень тяжело — звони <a href="tel:112">112</a>.{" "}
      <button type="button" className="help-line__more" onClick={onHelp}>
        Все контакты помощи
      </button>
    </span>
  );
}

const SEND_ICON = (
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path d="M3 20.5 21 12 3 3.5 3 10l12 2-12 2z" />
  </svg>
);

function useChat(chat: ChatSession): ChatState {
  return useSyncExternalStore(chat.subscribe, chat.getSnapshot);
}

/** Дольше этого `setTimeout` не ждёт (2^31−1 мс): более далёкое окно перепроверяется по частям. */
const MAX_TIMER_MS = 2_147_483_647;

/** Наступило ли время `at` по часам сессии; до него — перерисовка в момент `at`. `undefined` — никогда. */
function useReached(at: number | undefined, now: () => number): boolean {
  const [, tick] = useState(0);
  const reached = at !== undefined && now() >= at;
  useEffect(() => {
    if (at === undefined || reached) return;
    const timer = setTimeout(() => tick((n) => n + 1), Math.min(Math.max(at - now(), 0) + 50, MAX_TIMER_MS));
    return () => clearTimeout(timer);
  });
  return reached;
}

/**
 * Подпись реплики под дневным лимитом (429): до конца окна — без повтора; после окна (по `Retry-After`) —
 * «Повторить» тем же ключом: агент ответит на эту же сохранённую реплику (АР-51, MINOR-N5).
 */
function LimitedCaption({ item, chat, phase, retryAt, onRetry }: {
  item: ChatItem;
  chat: ChatSession;
  phase: ChatState["phase"];
  retryAt: number | undefined;
  onRetry: () => void;
}) {
  const ready = useReached(retryAt, chat.now);
  if (!ready) return <span className="msg__meta">Сохранено. Ответа пока нет — лимит на сегодня.</span>;
  return (
    <div className="msg__fail">
      <span>Сохранено. Ответа пока нет — лимит закончился, можно повторить.</span>
      <button
        type="button"
        className="btn btn--ghost msg__retry"
        disabled={phase !== "idle"}
        onClick={() => {
          onRetry();
          void chat.retry(item.key);
        }}
      >
        Повторить
      </button>
    </div>
  );
}

/** Подпись под репликой владельца: что с ней (ТЗ §8 — состояние словами, не цветом) и повтор. */
function OwnerCaption({
  item,
  chat,
  phase,
  onRetry,
  onHelp,
}: {
  item: ChatItem;
  chat: ChatSession;
  phase: ChatState["phase"];
  onRetry: () => void;
  onHelp: () => void;
}) {
  const st = item.status;
  if (!st || st.kind === "answered") return null;
  if (st.kind === "sending") {
    if (phase !== "waiting") return null;
    return <span className="msg__meta">Отправляем…</span>;
  }
  if (st.kind === "limited") return <LimitedCaption item={item} chat={chat} phase={phase} retryAt={st.retryAt} onRetry={onRetry} />;
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
          onClick={() => {
            onRetry();
            void chat.retry(item.key);
          }}
        >
          {st.kind === "unsent" ? "Отправить ещё раз" : st.kind === "unknown" ? "Проверить и повторить" : "Повторить"}
        </button>
      )}
      {(st.kind === "unanswered" || st.kind === "unsent") && st.help && <HelpLine onHelp={onHelp} />}
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
  const screenRef = useRef<HTMLElement>(null);
  const list = useRef<HTMLOListElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  /** Лента у низа — следует за новым. Считается по прокрутке человека, то есть до роста пузыря. */
  const follow = useRef(true);
  /** Прокрутить к концу при следующей отрисовке, даже если человек читает выше: своя отправка или повтор. */
  const force = useRef(false);
  const lastCrisis = useRef(state.outcome?.kind === "crisis" ? state.outcome.seq : 0);
  /** Человек читает выше, а внизу появилось новое — кнопка «К новым». */
  const [behind, setBehind] = useState(false);
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

  /** Высота закреплённого низа (поле ввода, «Помощь») — отступ прокрутки, чтобы пузырь не уходил под него. */
  const bottomHeight = useCallback(() => bottom.current?.getBoundingClientRect().height ?? 0, []);
  const syncBottom = useCallback(() => {
    screenRef.current?.style.setProperty("--chat-bottom-h", `${Math.ceil(bottomHeight())}px`);
  }, [bottomHeight]);

  /** К концу ленты: последний пузырь (и уведомление о лимите) — над полем ввода. */
  const toEnd = useCallback(() => {
    syncBottom();
    end.current?.scrollIntoView?.({ block: "end" });
    follow.current = true;
    setBehind(false);
  }, [syncBottom]);

  /**
   * Кризис — всегда в зоне видимости (ТЗ §9.3): ответ протокола с блоком «Помощь рядом» целиком над полем ввода,
   * а если он не помещается — блок помощи с начала (номера, 112).
   */
  const toCrisis = useCallback(() => {
    syncBottom();
    const li = list.current?.lastElementChild;
    const block = li?.querySelector(".crisis");
    const room = window.innerHeight - bottomHeight() - 2 * GAP_PX;
    if (!li || !block || li.getBoundingClientRect().height <= room) {
      toEnd();
      return;
    }
    block.scrollIntoView?.({ block: "start" });
  }, [bottomHeight, syncBottom, toEnd]);

  // Прокрутка человека: у низа — лента следует за новым; ушёл выше читать — не дёргаем.
  useEffect(() => {
    const onScroll = () => {
      const doc = document.documentElement;
      follow.current = doc.scrollHeight - (window.scrollY + window.innerHeight) <= STICK_PX;
      if (follow.current) setBehind(false);
    };
    // Клавиатура телефона и поворот меняют высоту окна: прилипшая лента остаётся у низа.
    const onResize = () => {
      if (follow.current) toEnd();
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onResize);
    let ro: ResizeObserver | undefined;
    if (typeof ResizeObserver === "function" && bottom.current) {
      ro = new ResizeObserver(() => {
        syncBottom();
        if (follow.current) end.current?.scrollIntoView?.({ block: "end" });
      });
      ro.observe(bottom.current);
    }
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onResize);
      ro?.disconnect();
    };
  }, [syncBottom, toEnd]);

  // Новая реплика, начало и куски ответа, кризис, лимит — прокрутка по правилам выше.
  const crisisSeq = state.outcome?.kind === "crisis" ? state.outcome.seq : 0;
  const lastKey = last?.key;
  const lastText = last?.text;
  const lastStatus = last?.status?.kind;
  useLayoutEffect(() => {
    if (crisisSeq !== lastCrisis.current) {
      lastCrisis.current = crisisSeq;
      force.current = false;
      toCrisis();
      return;
    }
    if (force.current || follow.current) {
      force.current = false;
      toEnd();
      return;
    }
    if (lastKey !== undefined) setBehind(true);
    // Подгрузка более ранних сообщений сверху конец ленты не меняет — она сюда не попадает.
  }, [lastKey, lastText, lastStatus, state.phase, state.limit, crisisSeq, toEnd, toCrisis]);

  const inFlight = state.phase === "waiting" || state.phase === "streaming";
  const canSend =
    !chat.busy && !isBlank(state.input) && state.input.length <= MAX_TEXT && state.history !== "loading";

  const send = () => {
    if (!canSend) return;
    tg.impact("light");
    if (chat.send()) {
      sentAt.current = Date.now();
      force.current = true;
    }
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
    <section className="chat-screen" data-screen="chat" ref={screenRef}>
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

      <ol className="chat" aria-label="Переписка с агентом" ref={list}>
        {items.map((it) => (
          <li
            key={it.key}
            className={`msg msg--${it.author === "owner" ? "own" : "agent"}${it.partial && it.partial !== "streaming" ? " msg--partial" : ""}`}
            data-author={it.author}
            aria-busy={it.partial === "streaming" || undefined}
          >
            <span className="visually-hidden">{it.author === "owner" ? "Ты: " : "Агент: "}</span>
            <MessageText text={it.text} />
            {it.author === "owner" ? (
              <OwnerCaption
                item={it}
                chat={chat}
                phase={state.phase}
                onRetry={() => {
                  force.current = true;
                }}
                onHelp={() => push({ name: "help" })}
              />
            ) : (
              <AgentCaption item={it} />
            )}
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
          сохранено. Если тебе сейчас опасно или очень тяжело — всё равно напиши: на такое сообщение тебе ответят и
          сверх лимита и подскажут, куда обратиться. Экстренная помощь — <a href="tel:112">112</a>, контакты помощи
          доступны всегда.
        </Notice>
      )}

      {/* Объявления для экранного диктора: «Агент пишет…», затем ответ целиком — куски потока не зачитываются. */}
      <div className="visually-hidden" role="status" aria-live="polite" data-live>
        {state.live}
      </div>
      <div ref={end} className="chat-end" />

      <div className="chat-bottom" ref={bottom}>
        {behind && (
          <button type="button" className="btn btn--ghost chat-new" onClick={toEnd}>
            К новым <span aria-hidden="true">↓</span>
          </button>
        )}
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
