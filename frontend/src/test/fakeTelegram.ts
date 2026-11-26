import type { TelegramWebApp } from "../telegram/webapp";

/** Подделка `Telegram.WebApp` для тестов: запоминает состояние нативных кнопок, ссылки и отклик. */
export interface FakeTelegram {
  webApp: TelegramWebApp;
  main: { text: string; visible: boolean; active: boolean; progress: boolean; params: Record<string, unknown>; click(): void };
  back: { visible: boolean; click(): void };
  opened: string[];
  haptics: string[];
  headerColors: string[];
  backgroundColors: string[];
  emit(event: "themeChanged" | "activated"): void;
  readyCalled: () => boolean;
}

export const INIT_DATA = "query_id=TEST&user=%7B%22id%22%3A1%2C%22first_name%22%3A%22%D0%90%D0%BD%D1%8F%22%7D&auth_date=1&hash=fake";

export function fakeTelegram(
  opts: {
    initData?: string;
    startParam?: string;
    colorScheme?: "light" | "dark";
    version?: string;
    firstName?: string;
    /** Цвета темы клиента Telegram (не совпадают с нашими токенами — как обычно и бывает). */
    themeParams?: { bg_color?: string; secondary_bg_color?: string };
    /** Методы SDK, которые бросают исключение (проверка устойчивости запуска). */
    throwOn?: ("setHeaderColor" | "setBackgroundColor" | "onEvent" | "expand")[];
  } = {},
): FakeTelegram {
  const mainHandlers = new Set<() => void>();
  const backHandlers = new Set<() => void>();
  const events = new Map<string, Set<() => void>>();
  let ready = false;
  const version = opts.version ?? "8.0";
  const cmp = (a: string, b: string) => {
    const pa = a.split(".").map(Number);
    const pb = b.split(".").map(Number);
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
      const d = (pa[i] ?? 0) - (pb[i] ?? 0);
      if (d) return d;
    }
    return 0;
  };

  const atLeast = (v: string) => cmp(version, v) >= 0;
  const themeParams = opts.themeParams ?? { bg_color: "#17212b", secondary_bg_color: "#232e3c" };
  const broken = (name: NonNullable<typeof opts.throwOn>[number]) => {
    if (opts.throwOn?.includes(name)) throw new Error(`fake ${name} failure`);
  };
  const HEX = /^#[0-9a-f]{6}$/i;
  const isKey = (c: string) => c === "bg_color" || c === "secondary_bg_color";

  const fake: FakeTelegram = {
    main: {
      text: "",
      visible: false,
      active: true,
      progress: false,
      params: {},
      click: () => mainHandlers.forEach((h) => h()),
    },
    back: { visible: false, click: () => backHandlers.forEach((h) => h()) },
    opened: [],
    haptics: [],
    headerColors: [],
    backgroundColors: [],
    emit: (e) => events.get(e)?.forEach((h) => h()),
    readyCalled: () => ready,
    webApp: undefined as unknown as TelegramWebApp,
  };

  fake.webApp = {
    initData: opts.initData ?? INIT_DATA,
    initDataUnsafe: { start_param: opts.startParam, user: { first_name: opts.firstName ?? "Аня" } },
    version,
    colorScheme: opts.colorScheme ?? "light",
    isVersionAtLeast: atLeast,
    ready: () => {
      ready = true;
    },
    expand: () => broken("expand"),
    themeParams,
    // Как официальный telegram-web-app.js: до 6.1 — предупреждение без смены цвета; в 6.1–6.8 — только ключ
    // темы (или hex, равный цвету темы клиента), иначе исключение WebAppHeaderColorKeyInvalid; с 6.9 — hex.
    // Вывод SDK в консоль не повторяется: консоль в тестах запрещена.
    setHeaderColor: (c) => {
      broken("setHeaderColor");
      if (!atLeast("6.1")) return;
      let color = c;
      if (!atLeast("6.9")) {
        if (themeParams.bg_color && themeParams.bg_color === c) color = "bg_color";
        else if (themeParams.secondary_bg_color && themeParams.secondary_bg_color === c) color = "secondary_bg_color";
        if (!isKey(color)) throw new Error("WebAppHeaderColorKeyInvalid");
      } else if (!isKey(color) && !HEX.test(color)) {
        throw new Error("WebAppHeaderColorInvalid");
      }
      fake.headerColors.push(color);
    },
    setBackgroundColor: (c) => {
      broken("setBackgroundColor");
      if (!atLeast("6.1")) return;
      if (!isKey(c) && !HEX.test(c)) throw new Error("WebAppBackgroundColorInvalid");
      fake.backgroundColors.push(c);
    },
    onEvent: (e, cb) => {
      broken("onEvent");
      if (!events.has(e)) events.set(e, new Set());
      events.get(e)!.add(cb);
    },
    offEvent: (e, cb) => events.get(e)?.delete(cb),
    openTelegramLink: (url) => fake.opened.push(url),
    MainButton: {
      setText: (t) => (fake.main.text = t),
      show: () => (fake.main.visible = true),
      hide: () => (fake.main.visible = false),
      enable: () => (fake.main.active = true),
      disable: () => (fake.main.active = false),
      showProgress: () => (fake.main.progress = true),
      hideProgress: () => (fake.main.progress = false),
      setParams: (p) => Object.assign(fake.main.params, p),
      // Настоящая MainButton вызывает обработчик и у неактивной кнопки — это проверяет защиту в MainButton.
      onClick: (cb) => mainHandlers.add(cb),
      offClick: (cb) => mainHandlers.delete(cb),
    },
    BackButton: {
      show: () => (fake.back.visible = true),
      hide: () => (fake.back.visible = false),
      onClick: (cb) => backHandlers.add(cb),
      offClick: (cb) => backHandlers.delete(cb),
    },
    HapticFeedback: {
      impactOccurred: (s) => fake.haptics.push(`impact:${s}`),
      notificationOccurred: (t) => fake.haptics.push(`notify:${t}`),
      selectionChanged: () => fake.haptics.push("selection"),
    },
  };
  return fake;
}
