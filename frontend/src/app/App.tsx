import { Component, Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Api, ApiError } from "../api/client";
import { errorText } from "../api/messages";
import type { InviteCreated, Me } from "../api/types";
import { ChatSession } from "../chat/session";
import { CoupleSession } from "../couple/session";
import { AuthError } from "../screens/AuthError";
import { Chat } from "../screens/Chat";
import { Draft } from "../screens/Draft";
import { Messages } from "../screens/Messages";
import { Help } from "../screens/Help";
import { Incoming } from "../screens/Incoming";
import { Memory } from "../screens/Memory";
import { Invite } from "../screens/Invite";
import { Onboarding } from "../screens/Onboarding";
import { OpenInTelegram, TelegramUnavailable, WaitingForTelegram } from "../screens/OpenInTelegram";
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

type Boot = { kind: "loading" } | { kind: "error"; error: ApiError } | { kind: "ready"; me: Me };

/** Первый экран после входа. */
export function initialRoute(me: Me, startParam: string | null): Route {
  if (startParam) return { name: "incoming", token: startParam };
  if (!me.onboarding_completed) return { name: "welcome", slide: 0 };
  return { name: me.has_active_couple ? "paired" : "invite" };
}

function routeKey(r: Route): string {
  if (r.name === "welcome") return `welcome-${r.slide}`;
  // Черновик для другого ответа — другой экран: состояние прежнего черновика не переносится (ревью 3.4 BLOCKER-1).
  if (r.name === "draft") return `draft-${r.replyTo ?? "new"}`;
  return r.name;
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
    case "chat":
      return <Chat />;
    case "memory":
      return <Memory />;
    case "draft":
      return <Draft replyTo={route.replyTo} />;
    case "messages":
      return <Messages />;
    case "underage":
      return <Underage />;
    case "help":
      return <Help api={api} />;
  }
}

/**
 * Вход и сессия вошедшего пользователя: стек экранов и состояние сессии (только в памяти).
 * До входа и после — один и тот же каркас (`Frame`) в одном месте дерева: при переходе «Входим…» → первый
 * экран ссылка «Помощь» и кнопка «Назад» не пересоздаются (фокус и нажатие не теряются).
 */
function Session({
  api,
  tg,
  boot,
  onRetry,
  help,
}: {
  api: Api;
  tg: TelegramApi;
  boot: Boot;
  onRetry: () => void;
  /** «Помощь» была открыта до запуска (ждали SDK): сессия начинается с неё. */
  help: boolean;
}) {
  const [helpBeforeLogin, setHelpBeforeLogin] = useState(help);
  const [me, setMe] = useState<Me | null>(null);
  const [stack, setStack] = useState<Route[]>([]);
  const [invite, setInvite] = useState<InviteCreated | null>(null);
  const [shared, setShared] = useState(false);
  const [pendingAccept, setPendingAccept] = useState<PendingAccept | null>(null);
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [onboardingDraft, setOnboardingDraft] = useState<OnboardingDraft | null>(null);
  const [chat] = useState(() => new ChatSession(api));
  const [couple] = useState(() => new CoupleSession());
  useEffect(() => {
    chat.open();
    return () => chat.close();
  }, [chat]);

  // Вход завершился: первый экран по онбордингу, паре и startapp. Вычисляется при отрисовке (состояние из
  // свойства), поэтому промежуточного кадра без экрана нет. Если человек открыл «Помощь» до окончания входа,
  // она остаётся на месте, а «Назад» ведёт на первый экран (ТЗ §9.3, n-6).
  if (boot.kind === "ready" && me === null) {
    const first = initialRoute(boot.me, tg.startParam);
    setMe(boot.me);
    setStack(helpBeforeLogin ? [first, { name: "help" }] : [first]);
    if (helpBeforeLogin) setHelpBeforeLogin(false);
  }

  const push = useCallback((r: Route) => setStack((s) => [...s, r]), []);
  const reset = useCallback((r: Route) => setStack([r]), []);
  const replace = useCallback((r: Route) => setStack((s) => [...s.slice(0, -1), r]), []);
  const back = useCallback(() => setStack((s) => (s.length > 1 ? s.slice(0, -1) : s)), []);
  const backTo = useCallback(
    (r: Route) =>
      setStack((s) => {
        for (let i = s.length - 2; i >= 0; i -= 1) if (s[i].name === r.name) return s.slice(0, i + 1);
        return [...s.slice(0, -1), r];
      }),
    [],
  );

  const route = stack.length > 0 ? stack[stack.length - 1] : null;
  const canGoBack = stack.length > 1;

  const state = useMemo<AppState | null>(
    () =>
      me && {
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
        chat,
        couple,
        push,
        reset,
        replace,
        back,
        backTo,
        canGoBack,
      },
    [api, me, invite, shared, pendingAccept, inviteError, onboardingDraft, chat, couple, push, reset, replace, back, backTo, canGoBack],
  );

  if (!state || !route) {
    return (
      <AppContext.Provider value={null}>
        <Frame
          onBack={helpBeforeLogin ? () => setHelpBeforeLogin(false) : null}
          onHelp={helpBeforeLogin ? null : () => setHelpBeforeLogin(true)}
        >
          {helpBeforeLogin ? (
            <Help api={api} />
          ) : boot.kind === "error" ? (
            <AuthError error={boot.error} onRetry={onRetry} />
          ) : (
            <section data-screen="loading">
              <Heading>Чувства AI</Heading>
              <Loading>Входим через Telegram…</Loading>
            </section>
          )}
        </Frame>
      </AppContext.Provider>
    );
  }

  return (
    <AppContext.Provider value={state}>
      <Frame
        onBack={canGoBack ? back : null}
        // Чат держит «Помощь» в закреплённом низу экрана, рядом с полем ввода: видна при любой длине ленты.
        onHelp={route.name === "help" || route.name === "chat" ? null : () => push({ name: "help" })}
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
 * Последний рубеж: упал сам запасной экран (или его «Помощь»). Статичная разметка без SDK, контекстов и
 * запросов — текст, номер 112 ссылкой и перезапуск; повторно упасть здесь нечему, цикла нет (A8-1.5-D6).
 */
class LastResort extends Component<{ onRestart: () => void; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="app">
        <main className="content">
          <section data-screen="fatal">
            <h1>Что-то пошло не так</h1>
            <p role="alert">
              Приложение не может показать экран. Введённое не отправлено. Закрой приложение и открой его снова.
            </p>
            <p>
              Если тебе или кому-то рядом опасно — звони{" "}
              <a className="contact__phone" href="tel:112">
                112
              </a>
              . Это бесплатно и круглосуточно.
            </p>
            <button type="button" className="btn btn--block btn--ghost" onClick={this.props.onRestart}>
              Перезапустить
            </button>
          </section>
        </main>
      </div>
    );
  }
}

/**
 * Граница ошибок: исключение при отрисовке экрана не оставляет пустую страницу без «Помощи».
 * Ошибка не журналируется (консоль не используется) и не показывается человеку как есть. Запасной экран
 * обёрнут в `LastResort`: если упадёт и он, граница не перерисовывает его снова, а показывает статичный минимум.
 */
class Boundary extends Component<{ api: Api; onRestart: () => void; children: ReactNode }, { crashed: boolean }> {
  state = { crashed: false };

  static getDerivedStateFromError(): { crashed: boolean } {
    return { crashed: true };
  }

  private restart = () => {
    this.setState({ crashed: false });
    this.props.onRestart();
  };

  render() {
    if (!this.state.crashed) return this.props.children;
    return (
      <LastResort onRestart={this.restart}>
        <Crashed api={this.props.api} onRestart={this.restart} />
      </LastResort>
    );
  }
}

/**
 * SDK Telegram не пришёл к сроку, хотя Mini App открыт из Telegram (в адресе есть параметры запуска):
 * `waiting` — скрипт ещё грузится, `failed` — не загрузился (см. boot.tsx, n-2).
 */
export type SdkWait = "waiting" | "failed";

/** Без SDK: «Открой в Telegram» (или ожидание SDK внутри Telegram) и «Помощь» (публичный путь, без входа). */
function Outside({
  api,
  sdk,
  initialHelp,
  onHelp,
}: {
  api: Api;
  sdk?: SdkWait;
  initialHelp: boolean;
  onHelp?: (open: boolean) => void;
}) {
  const [help, setHelpState] = useState(initialHelp);
  const setHelp = (open: boolean) => {
    setHelpState(open);
    onHelp?.(open);
  };
  const screen =
    sdk === "waiting" ? <WaitingForTelegram /> : sdk === "failed" ? <TelegramUnavailable /> : <OpenInTelegram />;
  return (
    <Frame onBack={help ? () => setHelp(false) : null} onHelp={help ? null : () => setHelp(true)}>
      {help ? <Help api={api} /> : screen}
    </Frame>
  );
}

/**
 * Корень Mini App. Вход по initData (ФТ-01) → экран по состоянию онбординга, пары и `startapp`.
 * Вне Telegram (нет initData) запросов входа нет — только «Открой в Telegram» и «Помощь».
 */
export function App({
  tg,
  api,
  sdk,
  help = false,
  onHelp,
}: {
  tg: TelegramApi;
  api: Api;
  sdk?: SdkWait;
  /** Приложение перезапущено (пришёл SDK), а «Помощь» была открыта: открыть сразу её (ТЗ §9.3). */
  help?: boolean;
  /** Сообщает запуску, открыта ли «Помощь» на экране без SDK, — чтобы перезапуск её сохранил. */
  onHelp?: (open: boolean) => void;
}) {
  const [boot, setBoot] = useState<Boot>({ kind: "loading" });
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

  // Новое поколение (перезапуск из запасного экрана) — новая сессия с нуля: стек, черновики, «Помощь» до входа.
  // «Помощь» из прошлого запуска открывается только в первом поколении, перезапуск с запасного экрана — с нуля.
  const startHelp = help && generation === 0;
  const body = tg.inside ? (
    <Session api={api} tg={tg} boot={boot} onRetry={login} help={startHelp} />
  ) : (
    <Outside api={api} sdk={sdk} initialHelp={startHelp} onHelp={onHelp} />
  );

  const restart = useCallback(() => {
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
