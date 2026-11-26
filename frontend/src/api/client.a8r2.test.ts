/**
 * A8, задача 1.5, раунд 2 — проверка исправлений клиента API (D1, D2, m-2 из docs/reviews/1.5-test-r1.md
 * и 1.5-review-r1.md). Основание: карточка 1.5 («401 → повторный вход… без цикла; обрыв связи — причина
 * подписью, повтор»), ФТ-01, ТЗ §9.3 («контакты помощи доступны всегда»), контракт api/openapi.yaml.
 * Дефекты — `it.fails` с пометкой `A8-1.5-Dn` (зеленеют, пока дефект есть).
 */
import { Api, ApiError, TIMEOUTS } from "./client";
import { FakeServer } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";

function apiFor(server: FakeServer, timeouts?: Partial<typeof TIMEOUTS>): Api {
  return new Api({ initData: () => INIT_DATA, fetch: (i, init) => server.fetch(i, init), timeouts });
}

async function caught(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p;
  } catch (e) {
    if (e instanceof ApiError) return e;
    throw new Error(`ожидалась ApiError, получено ${String(e)}`);
  }
  throw new Error("ожидалась ApiError, а запрос выполнился");
}

type Method = "me" | "saveOnboarding" | "couple" | "createCouple" | "reissueInvite" | "invite" | "acceptInvite" | "helpContacts";
const ONBOARDING = { name: "Аня", address_form: "Аня", partner_name: "Игорь", consent_terms: true, consent_data: true, consent_sensitive: true, age_confirmed: true } as const;
const CALLS: [Method, string, string, (a: Api) => Promise<unknown>][] = [
  ["me", "GET", "/me", (a) => a.me()],
  ["saveOnboarding", "PUT", "/me/onboarding", (a) => a.saveOnboarding(ONBOARDING)],
  ["couple", "GET", "/couple", (a) => a.couple()],
  ["createCouple", "POST", "/couple", (a) => a.createCouple()],
  ["reissueInvite", "POST", "/couple/invite", (a) => a.reissueInvite()],
  ["invite", "GET", `/invites/${"A".repeat(43)}`, (a) => a.invite("A".repeat(43))],
  ["acceptInvite", "POST", `/invites/${"A".repeat(43)}/accept`, (a) => a.acceptInvite("A".repeat(43))],
  ["helpContacts", "GET", "/help/contacts", (a) => a.helpContacts()],
];

describe("A8 р.2 · D2: 2xx с телом не по схеме — ApiError bad_response на каждом пути 1.5", () => {
  const BODIES: [string, string, string][] = [
    ["HTML от прокси", "<!doctype html><html></html>", "text/html"],
    ["пустое тело с 200", "", "application/json"],
    ["JSON null", "null", "application/json"],
    ["JSON-строка", '"ok"', "application/json"],
    ["JSON-число", "42", "application/json"],
    ["обрезанный JSON", '{"id": "x"', "application/json"],
  ];
  for (const [name, method, path, run] of CALLS) {
    for (const [label, body, type] of BODIES) {
      test(`${name}: ${label} — bad_response, повтор помогает`, async () => {
        const server = new FakeServer().onboarded();
        const api = apiFor(server);
        await api.login();
        const base = server.fetch;
        server.fetch = async (i, init) =>
          String(i).endsWith(path) && (init?.method ?? "GET") === method
            ? new Response(body, { status: 200, headers: { "Content-Type": type } })
            : base(i, init);
        const e = await caught(run(apiFor(server)));
        expect(e).toMatchObject({ status: 200, code: "bad_response" });
        expect(e.message.length).toBeGreaterThan(0);
        expect(e.message).not.toContain("<");
      });
    }
  }

  test("вход: 200 с JSON, но без access_token или без me — ApiError, а не TypeError и не «вошли»", async () => {
    for (const body of [{}, { access_token: "t" }, { me: { id: "x" } }, { access_token: 1, me: {} }, []]) {
      const server = new FakeServer();
      server.inject("POST", "/auth/telegram", 200, body);
      const api = apiFor(server);
      const e = await caught(api.login());
      expect(e.code).toBe("bad_response");
      expect(api.hasSession).toBe(false);
    }
  });

  test("GET /couple 200 с чужой схемой — ошибка, а не «пары нет» (null)", async () => {
    for (const body of [{}, [], { status: "pending" }, { members: [] }]) {
      const server = new FakeServer().onboarded();
      const api = apiFor(server);
      await api.login();
      server.inject("GET", "/couple", 200, body);
      expect((await caught(api.couple())).code).toBe("bad_response");
    }
  });

  // A8-1.5-D5 (р.2): 204 принимается как успех на любом пути, хотя ни один путь 1.5 по контракту 204 не отдаёт
  // (`api/openapi.yaml`: 204 — только /me DELETE, /couple/leave и пути 2.x+). GET /couple 204 → `undefined`,
  // и экран приглашения показывает «Ссылки пока нет» — тот же симптом, что D2(в) р.1; GET /invites 204 роняет
  // экран (`state.invite.inviter_name`), POST /auth/telegram 204 — TypeError вместо ApiError.
  it.fails("A8-1.5-D5: GET /couple 204 — ошибка, а не «пары нет»", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    server.inject("GET", "/couple", 204);
    expect((await caught(api.couple())).code).toBe("bad_response");
  });

  it.fails("A8-1.5-D5: POST /auth/telegram 204 — ApiError, а не TypeError", async () => {
    const server = new FakeServer();
    server.inject("POST", "/auth/telegram", 204);
    await expect(apiFor(server).login()).rejects.toBeInstanceOf(ApiError);
  });

  test("факт по D5: GET /invites/{token} 204 сейчас разрешается undefined", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    server.inject("GET", `/invites/${"A".repeat(43)}`, 204);
    await expect(api.invite("A".repeat(43))).resolves.toBeUndefined();
  });
});

describe("A8 р.2 · D1: срок ответа на каждом пути (TIMEOUTS)", () => {
  afterEach(() => vi.useRealTimers());

  for (const [name, method, path, run] of CALLS) {
    const limit = name === "helpContacts" ? TIMEOUTS.help : TIMEOUTS.default;
    test(`${name}: повис — timeout ровно по сроку ${limit} мс, не раньше; запрос прерван сигналом`, async () => {
      vi.useFakeTimers();
      const server = new FakeServer().onboarded();
      const api = apiFor(server);
      await api.login();
      const base = server.fetch;
      let signal: AbortSignal | undefined;
      // fetch, который сигнал не слушает: ожидание всё равно должно закончиться по сроку.
      server.fetch = (i, init) => {
        if (String(i).endsWith(path) && (init?.method ?? "GET") === method) {
          signal = init?.signal ?? undefined;
          return new Promise<Response>(() => undefined);
        }
        return base(i, init);
      };
      let settled = false;
      const p = caught(run(api)).finally(() => (settled = true));
      await vi.advanceTimersByTimeAsync(limit - 1);
      expect(settled).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      const e = await p;
      expect(e).toMatchObject({ status: 0, code: "timeout" });
      expect(e.message).toMatch(/не ответил вовремя/);
      expect(signal?.aborted).toBe(true);
    });
  }

  test("вход: срок TIMEOUTS.login (15 с), повтор после срока — новый запрос и успех", async () => {
    vi.useFakeTimers();
    const server = new FakeServer();
    const base = server.fetch;
    let hang = true;
    server.fetch = (i, init) => (hang ? new Promise<Response>(() => undefined) : base(i, init));
    const api = apiFor(server);
    const p = caught(api.login());
    await vi.advanceTimersByTimeAsync(TIMEOUTS.login);
    expect((await p).code).toBe("timeout");
    expect(api.hasSession).toBe(false);
    hang = false;
    await expect(api.login()).resolves.toMatchObject({ id: expect.any(String) });
    expect(api.hasSession).toBe(true);
  });

  test("ответ пришёл за 1 мс до срока — успех, таймер снят (нет ложного timeout позже)", async () => {
    vi.useFakeTimers();
    const server = new FakeServer();
    const base = server.fetch;
    server.fetch = (i, init) =>
      new Promise<Response>((res) => setTimeout(() => void base(i, init).then(res), TIMEOUTS.login - 1));
    const api = apiFor(server);
    const p = api.login();
    await vi.advanceTimersByTimeAsync(TIMEOUTS.login - 1);
    await expect(p).resolves.toBeTruthy();
    expect(vi.getTimerCount()).toBe(0);
  });

  test("тело приходит частями дольше срока — тоже timeout", async () => {
    vi.useFakeTimers();
    const server = new FakeServer();
    server.fetch = async () =>
      new Response(
        new ReadableStream({
          start(c) {
            c.enqueue(new TextEncoder().encode('{"access_token":'));
            // остаток тела не приходит никогда
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    const p = caught(apiFor(server).login());
    await vi.advanceTimersByTimeAsync(TIMEOUTS.login);
    expect((await p).code).toBe("timeout");
  });
});

describe("A8 р.2 · m-2: одновременные 401 — один повторный вход (ФТ-01, карточка «без цикла»)", () => {
  test("5 параллельных запросов с истёкшим токеном — ровно 1 повторный вход, каждый запрос повторён 1 раз", async () => {
    const server = new FakeServer().onboarded().pendingCouple();
    const api = apiFor(server);
    await api.login();
    server.expireSession();
    await Promise.all([api.me(), api.couple(), api.me(), api.couple(), api.helpContacts()]);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2); // первый вход + один повторный
    expect(server.calls("GET", "/me")).toHaveLength(4);
    expect(server.calls("GET", "/couple")).toHaveLength(4);
  });

  test("повторный вход отклонён — все параллельные запросы получают ошибку, вход один, цикла нет", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    server.expireSession();
    server.inject("POST", "/auth/telegram", 401, { code: "invalid_init_data", message: "x" }, undefined, 100);
    const res = await Promise.allSettled([api.me(), api.couple(), api.me()]);
    expect(res.every((r) => r.status === "rejected")).toBe(true);
    for (const r of res) expect((r as PromiseRejectedResult).reason).toMatchObject({ status: 401, code: "invalid_init_data" });
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
    expect(server.calls("GET", "/me")).toHaveLength(2); // по одному запросу, без повтора: вход не удался
  });

  test("запрос, начатый после неудачного входа, начинает новый вход (не залипает на старом промисе)", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    server.inject("POST", "/auth/telegram", 503, { code: "service_unavailable", message: "x" });
    await caught(api.login());
    await expect(api.me()).resolves.toMatchObject({ onboarding_completed: true });
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
  });

  // Добавлены после мутаций р.2 (M4, M5 выживали все прежние тесты).
  test("два login() подряд, пока первый идёт, — один POST /auth/telegram, один и тот же результат", async () => {
    const server = new FakeServer();
    const api = apiFor(server);
    const [a, b] = await Promise.all([api.login(), api.login()]);
    expect(a).toEqual(b);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
  });

  test("401 пришёл позже, чем другой запрос уже обновил токен, — нового входа нет, только повтор", async () => {
    const server = new FakeServer().onboarded().pendingCouple();
    const api = apiFor(server);
    await api.login();
    server.expireSession();
    // Ответ на GET /couple (со старым токеном) задерживается до тех пор, пока GET /me не войдёт заново.
    const base = server.fetch;
    let releaseCouple: () => void = () => undefined;
    let firstCouple = true;
    server.fetch = async (i, init) => {
      if (firstCouple && String(i).endsWith("/couple")) {
        firstCouple = false;
        const res = await base(i, init); // 401: токен уже истёк
        await new Promise<void>((r) => (releaseCouple = r));
        return res;
      }
      return base(i, init);
    };
    const couple = api.couple();
    await api.me(); // 401 → повторный вход → повтор → успех; токен новый
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
    releaseCouple();
    await expect(couple).resolves.toMatchObject({ status: "pending" });
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
    expect(server.calls("GET", "/couple")).toHaveLength(2);
  });

  test("сервер всегда 401 на защищённом пути — на запрос не больше 2 входов и 2 попыток", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    server.inject("GET", "/me", 401, { code: "unauthorized", message: "x" }, undefined, 100);
    const e = await caught(api.me());
    expect(e.status).toBe(401);
    expect(server.calls("POST", "/auth/telegram").length).toBeLessThanOrEqual(2);
    expect(server.calls("GET", "/me").length).toBeLessThanOrEqual(2);
  });
});
