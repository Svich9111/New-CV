import type { TelegramWebApp } from "../telegram/webapp";

/** Подделка `Telegram.WebApp` для тестов: запоминает состояние нативных кнопок, ссылки и отклик. */
export interface FakeTelegram {
  webApp: TelegramWebApp;
  main: { text: string; visible: boolean; active: boolean; progress: boolean; params: Record<string, unknown>; click(): void };
  back: { visible: boolean; click(): void };
  opened: string[];
  haptics: string[];
  headerColors: string[];
  emit(event: "themeChanged" | "activated"): void;
  readyCalled: () => boolean;
}

export const INIT_DATA = "query_id=TEST&user=%7B%22id%22%3A1%2C%22first_name%22%3A%22%D0%90%D0%BD%D1%8F%22%7D&auth_date=1&hash=fake";

export function fakeTelegram(
  opts: { initData?: string; startParam?: string; colorScheme?: "light" | "dark"; version?: string; firstName?: string } = {},
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
    emit: (e) => events.get(e)?.forEach((h) => h()),
    readyCalled: () => ready,
    webApp: undefined as unknown as TelegramWebApp,
  };

  fake.webApp = {
    initData: opts.initData ?? INIT_DATA,
    initDataUnsafe: { start_param: opts.startParam, user: { first_name: opts.firstName ?? "Аня" } },
    version,
    colorScheme: opts.colorScheme ?? "light",
    isVersionAtLeast: (v) => cmp(version, v) >= 0,
    ready: () => {
      ready = true;
    },
    expand: () => undefined,
    setHeaderColor: (c) => fake.headerColors.push(c),
    setBackgroundColor: () => undefined,
    onEvent: (e, cb) => {
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
