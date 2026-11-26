import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { errorText } from "../api/messages";
import type { ExportText, HelpContact, PartnerMessage } from "../api/types";
import { useApp } from "../app/store";
import { CRISIS_BLOCKS, crisisBlocks } from "../couple/crisis";
import type { CoupleSession } from "../couple/session";
import { useTelegram } from "../telegram/context";
import { formatMoment } from "../ui/format";
import { Heading } from "../ui/Heading";
import { Loading, Notice } from "../ui/Notice";
import { HelpLine } from "./Draft";
import { HelpContacts } from "./Help";

/** Размер страницы ленты. */
export const FEED_PAGE = 30;

/** Статус отправленного — словами (ТЗ §8: состояние подписью, не только цветом). */
export const OUTGOING_STATUS: Record<PartnerMessage["status"], string> = {
  processing: "Обрабатывается",
  delivered: "Доставлено",
  read: "Прочитано",
  answered: "Отвечено",
  withheld: "Не доставлено",
};

/** Статус полученного. */
export const INCOMING_STATUS: Partial<Record<PartnerMessage["status"], string>> = {
  delivered: "Новое",
  read: "Прочитано",
  answered: "Отвечено",
};

export const UNAVAILABLE = "Текст недоступен: его не удалось расшифровать.";

const plain = (t: ExportText): string | null => (typeof t === "string" ? t : null);
const errText = (e: unknown, op: "coupleMessages" | "messageOriginal") =>
  e instanceof ApiError ? e.message : errorText("internal", undefined, undefined, op);

/** Почему не доставлено — подпись для автора по причине (АР-44, Б-86). */
function withheldText(reason: PartnerMessage["withheld_reason"]): string {
  switch (reason) {
    case "safety":
      return "Не отправлено: сообщение остановлено проверкой безопасности. Партнёр его не получил и о нём не узнает.";
    case "model_refusal":
      return "Не доставлено: сервис не смог обработать это сообщение. Партнёр его не получил. Твой текст сохранён здесь.";
    case "pipeline_error":
      return "Не доставлено: сбой при обработке. Партнёр его не получил. Твой текст сохранён здесь — можно подготовить сообщение заново.";
    case "attempts_exhausted":
      return "Не доставлено: сервис не успел обработать сообщение. Партнёр его не получил. Твой текст сохранён здесь — можно подготовить сообщение заново.";
    case "couple_ended":
      return "Не доставлено: пара больше не активна.";
    default:
      return "Не доставлено. Партнёр это сообщение не получил.";
  }
}

/** Карточка показана на экране: колбэк один раз за показ (IntersectionObserver; без него — сразу). */
function useShown(enabled: boolean, onShown: () => void) {
  const ref = useRef<HTMLElement | null>(null);
  const cb = useRef(onShown);
  useLayoutEffect(() => {
    cb.current = onShown;
  });
  useEffect(() => {
    const el = ref.current;
    if (!enabled || !el) return;
    if (typeof IntersectionObserver !== "function") {
      cb.current();
      return;
    }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) cb.current();
    });
    io.observe(el);
    return () => io.disconnect();
  }, [enabled]);
  return ref;
}

/**
 * Отметка «прочитано» (`POST /couple/messages/{id}/read`): одна на сообщение за сессию; ошибка — молча, повтор при
 * следующем показе (сервер повтор принимает без изменений).
 */
function markRead(couple: CoupleSession, api: ReturnType<typeof useApp>["api"], id: string, onDone: () => void) {
  if (couple.readDone.has(id) || couple.readPending.has(id)) return;
  couple.readPending.add(id);
  api.messageRead(id).then(
    () => {
      couple.readPending.delete(id);
      couple.readDone.add(id);
      onDone();
    },
    () => {
      couple.readPending.delete(id);
    },
  );
}

function Outgoing({ m, contacts, onHelp, onChat }: { m: PartnerMessage; contacts: HelpContact[] | null; onHelp: () => void; onChat: () => void }) {
  const text = plain(m.text);
  const reason = m.status === "withheld" ? (m.withheld_reason ?? null) : null;
  return (
    <li className="feed__item feed__item--out" data-direction="outgoing" data-id={m.id} data-status={m.status}>
      <div className="sent">
        <span className="visually-hidden">Ты: </span>
        <span className={text === null ? "sent__text muted" : "sent__text"}>{text ?? UNAVAILABLE}</span>
        <span className="sent__status">
          Ты · {formatMoment(m.sent_at)} · <b>{OUTGOING_STATUS[m.status]}</b>
        </span>
      </div>
      {m.status === "processing" && (
        <p className="feed__note small muted">Сообщение проверяется и готовится к доставке.</p>
      )}
      {m.status === "withheld" && (
        <div className="feed__withheld small" data-withheld={reason ?? "unknown"}>
          <p>{withheldText(reason)}</p>
          {reason === "safety" && (
            <section className="crisis" aria-label="Контакты помощи" data-crisis={m.withheld_crisis ?? "general"}>
              {crisisBlocks(m.withheld_crisis).map((b) => (
                <p key={b} className="crisis__text">
                  {CRISIS_BLOCKS[b]}
                </p>
              ))}
              <h3 className="crisis__title">Помощь рядом</h3>
              <HelpContacts contacts={contacts ?? []} />
              <button type="button" className="btn btn--block btn--ghost" onClick={onHelp}>
                Все контакты помощи
              </button>
            </section>
          )}
          {reason === "model_refusal" && <HelpLine onHelp={onHelp} />}
          {(reason === "pipeline_error" || reason === "attempts_exhausted") && (
            // «Отправить снова» тот же текст контракт не позволяет (Б-86, АР-56) — подготовить заново через чат.
            <button type="button" className="btn btn--ghost" onClick={onChat}>
              Подготовить заново
            </button>
          )}
        </div>
      )}
    </li>
  );
}

function Incoming({
  m,
  partner,
  original,
  onToggle,
  onReply,
  onShown,
}: {
  m: PartnerMessage;
  partner: string;
  original: { open: boolean; loading: boolean; text?: ExportText; error?: string };
  onToggle: () => void;
  onReply: () => void;
  onShown: () => void;
}) {
  const ref = useShown(m.status === "delivered", onShown);
  const text = plain(m.text);
  const orig = original.text === undefined ? undefined : plain(original.text);
  return (
    <li className="feed__item" data-direction="incoming" data-id={m.id} data-status={m.status}>
      <article className="partner-card" ref={ref} aria-label={`Сообщение от партнёра, ${formatMoment(m.sent_at)}`}>
        <div className="partner-card__head">
          <span>
            От {partner} · {formatMoment(m.sent_at)} · <b>{INCOMING_STATUS[m.status]}</b>
          </span>
          {m.is_adapted && (
            <span className="badge" data-adapted>
              <span aria-hidden="true">✎ </span>адаптировано
            </span>
          )}
        </div>
        <p className={text === null ? "partner-card__text muted" : "partner-card__text"} data-delivered>
          {text ?? UNAVAILABLE}
        </p>
        {m.harsh_tone_warning && (
          <p className="small" data-harsh>
            <span aria-hidden="true">! </span>В оригинале резкий тон.
          </p>
        )}
        {m.is_adapted && (
          <p className="small muted">
            Агент смягчил форму, но не смысл. Оригинал — как его написал(а) и одобрил(а) {partner} — можно открыть.
          </p>
        )}
        {original.open && original.text !== undefined && (
          <div className="original" data-original>
            <span className="original__label">Оригинал — как написал(а) и одобрил(а) {partner}</span>
            <span className={orig === null ? "original__text muted" : "original__text"}>
              {orig ?? "Текст оригинала недоступен: его не удалось расшифровать."}
            </span>
          </div>
        )}
        {original.error && <Notice kind="error">{original.error}</Notice>}
        <div className="btn-row">
          {m.is_adapted && (
            <button
              type="button"
              className="btn btn--ghost"
              aria-expanded={original.open}
              aria-busy={original.loading || undefined}
              disabled={original.loading}
              onClick={onToggle}
            >
              {original.loading ? "Открываем оригинал…" : original.open ? "Скрыть оригинал" : "Показать оригинал"}
            </button>
          )}
          <button type="button" className="btn btn--fill" onClick={onReply}>
            Ответить с агентом
          </button>
        </div>
      </article>
    </li>
  );
}

type Load = { kind: "loading" } | { kind: "error"; text: string } | { kind: "ready" };

/**
 * Сообщения пары (ФТ-09…ФТ-11, ТЗ §4.4, §8): отправленные — свой оригинал и статус подписью, полученные — белая
 * карточка с красной линией слева, пометка «адаптировано» подписью, «В оригинале резкий тон», «Показать оригинал»
 * (только по нажатию), «Ответить с агентом». Сюда попадает только то, что отправлено кнопкой «Отправить»: личные
 * разговоры с агентами в ленте не появляются (принцип 4).
 */
export function Messages() {
  const { api, me, couple, push } = useApp();
  const tg = useTelegram();
  const [items, setItems] = useState<PartnerMessage[]>([]);
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [cursor, setCursor] = useState<string | null>(null);
  const [older, setOlder] = useState<{ loading: boolean; error: string | null }>({ loading: false, error: null });
  const [restarted, setRestarted] = useState(false);
  const [contacts, setContacts] = useState<HelpContact[] | null>(null);
  const [originals, setOriginals] = useState<Record<string, { open: boolean; loading: boolean; error?: string }>>({});
  /** Нерасшифрованные оригиналы — только на время экрана (в кеш сессии не попадают). */
  const [originalText, setOriginalText] = useState<Record<string, ExportText>>({});
  const alive = useRef(true);
  const partner = me.partner_name ?? "партнёр";

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const first = useCallback(() => {
    setLoad({ kind: "loading" });
    api.coupleMessages({ limit: FEED_PAGE }).then(
      (p) => {
        if (!alive.current) return;
        setItems(p.items);
        setCursor(p.next_cursor ?? null);
        setLoad({ kind: "ready" });
      },
      (e: unknown) => {
        if (alive.current) setLoad({ kind: "error", text: errText(e, "coupleMessages") });
      },
    );
  }, [api]);

  useEffect(() => {
    first();
  }, [first]);

  // Остановленное безопасностью — протокол помощи с контактами (`GET /help/contacts`; без них — 112 всегда).
  const needContacts = items.some((m) => m.status === "withheld" && m.withheld_reason === "safety");
  useEffect(() => {
    if (!needContacts || contacts !== null) return;
    let current = true;
    api.helpContacts().then(
      (c) => current && setContacts(c),
      () => current && setContacts([]),
    );
    return () => {
      current = false;
    };
  }, [needContacts, contacts, api]);

  const loadOlder = async () => {
    if (!cursor || older.loading) return;
    setOlder({ loading: true, error: null });
    try {
      const p = await api.coupleMessages({ cursor, limit: FEED_PAGE });
      if (!alive.current) return;
      setItems((prev) => {
        const seen = new Set(prev.map((m) => m.id));
        return [...prev, ...p.items.filter((m) => !seen.has(m.id))];
      });
      setCursor(p.next_cursor ?? null);
      setOlder({ loading: false, error: null });
    } catch (e) {
      if (!alive.current) return;
      if (e instanceof ApiError && e.status === 422) {
        // Курсор больше не принимается (смена ключа сервера): лента — с первой страницы, курсор не повторяется.
        setOlder({ loading: false, error: null });
        setRestarted(true);
        first();
        return;
      }
      setOlder({ loading: false, error: errText(e, "coupleMessages") });
    }
  };

  const toggleOriginal = async (m: PartnerMessage) => {
    tg.impact("light");
    const cur = originals[m.id];
    if (cur?.loading) return;
    if (cur?.open) {
      setOriginals((o) => ({ ...o, [m.id]: { open: false, loading: false } }));
      return;
    }
    // Уже открытый в этой сессии оригинал — без нового запроса.
    if (couple.originals.has(m.id)) {
      setOriginals((o) => ({ ...o, [m.id]: { open: true, loading: false } }));
      return;
    }
    setOriginals((o) => ({ ...o, [m.id]: { open: false, loading: true } }));
    try {
      const text = await api.messageOriginal(m.id);
      // Нерасшифрованный оригинал не кешируется: сервер не засчитал просмотр, следующий показ спросит снова.
      if (typeof text === "string") couple.originals.set(m.id, text);
      if (!alive.current) return;
      setOriginalText((t) => ({ ...t, [m.id]: text }));
      setOriginals((o) => ({ ...o, [m.id]: { open: true, loading: false } }));
    } catch (e) {
      if (!alive.current) return;
      tg.notify("error");
      setOriginals((o) => ({ ...o, [m.id]: { open: false, loading: false, error: errText(e, "messageOriginal") } }));
    }
  };

  const reply = (m: PartnerMessage) => {
    tg.impact("light");
    couple.setReply({ id: m.id, sentAt: m.sent_at });
    push({ name: "chat" });
  };

  const markShown = (m: PartnerMessage) =>
    markRead(couple, api, m.id, () => {
      if (alive.current) setItems((prev) => prev.map((x) => (x.id === m.id && x.status === "delivered" ? { ...x, status: "read" } : x)));
    });

  // На экране — от старых к новым (сервер отдаёт от новых к старым).
  const shown = [...items].reverse();

  return (
    <section data-screen="messages">
      <Heading>Сообщения пары</Heading>
      <p className="small muted">
        Здесь только то, что вы с партнёром отправили друг другу кнопкой «Отправить». Разговоры с агентами сюда не
        попадают.
      </p>
      {load.kind === "loading" && <Loading>Загружаем сообщения…</Loading>}
      {load.kind === "error" && (
        <>
          <Notice kind="error">{load.text}</Notice>
          <button type="button" className="btn btn--block btn--ghost" onClick={first}>
            Загрузить снова
          </button>
        </>
      )}
      {restarted && load.kind === "ready" && (
        <p className="small muted" role="status">
          Ленту пришлось загрузить заново — более ранние сообщения можно открыть снова.
        </p>
      )}
      {load.kind === "ready" && cursor && (
        <button
          type="button"
          className="btn btn--block btn--text"
          disabled={older.loading}
          aria-busy={older.loading || undefined}
          onClick={() => void loadOlder()}
        >
          {older.loading ? "Загружаем более ранние…" : "Показать более ранние"}
        </button>
      )}
      {older.error && <Notice kind="error">{older.error}</Notice>}
      {load.kind === "ready" && items.length === 0 && (
        <div className="empty" data-empty>
          {me.has_active_couple ? (
            <>
              <p>
                <b>Пока сообщений нет</b>
              </p>
              <p className="small">
                Когда кто-то из вас подготовит сообщение с агентом и нажмёт «Отправить», оно появится здесь.
              </p>
            </>
          ) : (
            <>
              <p>
                <b>Пары сейчас нет</b>
              </p>
              <p className="small">
                После выхода из пары обмен сообщениями прекращается и лента пуста. Сообщения прежней пары есть в
                выгрузке твоих данных.
              </p>
            </>
          )}
        </div>
      )}
      {load.kind === "ready" && items.length > 0 && (
        <ol className="feed" aria-label="Сообщения пары">
          {shown.map((m) =>
            m.direction === "outgoing" ? (
              <Outgoing
                key={m.id}
                m={m}
                contacts={contacts}
                onHelp={() => push({ name: "help" })}
                onChat={() => push({ name: "chat" })}
              />
            ) : (
              <Incoming
                key={m.id}
                m={m}
                partner={partner}
                original={{
                  open: originals[m.id]?.open ?? false,
                  loading: originals[m.id]?.loading ?? false,
                  error: originals[m.id]?.error,
                  text: originalText[m.id] ?? couple.originals.get(m.id),
                }}
                onToggle={() => void toggleOriginal(m)}
                onReply={() => reply(m)}
                onShown={() => markShown(m)}
              />
            ),
          )}
        </ol>
      )}
    </section>
  );
}
