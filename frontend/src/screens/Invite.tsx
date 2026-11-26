import { useCallback, useEffect, useId, useState } from "react";
import { ApiError } from "../api/client";
import { errorText } from "../api/messages";
import { useApp } from "../app/store";
import { MainButton } from "../telegram/MainButton";
import { useTelegram } from "../telegram/context";
import { formatDay } from "../ui/format";
import { Heading } from "../ui/Heading";
import { Loading, Notice } from "../ui/Notice";

/** Как часто экран «ждём партнёра» спрашивает статус пары. */
export const POLL_MS = 10_000;

const SHARE_TEXT =
  "Приглашаю тебя в «Чувства AI». У каждого из нас будет свой агент, который поможет говорить о важном. Ссылка одноразовая.";

type State =
  | { kind: "loading" }
  | { kind: "none" }
  /** Прежняя пара распалась (контракт: `Couple.status = dissolved`) — можно пригласить заново. */
  | { kind: "dissolved" }
  | { kind: "pending" }
  | { kind: "error"; text: string };

function text(e: unknown): string {
  return e instanceof ApiError ? e.message : errorText("internal");
}

/**
 * Приглашение со стороны инициатора (ФТ-03, ТЗ §4.1 п.3, §8): ссылка, «Отправить в Telegram»,
 * статус ожидания. Пока партнёр не принял приглашение, экран раз в POLL_MS проверяет пару.
 */
export function Invite() {
  const app = useApp();
  const tg = useTelegram();
  const { api, invite, shared, reset, setInvite, setShared, setInviteError } = app;
  const [state, setState] = useState<State>({ kind: "loading" });
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(app.inviteError);
  const linkId = useId();

  const load = useCallback(
    async (quiet: boolean) => {
      try {
        const couple = await api.couple();
        if (!couple) {
          setInvite(null);
          setState({ kind: "none" });
        } else if (couple.status === "active") {
          tg.notify("success");
          reset({ name: "paired" });
        } else if (couple.status === "dissolved") {
          setInvite(null);
          setState({ kind: "dissolved" });
        } else if (couple.status === "pending") {
          setState({ kind: "pending" });
        } else {
          setState({ kind: "error", text: errorText("bad_response") });
        }
      } catch (e) {
        // Тихий опрос молчит о временных сбоях, но не о потерянной сессии: если повторный вход не удался
        // (401), опрос останавливается и причина видна — иначе «Ждём партнёра» висело бы бесконечно.
        if (!quiet || (e instanceof ApiError && e.status === 401)) setState({ kind: "error", text: text(e) });
      }
    },
    [api, reset, setInvite, tg],
  );

  useEffect(() => {
    setInviteError(null);
    void load(false);
  }, [load, setInviteError]);

  const pending = state.kind === "pending";
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => void load(true), POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") void load(true);
    };
    document.addEventListener("visibilitychange", onVisible);
    const offActivated = tg.onActivated(() => void load(true));
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      offActivated();
    };
  }, [pending, load, tg]);

  async function issue(kind: "create" | "reissue") {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const created = kind === "create" ? await api.createCouple() : await api.reissueInvite();
      setInvite(created);
      setShared(false);
      setState({ kind: "pending" });
    } catch (e) {
      tg.notify("error");
      setError(text(e));
      // Состояние пары могло измениться (уже есть пара, пара уже не ждёт) — перечитать.
      if (e instanceof ApiError && (e.status === 409 || e.status === 404)) void load(true);
    } finally {
      setBusy(false);
    }
  }

  function share() {
    if (!invite) return;
    if (tg.shareLink(invite.link, SHARE_TEXT)) setShared(true);
    // Telegram не открыл выбор чата (отказ SDK) — у человека остаётся ссылка на экране и «Скопировать».
    else setNote("Не получилось открыть выбор чата. Скопируй ссылку и отправь её партнёру.");
  }

  async function copy() {
    if (!invite) return;
    try {
      await navigator.clipboard.writeText(invite.link);
      setNote("Ссылка скопирована.");
    } catch {
      setNote("Не получилось скопировать. Выдели ссылку и скопируй её вручную.");
    }
  }

  let main: { text: string; onClick: () => void } | null = null;
  if (state.kind === "none" || state.kind === "dissolved") main = { text: "Создать ссылку-приглашение", onClick: () => void issue("create") };
  else if (state.kind === "pending" && invite)
    main = { text: shared ? "Отправить ещё раз в Telegram" : "Отправить в Telegram", onClick: share };
  else if (state.kind === "pending") main = { text: "Получить новую ссылку", onClick: () => void issue("reissue") };
  else if (state.kind === "error") main = { text: "Попробовать снова", onClick: () => void load(false) };

  return (
    <section data-screen="invite">
      <Heading>Пригласи партнёра</Heading>
      <p className="muted">
        У партнёра будет свой агент. Ваши личные разговоры с агентами друг другу не видны — между вами передаются
        только сообщения, которые каждый сам отправит.
      </p>

      {state.kind === "loading" && <Loading />}
      {state.kind === "error" && <Notice kind="error">{state.text}</Notice>}

      {state.kind === "none" && (
        <div className="card card--soft">
          <div className="status" data-status="none">
            <span className="status__mark" aria-hidden="true">
              ○
            </span>
            <span>
              <b>Ссылки пока нет.</b> Создай её и отправь партнёру в Telegram.
            </span>
          </div>
        </div>
      )}

      {state.kind === "dissolved" && (
        <div className="card card--soft">
          <div className="status" data-status="dissolved" role="status">
            <span className="status__mark" aria-hidden="true">
              ○
            </span>
            <span>
              <b>Прежней пары больше нет.</b> Создай новую ссылку и отправь её партнёру в Telegram.
            </span>
          </div>
        </div>
      )}

      {state.kind === "pending" && invite && (
        <>
          {/* Видимая подпись и группа с именем: у абзаца без роли aria-label не озвучивается (ARIA 1.2). */}
          <div className="field" role="group" aria-labelledby={linkId}>
            <div className="label" id={linkId}>
              Ссылка-приглашение
            </div>
            <p className="link-box">{invite.link}</p>
          </div>
          <p className="small muted">Ссылка одноразовая и действует 7 дней — до {formatDay(invite.expires_at)}.</p>
          <div className="card card--soft">
            {shared ? (
              <div className="status" data-status="waiting" role="status">
                <span className="status__mark" aria-hidden="true">
                  …
                </span>
                <span>
                  <b>Ждём партнёра.</b> Когда приглашение примут, здесь появится «Вы пара».
                </span>
              </div>
            ) : (
              <div className="status" data-status="ready" role="status">
                <span className="status__mark" aria-hidden="true">
                  ○
                </span>
                <span>
                  <b>Ссылка готова.</b> Ещё не отправлена.
                </span>
              </div>
            )}
          </div>
          <div className="btn-row">
            <button type="button" className="btn btn--ghost" onClick={() => void copy()}>
              Скопировать ссылку
            </button>
            <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => void issue("reissue")}>
              Новая ссылка
            </button>
          </div>
          <p className="small muted">Новая ссылка заменит прежнюю — старая перестанет работать.</p>
        </>
      )}

      {state.kind === "pending" && !invite && (
        <div className="card card--soft">
          <div className="status" data-status="waiting" role="status">
            <span className="status__mark" aria-hidden="true">
              …
            </span>
            <span>
              <b>Ждём партнёра.</b> Приглашение уже создано. Если ссылка потерялась, получи новую — прежняя перестанет
              работать.
            </span>
          </div>
        </div>
      )}

      {pending && (
        <>
          <button type="button" className="btn btn--block btn--text" onClick={() => void load(false)}>
            Проверить, принято ли приглашение
          </button>
          <p className="small">Отправлять сообщения партнёру можно будет, когда пара создана.</p>
        </>
      )}

      <button type="button" className="btn btn--block btn--ghost" onClick={() => app.push({ name: "chat" })}>
        Поговорить с агентом
      </button>
      <p className="small muted">Говорить с агентом можно уже сейчас — этот разговор видишь только ты.</p>
      <button type="button" className="btn btn--block btn--ghost" onClick={() => app.push({ name: "memory" })}>
        Что агент помнит
      </button>

      {note && (
        <p className="small" role="status">
          {note}
        </p>
      )}
      {error && <Notice kind="error">{error}</Notice>}
      {main && <MainButton text={main.text} onClick={main.onClick} progress={busy} />}
    </section>
  );
}
