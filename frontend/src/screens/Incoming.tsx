import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import { errorText } from "../api/messages";
import type { InvitePreview } from "../api/types";
import { INVITE_TOKEN } from "../app/routes";
import { useApp } from "../app/store";
import { MainButton } from "../telegram/MainButton";
import { useTelegram } from "../telegram/context";
import { formatDay } from "../ui/format";
import { Heading } from "../ui/Heading";
import { Loading, Notice } from "../ui/Notice";

type State =
  | { kind: "loading" }
  | { kind: "ok"; invite: InvitePreview }
  | { kind: "invalid"; text: string; retry: boolean };

/**
 * Входящее приглашение по ссылке `startapp` (ФТ-03, ТЗ §4.1 п.4). Просмотр доступен до онбординга
 * (АР-17); «Принять» до онбординга ведёт через приветствие и онбординг, после — сразу принимает.
 */
export function Incoming({ token }: { token: string }) {
  const app = useApp();
  const tg = useTelegram();
  const { api } = app;
  const wellFormed = INVITE_TOKEN.test(token);
  const [state, setState] = useState<State>(
    wellFormed ? { kind: "loading" } : { kind: "invalid", text: errorText("not_found"), retry: false },
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!wellFormed) return;
    let alive = true;
    api.invite(token).then(
      (invite) => alive && setState({ kind: "ok", invite }),
      (e: unknown) => {
        if (!alive) return;
        const retry = !(e instanceof ApiError) || e.status === 0 || e.status >= 500 || e.status === 429;
        setState({ kind: "invalid", text: e instanceof ApiError ? e.message : errorText("internal"), retry });
      },
    );
    return () => {
      alive = false;
    };
  }, [api, token, wellFormed, attempt]);

  function withoutInvite() {
    app.setPendingAccept(null);
    if (!app.me.onboarding_completed) app.reset({ name: "welcome", slide: 0 });
    else app.reset({ name: app.me.has_active_couple ? "paired" : "invite" });
  }

  async function accept(invite: InvitePreview) {
    if (!app.me.onboarding_completed) {
      app.setPendingAccept({ token, inviterName: invite.inviter_name });
      app.push({ name: "welcome", slide: 0 });
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.acceptInvite(token);
      app.setMe({ ...app.me, has_active_couple: true });
      tg.notify("success");
      app.reset({ name: "paired" });
    } catch (e) {
      setBusy(false);
      tg.notify("error");
      setError(e instanceof ApiError ? e.message : errorText("internal"));
    }
  }

  if (state.kind === "loading") {
    return (
      <section data-screen="incoming">
        <Heading>Приглашение</Heading>
        <Loading>Проверяем приглашение…</Loading>
      </section>
    );
  }

  if (state.kind === "invalid") {
    return (
      <section data-screen="incoming">
        <Heading>Приглашение не работает</Heading>
        <Notice kind="error">{state.text}</Notice>
        <button type="button" className="btn btn--block btn--ghost" onClick={withoutInvite}>
          Продолжить без приглашения
        </button>
        {state.retry && (
          <MainButton
            text="Попробовать снова"
            onClick={() => {
              setState({ kind: "loading" });
              setAttempt((n) => n + 1);
            }}
          />
        )}
      </section>
    );
  }

  const inviter = state.invite.inviter_name;
  return (
    <section data-screen="incoming">
      <Heading>{inviter ? `${inviter} приглашает тебя` : "Тебя приглашают в «Чувства AI»"}</Heading>
      <p className="lead">Вместе вам будет проще говорить о важном — спокойно и вовремя.</p>
      <div className="card card--soft">
        <p>
          <b>Как это устроено</b>
        </p>
        <p className="small">
          У тебя будет свой агент. Он не видит разговоров партнёра, а партнёр — твоих. Между вами передаются только
          сообщения, которые каждый сам отправит кнопкой «Отправить».
        </p>
      </div>
      <p className="small muted">Приглашение действует до {formatDay(state.invite.expires_at)}.</p>
      {error && (
        <>
          <Notice kind="error">{error}</Notice>
          <button type="button" className="btn btn--block btn--ghost" onClick={withoutInvite}>
            Продолжить без приглашения
          </button>
        </>
      )}
      <MainButton text="Принять приглашение" progress={busy} onClick={() => void accept(state.invite)} />
    </section>
  );
}
