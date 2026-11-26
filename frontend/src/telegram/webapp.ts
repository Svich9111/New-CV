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

/**
 * API платформы для экранов. Вне Telegram (`webApp === null`) нативных кнопок нет — экраны рисуют
 * запасные кнопки в странице, а тактильный отклик молчит.
 */
export class TelegramApi {
  readonly webApp: TelegramWebApp | null;

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
    if (HEX.test(bg)) {
      wa.setHeaderColor?.(bg);
      wa.setBackgroundColor?.(bg);
    }
  }

  /** Запуск: тема, подписка на смену темы, сигнал готовности. Возвращает отписку. */
  start(): () => void {
    const wa = this.webApp;
    if (!wa) return () => undefined;
    const onTheme = () => this.applyTheme();
    this.applyTheme();
    wa.onEvent("themeChanged", onTheme);
    wa.ready();
    wa.expand();
    return () => wa.offEvent("themeChanged", onTheme);
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
