import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { ApiError } from "../api/client";
import { MEMORY_TYPES } from "../api/memory";
import { errorText } from "../api/messages";
import type { MemoryItem, MemoryType } from "../api/types";
import { useApp } from "../app/store";
import { useTelegram } from "../telegram/context";
import { formatDay } from "../ui/format";
import { Heading } from "../ui/Heading";
import { Loading, Notice } from "../ui/Notice";

/** Названия категорий — по прототипу 0.6 в порядке таблицы ТЗ §6.3 (вопрос 1 карточки 2.5, Г1). */
export const CATEGORY: Record<MemoryType, string> = {
  fact: "Факты",
  date: "Важные даты",
  preference: "Предпочтения",
  sensitive_topic: "Чувствительные темы",
  observation: "Наблюдения агента",
  from_partner: "Из сообщений партнёра",
};

/** Пояснение под заголовком категории: записи не из слов владельца отделены подписью (ТЗ §6.3, ФТ-14). */
const CATEGORY_NOTE: Partial<Record<MemoryType, string>> = {
  observation: "Вывод агента, а не твои слова.",
  from_partner: "Из сообщений, которые партнёр тебе отправил.",
};

/** Источник записи — коротко, в строке под текстом. */
const SOURCE: Record<MemoryItem["source"], string> = {
  owner_dialog: "твои слова",
  agent_inference: "вывод агента",
  partner_message: "сообщение партнёра",
};

/** Значок у записи не из слов владельца — видимое отличие, не только цвет. */
const BADGE: Partial<Record<MemoryItem["source"], string>> = {
  agent_inference: "вывод агента, а не твои слова",
  partner_message: "от партнёра",
};

/**
 * Текст подтверждения не обещает больше, чем делает сервер (АР-37 на подтверждении оператора): запись удаляется
 * навсегда и не извлекается повторно (ТЗ §6.3). Про краткие сводки и недавние реплики — ничего.
 */
export const CONFIRM_TEXT = "Удалить навсегда? Запись не восстановится, и агент не запомнит это снова.";

/** Ниже этого уровня уверенности вывод агента помечается «агент не уверен» (вопрос 3 карточки 2.5). */
export const LOW_CONFIDENCE = 0.5;

type Load = { kind: "loading" } | { kind: "error"; text: string } | { kind: "ready"; items: MemoryItem[] };

const textOf = (e: unknown) => (e instanceof ApiError ? e.message : errorText("internal"));
const plain = (m: MemoryItem): string | null => (typeof m.text === "string" ? m.text : null);

function without(rec: Readonly<Record<string, string>>, id: string): Record<string, string> {
  const out = { ...rec };
  delete out[id];
  return out;
}

/** «14 июня 2027 г.» — дата события без часового пояса (контракт: `date`, день как есть). */
function formatEventDate(day: string): string {
  const [y, mo, d] = day.split("-").map(Number);
  const dt = new Date(y, mo - 1, d);
  dt.setFullYear(y);
  return dt.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" });
}

/**
 * «Что агент помнит» (ФТ-13; ТЗ §2 п.6, §6.3, §8): свои записи памяти по шести категориям, удаление любой записи
 * с подтверждением на месте. Партнёр и его агент записи не видят — экран говорит об этом. Тексты записей — только
 * текст React, только в памяти экрана: не журналируются, не сохраняются в хранилищах браузера и не попадают в адрес.
 */
export function Memory() {
  const { api } = useApp();
  const tg = useTelegram();
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [failed, setFailed] = useState<Readonly<Record<string, string>>>({});
  const [live, setLive] = useState("");
  const inFlight = useRef(new Set<string>());
  const alive = useRef(true);
  const delButtons = useRef(new Map<string, HTMLButtonElement>());
  const cancelRef = useRef<HTMLButtonElement>(null);
  /** Куда перевести фокус после отрисовки: кнопка «Удалить» записи, «Отмена» подтверждения или заголовок экрана. */
  const focusNext = useRef<{ to: "delete"; id: string } | { to: "cancel" } | { to: "heading" } | null>(null);
  const confirmQ = useId();

  const fetchList = useCallback(() => {
    setLoad({ kind: "loading" });
    api.memoryList().then(
      (items) => alive.current && setLoad({ kind: "ready", items }),
      (e: unknown) => alive.current && setLoad({ kind: "error", text: textOf(e) }),
    );
  }, [api]);

  useEffect(() => {
    alive.current = true;
    fetchList();
    return () => {
      alive.current = false;
    };
  }, [fetchList]);

  useLayoutEffect(() => {
    const f = focusNext.current;
    if (!f) return;
    focusNext.current = null;
    if (f.to === "cancel") cancelRef.current?.focus();
    else if (f.to === "delete") delButtons.current.get(f.id)?.focus();
    else document.querySelector<HTMLElement>("h1")?.focus();
  });

  const items = load.kind === "ready" ? load.items : [];
  // Порядок записей на экране: по категориям ТЗ §6.3, внутри — как прислал сервер.
  const ordered = MEMORY_TYPES.flatMap((t) => items.filter((m) => m.type === t));
  const orderedRef = useRef(ordered);
  orderedRef.current = ordered;

  function ask(id: string) {
    setConfirmId(id);
    setFailed((f) => without(f, id));
    focusNext.current = { to: "cancel" };
  }

  function cancel(id: string) {
    setConfirmId(null);
    focusNext.current = { to: "delete", id };
  }

  /** Было ли внимание человека на этой записи: тогда после её удаления фокус переходит к соседней. */
  function focusWasOn(id: string): boolean {
    const el = document.activeElement;
    if (!el || el === document.body) return true;
    return el.closest("[data-memory-id]")?.getAttribute("data-memory-id") === id;
  }

  async function remove(id: string) {
    // Двойное нажатие и повтор, пока запрос идёт, — один запрос.
    if (inFlight.current.has(id)) return;
    inFlight.current.add(id);
    tg.impact("light");
    setConfirmId((c) => (c === id ? null : c));
    setFailed((f) => without(f, id));
    setBusy((s) => new Set(s).add(id));
    focusNext.current = { to: "delete", id };
    setLive("Удаляем запись…");
    try {
      const result = await api.memoryDelete(id);
      if (!alive.current) return;
      const now = orderedRef.current;
      const idx = now.findIndex((m) => m.id === id);
      const neighbour = idx < 0 ? undefined : (now[idx + 1] ?? now[idx - 1]);
      if (focusWasOn(id)) focusNext.current = neighbour ? { to: "delete", id: neighbour.id } : { to: "heading" };
      setLoad((l) => (l.kind === "ready" ? { kind: "ready", items: l.items.filter((m) => m.id !== id) } : l));
      if (result === "deleted") {
        tg.notify("success");
        setLive("Запись удалена.");
      } else {
        setLive("Эта запись уже была удалена.");
      }
    } catch (e) {
      if (!alive.current) return;
      tg.notify("error");
      setFailed((f) => ({ ...f, [id]: textOf(e) }));
      setLive("");
    } finally {
      inFlight.current.delete(id);
      if (alive.current) {
        setBusy((s) => {
          const n = new Set(s);
          n.delete(id);
          return n;
        });
      }
    }
  }

  function row(m: MemoryItem) {
    const text = plain(m);
    const category = CATEGORY[m.type];
    const isBusy = busy.has(m.id);
    const error = failed[m.id];
    const confirming = confirmId === m.id && !isBusy;
    const badge = BADGE[m.source];
    const meta = [`Источник: ${SOURCE[m.source]}`, formatDay(m.created_at)];
    if (m.source === "agent_inference" && m.confidence < LOW_CONFIDENCE) meta.push("агент не уверен");
    const name = text === null ? "текст записи недоступен" : `«${text}»`;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") cancel(m.id);
    };
    return (
      <li
        key={m.id}
        className={`mem-item${m.source === "owner_dialog" ? "" : " mem-item--other"}`}
        data-memory-id={m.id}
        data-source={m.source}
      >
        <div className="grow">
          {text === null ? (
            <p className="mem-item__text mem-item__text--unavailable">Текст записи недоступен</p>
          ) : (
            <p className="mem-item__text">{text}</p>
          )}
          {text === null && <p className="mem-item__meta">Не получилось прочитать запись. Её можно удалить.</p>}
          {m.event_date && <p className="mem-item__date">Дата: {formatEventDate(m.event_date)}</p>}
          {badge && <span className="badge">{badge}</span>}
          <p className="mem-item__meta">{meta.filter(Boolean).join(" · ")}</p>
          {confirming && (
            <div className="confirm" role="group" aria-labelledby={confirmQ} onKeyDown={onKey}>
              <p className="confirm__q small" id={confirmQ}>
                {CONFIRM_TEXT}
              </p>
              <button
                type="button"
                className="btn btn--fill"
                aria-label="Удалить навсегда"
                onClick={() => void remove(m.id)}
              >
                Удалить
              </button>
              <button type="button" className="btn btn--ghost" ref={cancelRef} onClick={() => cancel(m.id)}>
                Отмена
              </button>
            </div>
          )}
          {error && (
            <div className="mem-item__fail">
              <Notice kind="error">{error}</Notice>
              <button
                type="button"
                className="btn btn--ghost"
                aria-label={`Повторить удаление: ${name} (${category})`}
                onClick={() => void remove(m.id)}
              >
                Повторить
              </button>
            </div>
          )}
        </div>
        {!confirming && (
          <button
            type="button"
            className="mem-del"
            ref={(el) => {
              if (el) delButtons.current.set(m.id, el);
              else delButtons.current.delete(m.id);
            }}
            // Не `disabled`: фокус остаётся на кнопке, пока идёт запрос; повторное нажатие гасит `inFlight`.
            aria-disabled={isBusy || undefined}
            aria-busy={isBusy || undefined}
            aria-label={isBusy ? `Удаляем: ${name} (${category})` : `Удалить запись: ${name} (${category})`}
            onClick={() => !isBusy && ask(m.id)}
          >
            {isBusy ? "Удаляем…" : "Удалить"}
          </button>
        )}
      </li>
    );
  }

  return (
    <section data-screen="memory">
      <Heading>Что агент помнит</Heading>
      <div className="note" data-private-note>
        <span className="note__icon" aria-hidden="true">
          i
        </span>
        <span>
          <b>Партнёр и его агент этого не видят.</b> Эти записи видишь только ты и твой агент. Любую можно удалить.
        </span>
      </div>

      {load.kind === "loading" && <Loading>Загружаем память…</Loading>}
      {load.kind === "error" && (
        <>
          <Notice kind="error">{load.text}</Notice>
          <button type="button" className="btn btn--block btn--ghost" onClick={fetchList}>
            Попробовать снова
          </button>
        </>
      )}

      {load.kind === "ready" && items.length === 0 && (
        <div className="card card--soft" data-empty>
          <p>
            <b>Пока агент ничего не запомнил.</b>
          </p>
          <p className="small">
            Записи появляются после разговоров с агентом: факты о тебе, важные даты, что тебе нравится и о чём
            говорить бережно. Отдельно — наблюдения самого агента и то, что партнёр написал тебе в отправленных
            сообщениях. Любую запись можно будет удалить.
          </p>
        </div>
      )}

      {load.kind === "ready" &&
        items.length > 0 &&
        MEMORY_TYPES.map((t) => {
          const list = items.filter((m) => m.type === t);
          const headingId = `mem-cat-${t}`;
          return (
            <div className="mem-cat" key={t} data-category={t}>
              <h2 id={headingId}>{CATEGORY[t]}</h2>
              {CATEGORY_NOTE[t] && <p className="mem-cat__note small muted">{CATEGORY_NOTE[t]}</p>}
              {list.length > 0 ? (
                <ul className="mem-list" aria-labelledby={headingId}>
                  {list.map(row)}
                </ul>
              ) : (
                <p className="small muted">Пока пусто</p>
              )}
            </div>
          );
        })}

      <div className="visually-hidden" role="status" aria-live="polite" data-live>
        {live}
      </div>
    </section>
  );
}
