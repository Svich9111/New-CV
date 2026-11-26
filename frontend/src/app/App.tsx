import { Component, Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Api, ApiError } from "../api/client";
import { errorText } from "../api/messages";
import type { InviteCreated, Me } from "../api/types";
import { AuthError } from "../screens/AuthError";
import { Help } from "../screens/Help";
import { Incoming } from "../screens/Incoming";
import { Invite } from "../screens/Invite";
import { Onboarding } from "../screens/Onboarding";
import { OpenInTelegram } from "../screens/OpenInTelegram";
import { Paired } from "../screens/Paired";
import { Underage } from "../screens/Underage";
import { Welcome } from "../screens/Welcome";
import { BackButton } from "../telegram/BackButton";
import { TelegramContext } from "../telegram/context";
import type { TelegramApi } from "../telegram/webapp";
import { Heading } from "../ui/Heading";
import { Loading } from "../ui/Notice";
import type { Route } from "./routes";
import { MainButton } from "../telegram/MainButton";
import { Notice } from "../ui/Notice";
import { AppContext, type AppState, type OnboardingDraft, type PendingAccept } from "./store";

/** Первый экран после входа. */
export function initialRoute(me: Me, startParam: string | null): Route {
  if (startParam) return { name: "incoming", token: startParam };
  if (!me.onboarding_completed) return { name: "welcome", slide: 0 };
  return { name: me.has_active_couple ? "paired" : "invite" };
}

function routeKey(r: Route): string {
  return r.name === "welcome" ? `welcome-${r.slide}` : r.name;
}

/** Каркас экрана: «Назад», содержимое, ссылка на «Помощь» — на каждом экране (ТЗ §9.3). */
function Frame({
  onBack,
  onHelp,
  children,
}: {
  onBack: (() => void) | null;
  onHelp: (() => void) | null;
  children: ReactNode;
}) {
  return (
    <div className="app">
      <BackButton onBack={onBack} />
      <main className="content">{children}</main>
      {onHelp && (
        <footer className="app-footer">
          <button type="button" className="help-link" onClick={onHelp}>
            Помощь и контакты служб поддержки
          </button>
        </footer>
      )}
    </div>
  );
}

function Screen({ route, api }: { route: Route; api: Api }) {
  switch (route.name) {
    case "welcome":
      return <Welcome slide={route.slide} />;
    case "onboarding":
      return <Onboarding />;
    case "invite":
      return <Invite />;
    case "incoming":
      return <Incoming token={route.token} />;
    case "paired":
      return <Paired />;
    case "underage":
      return <Underage />;
    case "help":
      return <Help api={api} />;
  }
}

/** Вошедший пользователь: стек экранов и состояние сессии (только в памяти). */
function Session({ api, tg, initialMe }: { api: Api; tg: TelegramApi; initialMe: Me }) {
  const [me, setMe] = useState(initialMe);
  const [stack, setStack] = useState<Route[]>(() => [initialRoute(initialMe, tg.startParam)]);
  const [invite, setInvite] = useState<InviteCreated | null>(null);
  const [shared, setShared] = useState(false);
  const [pendingAccept, setPendingAccept] = useState<PendingAccept | null>(null);
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [onboardingDraft, setOnboardingDraft] = useState<OnboardingDraft | null>(null);

  const push = useCallback((r: Route) => setStack((s) => [...s, r]), []);
  const reset = useCallback((r: Route) => setStack([r]), []);
  const back = useCallback(() => setStack((s) => (s.length > 1 ? s.slice(0, -1) : s)), []);

  const route = stack[stack.length - 1];
  const canGoBack = stack.length > 1;

  const state = useMemo<AppState>(
    () => ({
      api,
      me,
      setMe,
      invite,
      setInvite,
      shared,
      setShared,
      pendingAccept,
      setPendingAccept,
      inviteError,
      setInviteError,
      onboardingDraft,
      setOnboardingDraft,
      push,
      reset,
      back,
      canGoBack,
    }),
    [api, me, invite, shared, pendingAccept, inviteError, onboardingDraft, push, reset, back, canGoBack],
  );

  return (
    <AppContext.Provider value={state}>
      <Frame
        onBack={canGoBack ? back : null}
        onHelp={route.name === "help" ? null : () => push({ name: "help" })}
      >
        <Screen key={`${stack.length}-${routeKey(route)}`} route={route} api={api} />
      </Frame>
    </AppContext.Provider>
  );
}

/** Запасной экран, если экран упал при отрисовке: причина подписью, «Помощь» и перезапуск (ТЗ §9.3). */
function Crashed({ api, onRestart }: { api: Api; onRestart: () => void }) {
  const [help, setHelp] = useState(false);
  return (
    <Frame onBack={help ? () => setHelp(false) : null} onHelp={help ? null : () => setHelp(true)}>
      {help ? (
        <Help api={api} />
      ) : (
        <section data-screen="crashed">
          <Heading>Что-то пошло не так</Heading>
          <Notice kind="error">
            Экран не открылся из-за ошибки в приложении. Введённое не отправлено. Перезапусти приложение — если
            ошибка повторится, закрой его и открой снова.
          </Notice>
          <p className="small muted">Если тебе или кому-то рядом опасно — звони 112, это бесплатно и круглосуточно.</p>
          <MainButton text="Перезапустить" onClick={onRestart} />
        </section>
      )}
    </Frame>
  );
}

/**
 * Граница ошибок: исключение при отрисовке экрана не оставляет пустую страницу без «Помощи».
 * Ошибка не журналируется (консоль не используется) и не показывается человеку как есть.
 */
class Boundary extends Component<{ api: Api; onRestart: () => void; children: ReactNode }, { crashed: boolean }> {
  state = { crashed: false };

  static getDerivedStateFromError(): { crashed: boolean } {
    return { crashed: true };
  }

  render() {
    if (!this.state.crashed) return this.props.children;
    return (
      <Crashed
        api={this.props.api}
        onRestart={() => {
          this.setState({ crashed: false });
          this.props.onRestart();
        }}
      />
    );
  }
}

type Boot = { kind: "loading" } | { kind: "error"; error: ApiError } | { kind: "ready"; me: Me };

/** Вне Telegram: экран «Открой в Telegram» и «Помощь» (публичный путь, без входа). */
function Outside({ api }: { api: Api }) {
  const [help, setHelp] = useState(false);
  return (
    <Frame onBack={help ? () => setHelp(false) : null} onHelp={help ? null : () => setHelp(true)}>
      {help ? <Help api={api} /> : <OpenInTelegram />}
    </Frame>
  );
}

/**
 * Корень Mini App. Вход по initData (ФТ-01) → экран по состоянию онбординга, пары и `startapp`.
 * Вне Telegram (нет initData) запросов входа нет — только «Открой в Telegram» и «Помощь».
 */
export function App({ tg, api }: { tg: TelegramApi; api: Api }) {
  const [boot, setBoot] = useState<Boot>({ kind: "loading" });
  const [helpBeforeLogin, setHelpBeforeLogin] = useState(false);
  const started = useRef(false);
  const [generation, setGeneration] = useState(0);

  const login = useCallback(() => {
    setBoot({ kind: "loading" });
    api.login().then(
      (me) => setBoot({ kind: "ready", me }),
      (e: unknown) =>
        setBoot({ kind: "error", error: e instanceof ApiError ? e : new ApiError(0, "internal", errorText("internal")) }),
    );
  }, [api]);

  useEffect(() => {
    if (!tg.inside || started.current) return;
    started.current = true;
    login();
  }, [tg, login]);

  let body: ReactNode;
  if (!tg.inside) {
    body = <Outside api={api} />;
  } else if (boot.kind === "ready") {
    body = <Session api={api} tg={tg} initialMe={boot.me} />;
  } else {
    body = (
      <Frame
        onBack={helpBeforeLogin ? () => setHelpBeforeLogin(false) : null}
        onHelp={helpBeforeLogin ? null : () => setHelpBeforeLogin(true)}
      >
        {helpBeforeLogin ? (
          <Help api={api} />
        ) : boot.kind === "error" ? (
          <AuthError error={boot.error} onRetry={login} />
        ) : (
          <section data-screen="loading">
            <Heading>Чувства AI</Heading>
            <Loading>Входим через Telegram…</Loading>
          </section>
        )}
      </Frame>
    );
  }

  const restart = useCallback(() => {
    setHelpBeforeLogin(false);
    setGeneration((g) => g + 1);
    if (tg.inside) login();
  }, [tg, login]);

  return (
    <TelegramContext.Provider value={tg}>
      <Boundary api={api} onRestart={restart}>
        <Fragment key={generation}>{body}</Fragment>
      </Boundary>
    </TelegramContext.Provider>
  );
}
