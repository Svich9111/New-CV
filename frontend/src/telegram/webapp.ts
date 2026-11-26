/**
 * Тонкая типизированная обёртка над официальным SDK Telegram Mini Apps (`telegram-web-app.js`,
 * подключается в index.html). Описаны только те части `Telegram.WebApp`, которые нужны клиенту.
 *
 * Правила (CLAUDE.md, ТЗ §9.1 п.5): initData и токены не пишутся ни в журнал, ни в хранилище
 * браузера — строка initData уходит только в `POST /auth/telegram`.
 */

export type ColorScheme = "light" | "dark";
export type HapticImpact = "light" | "medium" | "heavy" | "rigid" | "soft";
export type HapticNotification = "error" | "success" | "warning";

export interface TgMainButton {
  setText(text: string): void;
  show(): void;
  hide(): void;
  enable(): void;
  disable(): void;
  showProgress(leaveActive?: boolean): void;
  hideProgress(): void;
  setParams?(params: { color?: string; text_color?: string; is_active?: boolean }): void;
  onClick(cb: () => void): void;
  offClick(cb: () => void): void;
}

export interface TgBackButton {
  show(): void;
  hide(): void;
  onClick(cb: () => void): void;
  offClick(cb: () => void): void;
}

export interface TgHapticFeedback {
  impactOccurred(style: HapticImpact): void;
  notificationOccurred(type: HapticNotification): void;
  selectionChanged(): void;
}

export interface TelegramWebApp {
  initData: string;
  initDataUnsafe: { start_param?: string; user?: { first_name?: string } };
  version: string;
  colorScheme: ColorScheme;
  isVersionAtLeast(version: string): boolean;
  ready(): void;
  expand(): void;
  setHeaderColor?(color: string): void;
  setBackgroundColor?(color: string): void;
  onEvent(event: "themeChanged" | "activated", cb: () => void): void;
  offEvent(event: "themeChanged" | "activated", cb: () => void): void;
  themeParams?: { bg_color?: string; secondary_bg_color?: string };
  openTelegramLink(url: string): void;
  MainButton: TgMainButton;
  BackButton: TgBackButton;
  HapticFeedback?: TgHapticFeedback;
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}

/** Mini App открыт внутри Telegram: SDK загружен и Telegram передал подписанный initData. */
export function findWebApp(win: Window = window): TelegramWebApp | null {
  const wa = win.Telegram?.WebApp;
  if (!wa || typeof wa.initData !== "string" || wa.initData.length === 0) return null;
  return wa;
}

/** Цвет токена темы из theme.css (единственный источник цветов); пусто, если стили не загружены. */
export function cssToken(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const HEX = /^#[0-9a-f]{6}$/i;

/** Вызов SDK, который не бросает: `true` — прошёл, `false` — SDK отказал (отказ не журналируется). */
export function safely(fn: () => void): boolean {
  try {
    fn();
    return true;
  } catch {
    // Отказ SDK не журналируется (консоль не используется) и не прерывает запуск или отрисовку.
    return false;
  }
}

/** Скрипт SDK в index.html грузится асинхронно — страница и «Помощь» не ждут telegram.org. */
export const SDK_SCRIPT_ID = "telegram-web-app-sdk";
export const SDK_WAIT_MS = 4_000;

/**
 * Дождаться SDK Telegram: он уже загружен, или скрипт загрузился, или не загрузился, или вышел срок.
 * Никогда не отклоняется: без SDK приложение покажет «Открой в Telegram» и «Помощь».
 */
export function waitForTelegramSdk(win: Window = window, timeoutMs = SDK_WAIT_MS): Promise<void> {
  if (win.Telegram?.WebApp) return Promise.resolve();
  const script = win.document.getElementById(SDK_SCRIPT_ID);
  // Скрипт мог успеть загрузиться или упасть до запуска приложения — index.html отмечает это в data-state.
  if (!script || script.dataset.state) return Promise.resolve();
  return new Promise<void>((resolve) => {
    const done = () => {
      clearTimeout(timer);
      script.removeEventListener("load", done);
      script.removeEventListener("error", done);
      resolve();
    };
    const timer = setTimeout(done, timeoutMs);
    script.addEventListener("load", done);
    script.addEventListener("error", done);
  });
}

/** Состояние скрипта SDK по отметке sdk-state.js: загрузился, не загрузился или ещё грузится. */
export function sdkScriptState(win: Window = window): "loaded" | "failed" | "pending" {
  const state = win.document.getElementById(SDK_SCRIPT_ID)?.dataset.state;
  return state === "loaded" || state === "failed" ? state : "pending";
}

/** Скрипт SDK загрузился или упал позже срока ожидания: `cb` вызывается один раз. Возвращает отписку. */
export function onSdkScriptSettled(win: Window, cb: () => void): () => void {
  const script = win.document.getElementById(SDK_SCRIPT_ID);
  if (!script) return () => undefined;
  const done = () => {
    off();
    cb();
  };
  const off = () => {
    script.removeEventListener("load", done);
    script.removeEventListener("error", done);
  };
  script.addEventListener("load", done);
  script.addEventListener("error", done);
  return off;
}

/**
 * Mini App открыт из Telegram: клиент Telegram кладёт параметры запуска в адрес (`#tgWebAppData=…`,
 * `tgWebAppVersion`) ещё до загрузки SDK. Признак нужен, чтобы медленный telegram.org внутри Telegram не
 * показал ложное «Открой в Telegram» (n-2). Сама строка не читается и никуда не передаётся.
 */
export function launchedFromTelegram(win: Window = window): boolean {
  const has = (part: string) => {
    const p = new URLSearchParams(part.replace(/^[#?]/, ""));
    return p.has("tgWebAppData") || p.has("tgWebAppVersion");
  };
  try {
    return has(win.location.hash) || has(win.location.search);
  } catch {
    return false;
  }
}

/**
 * API платформы для экранов. Вне Telegram (`webApp === null`) нативных кнопок нет — экраны рисуют
 * запасные кнопки в странице, а тактильный отклик молчит.
 */
export class TelegramApi {
  readonly webApp: TelegramWebApp | null;
  private readonly themeListeners = new Set<() => void>();

  constructor(webApp: TelegramWebApp | null) {
    this.webApp = webApp;
  }

  get inside(): boolean {
    return this.webApp !== null;
  }

  get initData(): string {
    return this.webApp?.initData ?? "";
  }

  get startParam(): string | null {
    const p = this.webApp?.initDataUnsafe?.start_param;
    return typeof p === "string" && p ? p : null;
  }

  get firstName(): string {
    const name = this.webApp?.initDataUnsafe?.user?.first_name;
    return typeof name === "string" ? name : "";
  }

  /**
   * Нативная кнопка отказала (метод SDK бросил): до конца сессии экраны рисуют запасную кнопку в странице,
   * а не пробуют нативную на каждом экране снова (A8-1.5-D6).
   */
  private mainBroken = false;
  private backBroken = false;

  private atLeast(version: string): boolean {
    const wa = this.webApp;
    if (!wa) return false;
    try {
      return wa.isVersionAtLeast(version) === true;
    } catch {
      return false;
    }
  }

  /** Нативная кнопка «Назад» есть с Bot API 6.1 и пока она не отказала. */
  get nativeBack(): boolean {
    return !this.backBroken && this.atLeast("6.1");
  }

  /** Нативная MainButton есть внутри Telegram, пока она не отказала. */
  get nativeMain(): boolean {
    return !this.mainBroken && !!this.webApp;
  }

  /**
   * Вызвать методы нативной MainButton. Отказ любого — кнопка помечается неработающей, обработчик снят,
   * кнопка скрыта (насколько SDK позволяет); возвращает `false` — экран рисует кнопку в странице.
   */
  withMain(fn: (mb: TgMainButton) => void): boolean {
    const wa = this.webApp;
    if (!wa || this.mainBroken) return false;
    if (safely(() => fn(wa.MainButton))) return true;
    this.mainBroken = true;
    safely(() => wa.MainButton.hideProgress());
    safely(() => wa.MainButton.hide());
    return false;
  }

  /** То же для нативной BackButton. */
  withBack(fn: (bb: TgBackButton) => void): boolean {
    const wa = this.webApp;
    if (!wa || this.backBroken) return false;
    if (safely(() => fn(wa.BackButton))) return true;
    this.backBroken = true;
    safely(() => wa.BackButton.hide());
    return false;
  }

  /** Тема Telegram → атрибут data-theme на <html>; токены theme.css переключаются по нему. */
  applyTheme(): void {
    const wa = this.webApp;
    if (!wa) return;
    document.documentElement.dataset.theme = wa.colorScheme === "dark" ? "dark" : "light";
    if (!this.atLeast("6.1")) return;
    const bg = cssToken("--bg");
    // Каждый вызов SDK — отдельно: отказ одного (старый клиент, неожиданный формат) не мешает остальным
    // и не роняет запуск приложения.
    safely(() => {
      // Цвет шапки как hex SDK принимает только с Bot API 6.9; в 6.1–6.8 — только ключ темы
      // (`bg_color` / `secondary_bg_color`), на любой другой hex он бросает WebAppHeaderColorKeyInvalid.
      if (this.atLeast("6.9")) {
        if (HEX.test(bg)) wa.setHeaderColor?.(bg);
      } else {
        wa.setHeaderColor?.("bg_color");
      }
    });
    // Фон как hex SDK принимает с 6.1.
    if (HEX.test(bg)) safely(() => wa.setBackgroundColor?.(bg));
  }

  /** Подписка на смену темы (после того как data-theme уже обновлён). Возвращает отписку. */
  onThemeChange(cb: () => void): () => void {
    this.themeListeners.add(cb);
    return () => this.themeListeners.delete(cb);
  }

  /**
   * Запуск: тема, подписка на смену темы, сигнал готовности. Возвращает отписку. Не бросает: ошибка SDK
   * не должна оставить человека на вечной заставке Telegram — `ready()` и `expand()` вызываются всегда.
   */
  start(): () => void {
    const wa = this.webApp;
    if (!wa) return () => undefined;
    const onTheme = () => {
      safely(() => this.applyTheme());
      for (const cb of [...this.themeListeners]) safely(cb);
    };
    safely(() => this.applyTheme());
    safely(() => wa.onEvent("themeChanged", onTheme));
    safely(() => wa.ready());
    safely(() => wa.expand());
    return () => void safely(() => wa.offEvent("themeChanged", onTheme));
  }

  /** Тактильный отклик — украшение: отказ SDK молча пропускается. */
  impact(style: HapticImpact = "light"): void {
    const wa = this.webApp;
    if (wa && this.atLeast("6.1")) safely(() => wa.HapticFeedback?.impactOccurred(style));
  }

  notify(type: HapticNotification): void {
    const wa = this.webApp;
    if (wa && this.atLeast("6.1")) safely(() => wa.HapticFeedback?.notificationOccurred(type));
  }

  /** Выбор чата Telegram для пересылки ссылки (t.me/share/url); `false` — вне Telegram или SDK отказал. */
  shareLink(url: string, text: string): boolean {
    const wa = this.webApp;
    if (!wa) return false;
    const share = `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(text)}`;
    return safely(() => wa.openTelegramLink(share));
  }

  onActivated(cb: () => void): () => void {
    const wa = this.webApp;
    if (!wa || !this.atLeast("8.0")) return () => undefined;
    if (!safely(() => wa.onEvent("activated", cb))) return () => undefined;
    return () => void safely(() => wa.offEvent("activated", cb));
  }
}
