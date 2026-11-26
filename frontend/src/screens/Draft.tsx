import { useEffect, useId, useRef, useState } from "react";
import { ApiError, isRetryable } from "../api/client";
import { DRAFT_MAX, WISH_MAX, charCount, clipChars, type CrisisView, type DraftResult } from "../api/couple";
import { errorText, type Op } from "../api/messages";
import type { Draft as DraftData } from "../api/types";
import { useApp } from "../app/store";
import { CRISIS_FALLBACK, isBlank } from "../chat/session";
import type { CoupleSession, DraftMemory } from "../couple/session";
import { MainButton } from "../telegram/MainButton";
import { useTelegram } from "../telegram/context";
import { formatMoment } from "../ui/format";
import { Heading } from "../ui/Heading";
import { Loading, Notice } from "../ui/Notice";
import { HelpContacts } from "./Help";

/** С какой длины показывать счётчик символов. */
const COUNTER_FROM = 3500;

/** Подпись главной кнопки. Имя партнёра в дательном не подставляется: склонения нет (вопрос 5 карточки 3.4). */
export const SEND_TEXT = "Отправить";

/** Пометка о приватности черновика (ТЗ §2 п.2). */
export function privacyNote(partner: string | null): string {
  return `Черновик видишь только ты. Пока ты не нажмёшь «Отправить», ${partner ?? "партнёр"} ничего не получит.`;
}

type Phase =
  | { kind: "creating" }
  | { kind: "ready" }
  /** Кризисный протокол вместо черновика (ТЗ §9.3): черновика на экране нет, отправлять нечего. */
  | { kind: "crisis"; view: CrisisView }
  /** Черновик не получился (ошибка `POST /drafts`): разговор цел. */
  | { kind: "failed"; text: string; retry: boolean; help: boolean }
  /** Черновик уже отправлен или отклонён (409 `not_editable`), или исчез (404): отправлять нечего. */
  | { kind: "closed"; text: string };

type Busy = "regenerate" | "send" | "reject" | null;

const codeOf = (e: unknown) => (e instanceof ApiError ? e.code : "internal");
const textOf = (e: unknown, op: Op) => (e instanceof ApiError ? e.message : errorText("internal", undefined, undefined, op));
/** 409 `not_editable` на «Другом варианте», когда черновик ещё `draft`: его изменили, пока агент писал вариант. */
const REGEN_CHANGED =
  "Черновик изменился, пока агент писал другой вариант: в поле — его актуальный текст. Партнёру ничего не отправлено.";
/** Показанный черновик расходится с памятью сессии — отправка не выполняется. */
const STALE = "Этот черновик уже не текущий — партнёру ничего не отправлено. Вернись в чат и открой черновик заново.";
/** Ошибки, после которых стоит показать строку о помощи со 112: отказ модели мог быть кризисом (АР-34 п.2). */
const HELP_CODES = new Set(["llm_refusal", "blocked_by_safety"]);

/** Черновик из памяти сессии, если он подходит к этому экрану (тот же ответ и ещё не отправлен). */
function reusable(couple: CoupleSession, replyTo: string | null): DraftMemory | null {
  const d = couple.draft;
  if (!d || d.draft.status !== "draft") return null;
  return (d.draft.reply_to_id ?? null) === replyTo ? d : null;
}

/**
 * `POST /drafts` — один на открытие экрана (повторный запуск эффекта ждёт тот же запрос).
 *
 * Ответ запоминается в сессии, только если запрос ещё текущий: человек мог уйти и открыть черновик для другого
 * ответа (`replyTo`), и тогда `couple.creating` уже указывает на новый запрос. Опоздавший ответ прежнего запроса
 * память сессии не трогает — иначе он подменил бы показанный черновик (ревью 3.4 BLOCKER-1, A8-3.4-2). Такой
 * черновик остаётся на сервере в статусе `draft` без доступа с экрана (до `GET /drafts`, предложение A1 № 3).
 */
function create(couple: CoupleSession, api: ReturnType<typeof useApp>["api"], replyTo: string | null): Promise<DraftResult> {
  if (couple.creating && couple.creating.replyTo === replyTo) return couple.creating.promise;
  const promise = api.draftCreate(replyTo).then(
    (r) => {
      const current = couple.creating?.promise === promise;
      if (current) couple.creating = null;
      // Текущий запрос запоминается, даже если человек ушёл с экрана, — вернувшись, он увидит черновик, а не новый запрос.
      if (current && r.kind === "draft") couple.setDraft({ draft: r.draft, text: r.draft.text, lostEdit: null });
      return r;
    },
    (e: unknown) => {
      if (couple.creating?.promise === promise) couple.creating = null;
      throw e;
    },
  );
  couple.creating = { replyTo, promise };
  return promise;
}

function Crisis({ view, onHelp, onChat }: { view: CrisisView; onHelp: () => void; onChat: () => void }) {
  return (
    <>
      <section className="crisis crisis--page" aria-label="Контакты помощи" data-crisis>
        {/* Ответ протокола целиком, как пришёл: ничего не сокращается и не смягчается (ТЗ §9.3). */}
        <p className="crisis__text">{view.text ?? CRISIS_FALLBACK}</p>
        <h2 className="crisis__title">Помощь рядом</h2>
        <HelpContacts contacts={view.contacts} />
        <button type="button" className="btn btn--block btn--ghost" onClick={onHelp}>
          Все контакты помощи
        </button>
      </section>
      <p className="small muted">Черновика нет — партнёру ничего не отправлено. Партнёр об этом не узнает.</p>
      <button type="button" className="btn btn--block" onClick={onChat}>
        Вернуться в чат
      </button>
    </>
  );
}

/**
 * Черновик сообщения партнёру (ФТ-07, ФТ-08, ТЗ §4.3, §8): текст агента в поле правки, «Другой вариант», «Не
 * отправлять» и главная кнопка «Отправить». Партнёру сообщение уходит **только** по нажатию «Отправить»: ни открытие,
 * ни правка, ни «Другой вариант», ни «Не отправлять», ни уход с экрана `send` не вызывают (ТЗ §2 п.2).
 *
 * Правка сохраняется на сервере одним `PATCH` прямо перед отправкой (вопрос 3 карточки 3.4): при уходе с экрана
 * она остаётся в памяти сессии и снова видна при возврате. Отправка подтверждает ровно показанную версию.
 */
export function Draft({ replyTo }: { replyTo: string | null }) {
  const { api, me, couple, chat, back, backTo, push } = useApp();
  const tg = useTelegram();
  const textId = useId();
  const counterId = useId();
  const wishId = useId();
  const [memory, setMemory] = useState<DraftMemory | null>(() => reusable(couple, replyTo));
  const [phase, setPhase] = useState<Phase>(() => (memory ? { kind: "ready" } : { kind: "creating" }));
  const [busy, setBusy] = useState<Busy>(null);
  /** Пояснение или ошибка последнего действия; `error` — с ролью alert. */
  const [notice, setNotice] = useState<{ text: string; error: boolean; help?: boolean; checkFeed?: boolean } | null>(null);
  const [wish, setWish] = useState("");
  const [confirmVariant, setConfirmVariant] = useState(false);
  const [live, setLive] = useState(memory ? "Это черновик, который ты начал(а) раньше." : "Агент готовит черновик…");
  /** Идёт действие: второе нажатие (в том числе двойное касание MainButton) не начинает второй запрос. */
  const inFlight = useRef(false);
  const alive = useRef(true);
  const [attempt, setAttempt] = useState(0);
  const resumed = useRef(memory !== null);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Открытие экрана — `POST /drafts` (если в памяти сессии нет подходящего черновика). Ничего не отправляет.
  useEffect(() => {
    if (phase.kind !== "creating") return;
    let current = true;
    create(couple, api, replyTo).then(
      (r) => {
        if (!current) return;
        if (r.kind === "crisis") {
          setPhase({ kind: "crisis", view: r });
          setLive("Черновика нет. Ниже — ответ и контакты помощи.");
          return;
        }
        // На экране — черновик **этого** запроса (а не то, что сейчас лежит в памяти сессии).
        const m = couple.draft?.draft.id === r.draft.id ? couple.draft : { draft: r.draft, text: r.draft.text, lostEdit: null };
        if (couple.draft === null) couple.setDraft(m);
        setMemory(m);
        setPhase({ kind: "ready" });
        setLive(`Черновик готов: ${r.draft.text}`);
      },
      (e: unknown) => {
        if (!current) return;
        const code = codeOf(e);
        setPhase({
          kind: "failed",
          text: textOf(e, "draftCreate"),
          retry: isRetryable(e) || code === "llm_unavailable" || code === "llm_refusal" || code === "draft_quote",
          help: HELP_CODES.has(code),
        });
        setLive("");
        tg.notify("error");
      },
    );
    return () => {
      current = false;
    };
    // `attempt` — «Повторить»: новый запрос.
  }, [phase.kind, attempt, api, couple, replyTo, tg]);

  const partner = me.partner_name ?? null;
  const text = memory?.text ?? "";
  const edited = memory !== null && memory.text !== memory.draft.text;
  /** Длина в символах Unicode, как у сервера (ревью 3.4 MINOR-2). */
  const length = charCount(text);
  const tooLong = length > DRAFT_MAX;
  const canSend = phase.kind === "ready" && memory !== null && busy === null && !isBlank(text) && !tooLong;

  /**
   * Изменить черновик `id` на экране и в памяти сессии. Память сессии меняется, только если в ней этот же черновик
   * (или пусто): действие над показанным черновиком никогда не трогает другой (ревью 3.4 BLOCKER-1).
   */
  const remember = (id: string, m: DraftMemory | null) => {
    if (couple.draft === null || couple.draft.draft.id === id) couple.setDraft(m);
    if (alive.current) setMemory(m);
  };

  const setText = (value: string) => {
    if (!memory) return;
    remember(memory.draft.id, { ...memory, text: clipChars(value, DRAFT_MAX) });
  };

  /** Черновик закрыт (уже отправлен/отклонён или не найден): из памяти убирается, отправлять нечего. */
  const close = (mine: DraftMemory, e: unknown, op: Op) => {
    remember(mine.draft.id, null);
    if (alive.current) setPhase({ kind: "closed", text: textOf(e, op) });
  };

  /**
   * Перечитать черновик (`GET /drafts/{id}`), ничего не отправляя: после 409 `version_conflict` и после 409
   * `not_editable` на «Другом варианте» (его правили, пока шла модель, — черновик ещё `draft`, ревью 3.4 MINOR-1).
   * Закрывается он, только если сервер подтвердил, что статус уже не `draft`. Актуальный текст сервера — в поле;
   * правка автора, если он правил и она отличается, сохраняется рядом («Вернуть мою правку») — не теряется.
   */
  const reload = async (mine: DraftMemory, op: Op, err: unknown, message?: string) => {
    try {
      const fresh = await api.draftGet(mine.draft.id);
      const editedByMe = op !== "draftRegenerate" || mine.text !== mine.draft.text;
      const lost = editedByMe && mine.text !== fresh.text ? mine.text : null;
      if (fresh.status !== "draft") {
        close(mine, new ApiError(409, "not_editable", errorText("not_editable", undefined, undefined, op)), op);
        return;
      }
      remember(mine.draft.id, { draft: fresh, text: fresh.text, lostEdit: lost });
      if (alive.current) setNotice({ text: message ?? textOf(err, op), error: true });
    } catch (e) {
      if (e instanceof ApiError && e.code === "not_found") {
        close(mine, e, "draftGet");
        return;
      }
      if (alive.current) setNotice({ text: `${message ?? textOf(err, op)} ${textOf(e, "draftGet")}`, error: true });
    }
  };

  /** Общая обработка ошибки действия над черновиком `mine` (показанным на экране). */
  const failed = async (e: unknown, op: Op, mine: DraftMemory) => {
    tg.notify("error");
    const code = codeOf(e);
    if (code === "version_conflict") return reload(mine, op, e);
    // Контракт `regenerate`: not_editable и тогда, когда автор правил текст, пока шла модель, — черновик жив.
    if (code === "not_editable" && op === "draftRegenerate") return reload(mine, op, e, REGEN_CHANGED);
    if (code === "not_editable" || code === "not_found") return close(mine, e, op);
    if (alive.current)
      setNotice({
        text: textOf(e, op),
        error: true,
        help: HELP_CODES.has(code),
        // Неизвестно, дошёл ли «Отправить»: человек проверяет ленту, прежде чем нажать снова.
        checkFeed: op === "draftSend" && (isRetryable(e) || code === "bad_response") && code !== "conflict_retry",
      });
  };

  /**
   * «Отправить» — единственный путь сообщения к партнёру (ФТ-08). Только по нажатию человека и только черновик,
   * показанный на экране (`memory`: его id и версия), — не то, что лежит в памяти сессии (ревью 3.4 BLOCKER-1).
   */
  const send = async () => {
    const mine = memory;
    if (inFlight.current || !mine || !canSend) return;
    if (couple.draft !== null && couple.draft.draft.id !== mine.draft.id) {
      // Страж в глубину: в памяти сессии другой черновик — что-то разошлось; ничего не отправляем.
      setNotice({ text: STALE, error: true });
      return;
    }
    inFlight.current = true;
    setBusy("send");
    setNotice(null);
    setLive("Отправляем…");
    let shown: DraftData = mine.draft;
    let latest: DraftMemory = mine;
    let op: Op = "draftPatch";
    try {
      // Правка автора — на сервер одним `PATCH` с версией, которую он правил; затем отправка этой новой версии.
      if (mine.text !== shown.text) {
        shown = await api.draftPatch(shown.id, mine.text, shown.version);
        latest = { draft: shown, text: shown.text, lostEdit: null };
        remember(shown.id, latest);
      }
      op = "draftSend";
      await api.draftSend(shown.id, shown.version);
      tg.notify("success");
      remember(shown.id, null);
      if (replyTo && couple.reply?.id === replyTo) couple.setReply(null);
      chat.dismissSuggest();
      // К уже открытой ленте, если черновик открыт из неё (лента → чат → черновик), иначе лента вместо черновика.
      if (alive.current) backTo({ name: "messages" });
    } catch (e) {
      setLive("");
      await failed(e, op, latest);
    } finally {
      inFlight.current = false;
      if (alive.current) setBusy(null);
    }
  };

  /** «Другой вариант»: если автор правил текст — сначала подтверждение «Твоя правка пропадёт». */
  const askVariant = () => {
    if (busy !== null) return;
    tg.impact("light");
    if (edited) {
      setConfirmVariant(true);
      return;
    }
    void regenerate();
  };

  const regenerate = async () => {
    const mine = memory;
    if (inFlight.current || !mine) return;
    inFlight.current = true;
    setConfirmVariant(false);
    setBusy("regenerate");
    setNotice(null);
    setLive("Агент пишет другой вариант…");
    try {
      const r = await api.draftRegenerate(mine.draft.id, wish.trim() ? wish.trim() : undefined);
      if (r.kind === "crisis") {
        // Прежний черновик сервер не изменил, но на экране его больше нет: при кризисе отправлять нечего (§9.3).
        remember(mine.draft.id, null);
        if (alive.current) {
          setPhase({ kind: "crisis", view: r });
          setLive("Черновика нет. Ниже — ответ и контакты помощи.");
        }
        return;
      }
      remember(mine.draft.id, { draft: r.draft, text: r.draft.text, lostEdit: null });
      if (alive.current) {
        setWish("");
        setLive(`Новый вариант: ${r.draft.text}`);
      }
    } catch (e) {
      setLive("");
      // Прежний текст (и правка автора) остаются на месте (§10).
      await failed(e, "draftRegenerate", mine);
    } finally {
      inFlight.current = false;
      if (alive.current) setBusy(null);
    }
  };

  /** «Не отправлять»: черновик отклоняется, партнёру ничего не уходит; возврат в чат. */
  const reject = async () => {
    const mine = memory;
    if (inFlight.current || !mine) return;
    inFlight.current = true;
    tg.impact("light");
    setBusy("reject");
    setNotice(null);
    try {
      await api.draftReject(mine.draft.id);
      remember(mine.draft.id, null);
      if (alive.current) back();
    } catch (e) {
      await failed(e, "draftReject", mine);
    } finally {
      inFlight.current = false;
      if (alive.current) setBusy(null);
    }
  };

  const toChat = () => {
    tg.impact("light");
    back();
  };
  const toHelp = () => push({ name: "help" });
  const reply = couple.reply && couple.reply.id === replyTo ? couple.reply : null;

  return (
    <section data-screen="draft">
      <Heading>{replyTo ? "Ответ партнёру" : "Черновик"}</Heading>
      {phase.kind !== "crisis" && phase.kind !== "closed" && (
        <Notice>{privacyNote(partner)}</Notice>
      )}
      {replyTo && phase.kind !== "crisis" && (
        <p className="small muted" data-reply-to={replyTo}>
          {reply ? `Ответ на сообщение от ${formatMoment(reply.sentAt)}.` : "Ответ на сообщение партнёра."}
        </p>
      )}

      {phase.kind === "creating" && <Loading>Агент готовит черновик…</Loading>}

      {phase.kind === "failed" && (
        <>
          <Notice kind="error">{phase.text}</Notice>
          {phase.help && <HelpLine onHelp={toHelp} />}
          {phase.retry && (
            <button
              type="button"
              className="btn btn--block"
              onClick={() => {
                tg.impact("light");
                setPhase({ kind: "creating" });
                setLive("Агент готовит черновик…");
                setAttempt((n) => n + 1);
              }}
            >
              Повторить
            </button>
          )}
          <button type="button" className="btn btn--block btn--ghost" onClick={toChat}>
            Вернуться в чат
          </button>
        </>
      )}

      {phase.kind === "crisis" && <Crisis view={phase.view} onHelp={toHelp} onChat={toChat} />}

      {phase.kind === "closed" && (
        <>
          <Notice kind="error">{phase.text}</Notice>
          <button type="button" className="btn btn--block" onClick={() => backTo({ name: "messages" })}>
            Открыть сообщения пары
          </button>
          <button type="button" className="btn btn--block btn--ghost" onClick={toChat}>
            Вернуться в чат
          </button>
        </>
      )}

      {phase.kind === "ready" && memory && (
        <>
          {resumed.current && <p className="small muted">Это черновик, который ты начал(а) раньше.</p>}
          <div className="field">
            <label htmlFor={textId}>Текст сообщения партнёру</label>
            <textarea
              id={textId}
              className="input draft-text"
              value={text}
              readOnly={busy !== null}
              aria-describedby={length >= COUNTER_FROM ? counterId : undefined}
              onChange={(e) => setText(e.target.value)}
            />
          </div>
          {length >= COUNTER_FROM && (
            <p className="small muted" id={counterId}>
              {length} из {DRAFT_MAX} символов
            </p>
          )}
          <p className="small muted" data-draft-state>
            {edited
              ? "Изменено тобой. Правка сохранится, когда нажмёшь «Отправить»."
              : "Текст от агента — его можно править."}
          </p>
          {isBlank(text) && <p className="small">Текст пустой — отправлять нечего.</p>}
          {memory.lostEdit !== null && (
            <div className="card card--soft" data-lost-edit>
              <p className="small">
                <b>Твоя правка не потерялась.</b> Сейчас в поле — актуальный текст черновика. Можно вернуть свою правку и
                проверить её ещё раз.
              </p>
              <button
                type="button"
                className="btn btn--block btn--ghost"
                onClick={() => remember(memory.draft.id, { ...memory, text: memory.lostEdit ?? memory.text, lostEdit: null })}
              >
                Вернуть мою правку
              </button>
            </div>
          )}

          {notice && (notice.error ? <Notice kind="error">{notice.text}</Notice> : <Notice>{notice.text}</Notice>)}
          {notice?.help && <HelpLine onHelp={toHelp} />}
          {notice?.checkFeed && (
            <button type="button" className="btn btn--block btn--ghost" onClick={() => push({ name: "messages" })}>
              Проверить ленту сообщений пары
            </button>
          )}

          <div className="field">
            <label htmlFor={wishId}>Пожелание к другому варианту (необязательно)</label>
            <input
              id={wishId}
              className="input"
              value={wish}
              placeholder="Например: мягче, короче"
              onChange={(e) => setWish(clipChars(e.target.value, WISH_MAX))}
            />
          </div>
          {confirmVariant ? (
            <div className="confirm" role="group" aria-label="Заменить твой текст?" data-confirm-variant>
              <p className="confirm__q small">Твоя правка пропадёт, а агент подставит новый вариант. Заменить?</p>
              <button type="button" className="btn btn--fill" onClick={() => void regenerate()}>
                Заменить
              </button>
              <button type="button" className="btn btn--ghost" onClick={() => setConfirmVariant(false)}>
                Оставить мой текст
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="btn btn--block"
              disabled={busy !== null}
              aria-busy={busy === "regenerate" || undefined}
              onClick={askVariant}
            >
              {busy === "regenerate" ? "Агент пишет вариант…" : "Другой вариант"}
            </button>
          )}
          <button
            type="button"
            className="btn btn--block btn--text"
            disabled={busy !== null}
            aria-busy={busy === "reject" || undefined}
            onClick={() => void reject()}
          >
            Не отправлять
          </button>
          <MainButton text={SEND_TEXT} onClick={() => void send()} disabled={!canSend} progress={busy === "send"} />
        </>
      )}

      <div className="visually-hidden" role="status" aria-live="polite" data-live>
        {live}
      </div>
    </section>
  );
}

/** Строка о помощи под ошибкой (отказ модели, остановка безопасностью): 112 и переход в «Помощь». */
export function HelpLine({ onHelp }: { onHelp: () => void }) {
  return (
    <p className="small help-line">
      Если тебе сейчас опасно или очень тяжело — звони <a href="tel:112">112</a>.{" "}
      <button type="button" className="help-line__more" onClick={onHelp}>
        Все контакты помощи
      </button>
    </p>
  );
}
