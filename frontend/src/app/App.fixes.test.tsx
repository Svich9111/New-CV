/**
 * Исправления задачи 1.5 по ревью A6 и тестам A8 (раунд 1): старые клиенты Telegram, срок ответа,
 * ответы не того вида, черновик онбординга, повтор принятия приглашения, опрос при потерянной сессии,
 * распавшаяся пара и «Помощь» на каждом экране.
 */
import { act, screen, waitFor } from "@testing-library/react";
import { TIMEOUTS } from "../api/client";
import { POLL_MS } from "../screens/Invite";
import { FakeServer, PARTNER_TOKEN } from "../test/fakeApi";
import { fakeTelegram } from "../test/fakeTelegram";
import { heading, renderApp, type Rendered } from "../test/renderApp";
import type { Route } from "./routes";

const CONSENTS = [
  "Принимаю правила сервиса",
  "Даю согласие на обработку персональных данных",
  "Даю отдельное согласие на обработку сведений о чувствах и отношениях",
  "Мне исполнилось 18 лет",
];

const helpLink = () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" });

async function toOnboarding(r: Rendered) {
  await heading("Твой личный агент");
  await r.pressMain();
  await r.pressMain();
  await r.pressMain();
  await heading("Давай познакомимся");
}

async function fillAll(r: Rendered) {
  await r.user.type(screen.getByLabelText("Как к тебе обращаться"), "Аня");
  await r.user.type(screen.getByLabelText("Имя партнёра"), "Игорь");
  for (const c of CONSENTS) await r.user.click(screen.getByRole("checkbox", { name: c }));
}

/** Сервер отвечает на путь 200 с HTML (прокси, заглушка) — ставится до renderApp. */
function html200(server: FakeServer, suffix: string) {
  const base = server.fetch;
  server.fetch = async (i, init) =>
    String(i).endsWith(suffix) ? new Response("<!doctype html><html></html>", { status: 200, headers: { "Content-Type": "text/html" } }) : base(i, init);
}

describe("MAJOR-1: Bot API 6.1–6.8 — приложение запускается", () => {
  for (const version of ["6.1", "6.2", "6.8"]) {
    test(`Bot API ${version}: ready() вызван, приветствие на экране, шапка — ключ bg_color`, async () => {
      const tg = fakeTelegram({ version, colorScheme: "dark" });
      renderApp({ tg });
      expect(await heading("Твой личный агент")).toBeInTheDocument();
      expect(tg.readyCalled()).toBe(true);
      expect(tg.headerColors.every((c) => c === "bg_color")).toBe(true);
      act(() => tg.emit("themeChanged"));
      expect(await heading("Твой личный агент")).toBeInTheDocument();
    });
  }
});

describe("D1: срок ответа", () => {
  test("вход повис — через TIMEOUTS.login причина подписью и «Попробовать снова», повтор входит", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const server = new FakeServer();
      const base = server.fetch;
      let hang = true;
      server.fetch = (i, init) => (hang ? new Promise<Response>(() => undefined) : base(i, init));
      const r = renderApp({ server });
      expect(await screen.findByText("Входим через Telegram…")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(TIMEOUTS.login);
      });
      expect(await heading("Не получилось войти")).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent("Сервер не ответил вовремя");
      expect(r.tg!.main).toMatchObject({ visible: true, text: "Попробовать снова" });
      expect(helpLink()).toBeInTheDocument();
      hang = false;
      await r.pressMain();
      expect(await heading("Твой личный агент")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  test("/help/contacts повис — 112 виден сразу, по сроку — пояснение «не получилось загрузить»", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const server = new FakeServer();
      const base = server.fetch;
      server.fetch = (i, init) => (String(i).endsWith("/help/contacts") ? new Promise<Response>(() => undefined) : base(i, init));
      const r = renderApp({ server });
      await heading("Твой личный агент");
      await r.user.click(helpLink());
      await heading("Помощь");
      expect(screen.getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(TIMEOUTS.help);
      });
      expect(await screen.findByText(/Не получилось загрузить полный список/)).toBeInTheDocument();
      expect(screen.queryByText("Загружаем контакты…")).toBeNull();
      expect(screen.getByRole("link", { name: "112" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("D2: 2xx с телом не того вида", () => {
  test("«Помощь»: /help/contacts 200 text/html — запасной 112 и пояснение, экран не падает", async () => {
    const server = new FakeServer();
    html200(server, "/help/contacts");
    const r = renderApp({ server });
    await heading("Твой личный агент");
    await r.user.click(helpLink());
    expect(await screen.findByText(/Не получилось загрузить полный список/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
  });

  test("«Помощь»: в списке с сервера записи не той схемы отброшены, пустой итог — 112", async () => {
    const server = new FakeServer().inject("GET", "/help/contacts", 200, [null, { name: 1 }, { name: "x" }]);
    const r = renderApp({ server });
    await heading("Твой личный агент");
    await r.user.click(helpLink());
    await waitFor(() => expect(screen.queryByText("Загружаем контакты…")).toBeNull());
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "112" })).toBeInTheDocument();
  });

  test("входящее приглашение: 200 text/html — «Приглашение не работает», повтор и «Помощь», без падения", async () => {
    const server = new FakeServer().partnerInvite();
    html200(server, `/invites/${PARTNER_TOKEN}`);
    const r = renderApp({ server, tg: fakeTelegram({ startParam: PARTNER_TOKEN }) });
    expect(await heading("Приглашение не работает")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Сервер ответил непонятно");
    expect(r.tg!.main.text).toBe("Попробовать снова");
    expect(screen.getByRole("button", { name: "Продолжить без приглашения" })).toBeInTheDocument();
    await r.user.click(helpLink());
    expect(await screen.findByRole("link", { name: "112" })).toBeInTheDocument();
  });

  test("GET /couple 200 text/html — ошибка с повтором, а не «пары нет»", async () => {
    const server = new FakeServer().onboarded().pendingCouple();
    html200(server, "/couple");
    const r = renderApp({ server });
    await heading("Пригласи партнёра");
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервер ответил непонятно");
    expect(screen.queryByText(/Ссылки пока нет/)).toBeNull();
    expect(r.tg!.main.text).toBe("Попробовать снова");
  });
});

describe("m-3: опрос при потерянной сессии", () => {
  test("токен истёк, повторный вход отклонён — опрос останавливается, причина подписью", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const server = new FakeServer().onboarded().pendingCouple();
      const r = renderApp({ server });
      await screen.findByText(/Приглашение уже создано/);
      server.expireSession();
      server.inject("POST", "/auth/telegram", 401, { code: "invalid_init_data", message: "x" }, undefined, 1000);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_MS);
      });
      expect(await screen.findByRole("alert")).toHaveTextContent("Не получилось подтвердить вход через Telegram");
      expect(screen.queryByText(/Приглашение уже создано/)).toBeNull();
      const logins = server.calls("POST", "/auth/telegram").length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_MS * 3);
      });
      expect(server.calls("POST", "/auth/telegram")).toHaveLength(logins);
      expect(r.tg!.main.text).toBe("Попробовать снова");
    } finally {
      vi.useRealTimers();
    }
  });

  test("временный сбой тихого опроса (5xx) — по-прежнему без шума, опрос продолжается", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const server = new FakeServer().onboarded().pendingCouple();
      renderApp({ server });
      await screen.findByText(/Приглашение уже создано/);
      server.inject("GET", "/couple", 503, { code: "service_unavailable", message: "x" });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_MS);
      });
      expect(screen.queryByRole("alert")).toBeNull();
      server.partnerAccepts();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_MS);
      });
      expect(await heading("Вы пара")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("m-4: онбординг → «Помощь» → «Назад» — введённое сохраняется", () => {
  test("поля и флажки на месте", async () => {
    const r = renderApp();
    await toOnboarding(r);
    await r.user.type(screen.getByLabelText("Как к тебе обращаться"), "Аня, на «ты»");
    await r.user.type(screen.getByLabelText("Имя партнёра"), "Игорь");
    await r.user.click(screen.getByRole("checkbox", { name: CONSENTS[0] }));
    await r.user.click(screen.getByRole("checkbox", { name: CONSENTS[2] }));
    await r.user.click(helpLink());
    await heading("Помощь");
    await r.pressBack();
    await heading("Давай познакомимся");
    expect(screen.getByLabelText("Твоё имя")).toHaveValue("Аня");
    expect(screen.getByLabelText("Как к тебе обращаться")).toHaveValue("Аня, на «ты»");
    expect(screen.getByLabelText("Имя партнёра")).toHaveValue("Игорь");
    expect(CONSENTS.map((c) => (screen.getByRole("checkbox", { name: c }) as HTMLInputElement).checked)).toEqual([
      true,
      false,
      true,
      false,
    ]);
  });
});

describe("m-6: принять приглашение не удалось после онбординга", () => {
  async function partnerToAccept(server: FakeServer): Promise<Rendered> {
    const r = renderApp({ server, tg: fakeTelegram({ startParam: PARTNER_TOKEN, firstName: "Аня" }) });
    await heading("Игорь приглашает тебя");
    await r.pressMain();
    await toOnboarding(r);
    await r.user.type(screen.getByLabelText("Как к тебе обращаться"), "Аня");
    for (const c of CONSENTS) await r.user.click(screen.getByRole("checkbox", { name: c }));
    await r.pressMain();
    return r;
  }

  test("сбой сервера — «Принять приглашение снова» только принимает (онбординг не отправляется второй раз)", async () => {
    const server = new FakeServer().partnerInvite().inject("POST", `/invites/${PARTNER_TOKEN}/accept`, 503, {
      code: "service_unavailable",
      message: "x",
    });
    const r = await partnerToAccept(server);
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервис временно недоступен");
    expect(r.tg!.main).toMatchObject({ visible: true, text: "Принять приглашение снова" });
    expect(screen.queryByLabelText("Имя партнёра")).toBeNull();
    expect(screen.getByText(/Знакомство сохранено/)).toBeInTheDocument();
    await r.pressMain();
    expect(await heading("Вы пара")).toBeInTheDocument();
    expect(server.calls("PUT", "/me/onboarding")).toHaveLength(1);
    expect(server.calls("POST", `/invites/${PARTNER_TOKEN}/accept`)).toHaveLength(2);
  });

  for (const [label, status, code] of [
    ["410 invite_expired", 410, "invite_expired"],
    ["410 invite_used", 410, "invite_used"],
    ["404 not_found", 404, "not_found"],
  ] as const) {
    test(`${label} — одно действие: «Продолжить без приглашения», MainButton скрыта`, async () => {
      const server = new FakeServer().partnerInvite().inject("POST", `/invites/${PARTNER_TOKEN}/accept`, status, { code, message: "x" });
      const r = await partnerToAccept(server);
      await screen.findByRole("alert");
      expect(r.tg!.main.visible).toBe(false);
      await r.pressMain();
      expect(server.calls("PUT", "/me/onboarding")).toHaveLength(1);
      expect(server.calls("POST", `/invites/${PARTNER_TOKEN}/accept`)).toHaveLength(1);
      await r.user.click(screen.getByRole("button", { name: "Продолжить без приглашения" }));
      expect(await heading("Пригласи партнёра")).toBeInTheDocument();
    });
  }
});

describe("m-12: пара распалась (Couple.status = dissolved)", () => {
  test("отдельное состояние подписью и «Создать ссылку-приглашение», опроса нет", async () => {
    const server = new FakeServer().onboarded().pendingCouple();
    server.couple = { ...server.couple!, status: "dissolved" };
    const r = renderApp({ server });
    expect(await screen.findByText("Прежней пары больше нет.")).toBeInTheDocument();
    expect(screen.getByText("Прежней пары больше нет.").closest("[role=status]")).toHaveAttribute("data-status", "dissolved");
    expect(screen.queryByText(/Ждём партнёра/)).toBeNull();
    expect(r.tg!.main.text).toBe("Создать ссылку-приглашение");
    expect(screen.queryByRole("button", { name: "Проверить, принято ли приглашение" })).toBeNull();
  });
});

describe("m-8: «Помощь» на каждом экране (по всем маршрутам Route)", () => {
  // Record по Route["name"]: новый маршрут без строки здесь не скомпилируется.
  const SCREENS: Record<Route["name"], { h1: string; open: () => Promise<Rendered> } | "self"> = {
    welcome: {
      h1: "Чем мы не являемся",
      open: async () => {
        const r = renderApp();
        for (const h of ["Твой личный агент", "Что приватно"]) {
          await heading(h);
          expect(helpLink()).toBeInTheDocument();
          await r.pressMain();
        }
        return r;
      },
    },
    onboarding: {
      h1: "Давай познакомимся",
      open: async () => {
        const r = renderApp();
        await toOnboarding(r);
        return r;
      },
    },
    invite: { h1: "Пригласи партнёра", open: async () => renderApp({ server: new FakeServer().onboarded() }) },
    incoming: {
      h1: "Игорь приглашает тебя",
      open: async () => renderApp({ server: new FakeServer().partnerInvite(), tg: fakeTelegram({ startParam: PARTNER_TOKEN }) }),
    },
    paired: {
      h1: "Вы пара",
      open: async () => {
        const server = new FakeServer().onboarded().pendingCouple();
        server.partnerAccepts();
        return renderApp({ server });
      },
    },
    underage: {
      h1: "Сервис доступен только с 18 лет",
      open: async () => {
        const r = renderApp({ server: new FakeServer().inject("PUT", "/me/onboarding", 403, { code: "underage", message: "x" }) });
        await toOnboarding(r);
        await fillAll(r);
        await r.pressMain();
        return r;
      },
    },
    help: "self",
  };

  for (const [name, s] of Object.entries(SCREENS)) {
    if (s === "self") {
      test(`${name}: сам экран «Помощь» — без ссылки на себя, с «Назад»`, async () => {
        const r = renderApp();
        await heading("Твой личный агент");
        await r.user.click(helpLink());
        await heading("Помощь");
        expect(screen.queryByRole("button", { name: "Помощь и контакты служб поддержки" })).toBeNull();
        expect(r.tg!.back.visible).toBe(true);
      });
      continue;
    }
    test(`${name}: ссылка есть, «Помощь» с 112 открывается, «Назад» возвращает`, async () => {
      const r = await s.open();
      await heading(s.h1);
      await r.user.click(helpLink());
      await heading("Помощь");
      expect(screen.getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
      await r.pressBack();
      expect(await heading(s.h1)).toBeInTheDocument();
    });
  }
});
