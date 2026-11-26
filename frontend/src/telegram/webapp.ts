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

function safely(fn: () => void): void {
  try {
    fn();
  } catch {
    // Отказ SDK не журналируется (консоль не используется) и не прерывает запуск.
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
    return p ? p : null;
  }

  get firstName(): string {
    return this.webApp?.initDataUnsafe?.user?.first_name ?? "";
  }

  /** Нативная кнопка «Назад» есть с Bot API 6.1. */
  get nativeBack(): boolean {
    return !!this.webApp && this.webApp.isVersionAtLeast("6.1");
  }

  get nativeMain(): boolean {
    return !!this.webApp;
  }

  /** Тема Telegram → атрибут data-theme на <html>; токены theme.css переключаются по нему. */
  applyTheme(): void {
    const wa = this.webApp;
    if (!wa) return;
    document.documentElement.dataset.theme = wa.colorScheme === "dark" ? "dark" : "light";
    if (!wa.isVersionAtLeast("6.1")) return;
    const bg = cssToken("--bg");
    // Каждый вызов SDK — отдельно: отказ одного (старый клиент, неожиданный формат) не мешает остальным
    // и не роняет запуск приложения.
    safely(() => {
      // Цвет шапки как hex SDK принимает только с Bot API 6.9; в 6.1–6.8 — только ключ темы
      // (`bg_color` / `secondary_bg_color`), на любой другой hex он бросает WebAppHeaderColorKeyInvalid.
      if (wa.isVersionAtLeast("6.9")) {
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
    return () => safely(() => wa.offEvent("themeChanged", onTheme));
  }

  impact(style: HapticImpact = "light"): void {
    if (this.webApp?.isVersionAtLeast("6.1")) this.webApp.HapticFeedback?.impactOccurred(style);
  }

  notify(type: HapticNotification): void {
    if (this.webApp?.isVersionAtLeast("6.1")) this.webApp.HapticFeedback?.notificationOccurred(type);
  }

  /** Выбор чата Telegram для пересылки ссылки (t.me/share/url). */
  shareLink(url: string, text: string): boolean {
    if (!this.webApp) return false;
    const share = `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(text)}`;
    this.webApp.openTelegramLink(share);
    return true;
  }

  onActivated(cb: () => void): () => void {
    const wa = this.webApp;
    if (!wa || !wa.isVersionAtLeast("8.0")) return () => undefined;
    wa.onEvent("activated", cb);
    return () => wa.offEvent("activated", cb);
  }
}
