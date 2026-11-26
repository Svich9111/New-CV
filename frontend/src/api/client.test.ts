import { Api, ApiError, TIMEOUTS } from "./client";
import { FakeServer, json } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";

function setup(initData = INIT_DATA) {
  const server = new FakeServer();
  const api = new Api({ initData: () => initData, fetch: server.fetch });
  return { server, api };
}

async function caught(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p;
  } catch (e) {
    expect(e).toBeInstanceOf(ApiError);
    return e as ApiError;
  }
  throw new Error("ожидалась ошибка");
}

describe("Api — вход и сессия (ФТ-01)", () => {
  test("POST /auth/telegram отправляет initData как есть, без заголовка Authorization", async () => {
    const { server, api } = setup();
    const me = await api.login();
    expect(me.onboarding_completed).toBe(false);
    const [call] = server.calls("POST", "/auth/telegram");
    expect(call.body).toEqual({ init_data: INIT_DATA });
    expect(call.auth).toBeNull();
  });

  test("токен сессии уходит в Authorization и не попадает в хранилища браузера", async () => {
    const { server, api } = setup();
    await api.login();
    await api.me();
    expect(server.calls("GET", "/me")[0].auth).toBe(`Bearer ${server.token}`);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });

  test("защищённый запрос без сессии: сначала вход, без запроса с пустым Authorization", async () => {
    const { server, api } = setup();
    await api.me();
    expect(server.log.map((l) => `${l.method} ${l.path}`)).toEqual(["POST /auth/telegram", "GET /me"]);
    expect(server.calls("GET", "/me")[0].auth).toBe(`Bearer ${server.token}`);
  });

  test("неверная подпись initData — 401 invalid_init_data с понятным текстом", async () => {
    const { api } = setup("forged");
    const e = await caught(api.login());
    expect(e.status).toBe(401);
    expect(e.code).toBe("invalid_init_data");
    expect(e.message).toMatch(/Telegram/);
    expect(api.hasSession).toBe(false);
  });

  test("401 на защищённом пути: один повторный вход и один повтор запроса", async () => {
    const { server, api } = setup();
    await api.login();
    server.expireSession();
    const me = await api.me();
    expect(me.id).toBe(server.me.id);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
    expect(server.calls("GET", "/me")).toHaveLength(2);
  });

  test("повторный вход не удался — ошибка входа, без бесконечного цикла", async () => {
    const { server, api } = setup();
    await api.login();
    server.inject("GET", "/me", 401, { code: "unauthorized", message: "Нет сессии" });
    server.inject("POST", "/auth/telegram", 401, { code: "invalid_init_data", message: "x" });
    const e = await caught(api.me());
    expect(e.code).toBe("invalid_init_data");
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
    expect(server.calls("GET", "/me")).toHaveLength(1);
  });

  test("403 account_deleted и 503 service_unavailable при входе", async () => {
    const { server, api } = setup();
    server.inject("POST", "/auth/telegram", 403, { code: "account_deleted", message: "Аккаунт удалён" });
    expect((await caught(api.login())).code).toBe("account_deleted");
    server.inject("POST", "/auth/telegram", 503, { code: "service_unavailable", message: "Вход временно недоступен" });
    const e = await caught(api.login());
    expect(e.status).toBe(503);
    expect(e.message).toBe("Сервис временно недоступен. Попробуй чуть позже.");
  });
});

describe("Api — ошибки по контракту", () => {
  test("422 validation_error — текст на «ты», без эха отправленных данных", async () => {
    const { server, api } = setup();
    await api.login();
    server.inject("PUT", "/me/onboarding", 422, { code: "validation_error", message: "Проверьте данные" });
    const e = await caught(
      api.saveOnboarding({
        name: "Секретное имя",
        address_form: "x",
        partner_name: "y",
        consent_terms: true,
        consent_data: true,
        consent_sensitive: true,
        age_confirmed: true,
      }),
    );
    expect(e.status).toBe(422);
    expect(e.message).toMatch(/Проверь/);
    expect(e.message).not.toContain("Секретное имя");
  });

  test("429 — Retry-After в ошибке и в тексте", async () => {
    const { server, api } = setup();
    await api.login();
    server.inject("GET", "/me", 429, { code: "rate_limited", message: "лимит" }, { "Retry-After": "30" });
    const e = await caught(api.me());
    expect(e.status).toBe(429);
    expect(e.retryAfter).toBe(30);
    expect(e.message).toContain("30 с");
  });

  test("429 без тела ответа — всё равно rate_limited", async () => {
    const { server, api } = setup();
    await api.login();
    server.inject("GET", "/me", 429, undefined, { "Retry-After": "5" });
    const e = await caught(api.me());
    expect(e.code).toBe("rate_limited");
    expect(e.retryAfter).toBe(5);
  });

  test("500 без тела и 502 с HTML — общий понятный текст", async () => {
    const { server, api } = setup();
    await api.login();
    server.inject("GET", "/me", 500);
    expect((await caught(api.me())).message).toBe("Что-то пошло не так. Попробуй ещё раз.");
    server.inject("GET", "/me", 502, undefined, { "Content-Type": "text/html" });
    const e = await caught(api.me());
    expect(e.status).toBe(502);
    expect(e.code).toBe("internal");
  });

  test("обрыв связи — status 0, код network", async () => {
    const api = new Api({
      initData: () => INIT_DATA,
      fetch: async () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const e = await caught(api.login());
    expect(e.status).toBe(0);
    expect(e.code).toBe("network");
  });

  test("409 conflict_retry повторяется один раз (АР-24), второй — отдаётся", async () => {
    const { server, api } = setup();
    server.onboarded();
    await api.login();
    server.inject("POST", "/couple", 409, { code: "conflict_retry", message: "повторите" });
    const created = await api.createCouple();
    expect(created.link).toContain("startapp=");
    expect(server.calls("POST", "/couple")).toHaveLength(2);

    server.inject("POST", "/couple/invite", 409, { code: "conflict_retry", message: "повторите" }, {}, 2);
    expect((await caught(api.reissueInvite())).code).toBe("conflict_retry");
    expect(server.calls("POST", "/couple/invite")).toHaveLength(2);
  });

  test("GET /couple 404 — null, а не ошибка", async () => {
    const { server, api } = setup();
    server.onboarded();
    await api.login();
    expect(await api.couple()).toBeNull();
  });

  test("токен приглашения кодируется в пути", async () => {
    const { server, api } = setup();
    await api.login();
    await caught(api.invite("a/b?c"));
    expect(server.log.at(-1)!.path).toBe("/invites/a%2Fb%3Fc");
  });

  test("/help/contacts — публичный путь: без входа и без Authorization", async () => {
    const { server, api } = setup();
    const list = await api.helpContacts();
    expect(list[0].phone).toBe("112");
    expect(server.calls("GET", "/help/contacts")[0].auth).toBeNull();
  });

  test("запросы без cookie и без кэша", async () => {
    const seen: RequestInit[] = [];
    const server = new FakeServer();
    const api = new Api({
      initData: () => INIT_DATA,
      fetch: (i, init) => {
        seen.push(init ?? {});
        return server.fetch(i, init);
      },
    });
    await api.login();
    expect(seen[0].credentials).toBe("omit");
    expect(seen[0].cache).toBe("no-store");
  });

  // D5 (A8, р.2): 204 — успех только там, где его объявляет контракт; у путей 1.5 его нет.
  test("ответ 204 на пути, где контракт его не объявляет, — ApiError bad_response", async () => {
    const api = new Api({ initData: () => INIT_DATA, fetch: async () => json(204) });
    await expect(api.helpContacts()).rejects.toMatchObject({ status: 204, code: "bad_response" });
  });

  test("ответ 204 там, где контракт его объявляет (allowNoContent), — успех без чтения тела", async () => {
    let read = false;
    const res = new Response(null, { status: 204 });
    vi.spyOn(res, "text").mockImplementation(async () => {
      read = true;
      return "";
    });
    const api = new Api({ initData: () => INIT_DATA, fetch: async () => res });
    // Таких путей в 1.5 нет (например, POST /couple/leave — задача 2.x): флаг проверяется через закрытый вызов.
    const send = (api as unknown as { send: (m: string, p: string, o: object) => Promise<unknown> }).send.bind(api);
    await expect(send("GET", "/help/contacts", { public: true, op: "help", allowNoContent: true })).resolves.toBeUndefined();
    expect(read).toBe(false);
  });
});

describe("Api — срок ответа, вид ответа, общий вход (исправления 1.5, р.1)", () => {
  function hanging(onSignal?: (s: AbortSignal | null | undefined) => void): typeof fetch {
    return (_i, init) => {
      onSignal?.(init?.signal);
      return new Promise<Response>(() => undefined); // сигнал не слушает — срок всё равно должен сработать
    };
  }

  test("повисший вход — ApiError timeout через TIMEOUTS.login, запрос прерван сигналом", async () => {
    vi.useFakeTimers();
    try {
      let signal: AbortSignal | null | undefined;
      const api = new Api({ initData: () => INIT_DATA, fetch: hanging((s) => (signal = s)) });
      const p = caught(api.login());
      await vi.advanceTimersByTimeAsync(TIMEOUTS.login - 1);
      expect(signal?.aborted).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      const e = await p;
      expect(e).toMatchObject({ status: 0, code: "timeout" });
      expect(e.message).toBe("Сервер не ответил вовремя. Проверь интернет и попробуй ещё раз.");
      expect(signal?.aborted).toBe(true);
      expect(api.hasSession).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  test("/help/contacts ждёт недолго (TIMEOUTS.help), остальные пути — TIMEOUTS.default", async () => {
    expect(TIMEOUTS.help).toBeLessThan(TIMEOUTS.login);
    expect(TIMEOUTS.login).toBeLessThanOrEqual(TIMEOUTS.default);
    vi.useFakeTimers();
    try {
      const api = new Api({ initData: () => INIT_DATA, fetch: hanging() });
      const p = caught(api.helpContacts());
      await vi.advanceTimersByTimeAsync(TIMEOUTS.help);
      expect((await p).code).toBe("timeout");
    } finally {
      vi.useRealTimers();
    }
  });

  test("тело, повисшее после заголовков, тоже ограничено сроком", async () => {
    vi.useFakeTimers();
    try {
      const body = new ReadableStream({ start: () => undefined }); // заголовки пришли, тело — нет
      const api = new Api({ initData: () => INIT_DATA, fetch: async () => new Response(body, { status: 200 }) });
      const p = caught(api.login());
      await vi.advanceTimersByTimeAsync(TIMEOUTS.login);
      expect((await p).code).toBe("timeout");
    } finally {
      vi.useRealTimers();
    }
  });

  test("срок не срабатывает после ответа (таймер снят)", async () => {
    vi.useFakeTimers();
    try {
      const { api } = setup();
      await api.login();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  for (const [label, body] of [
    ["пустое тело", ""],
    ["HTML", "<html></html>"],
    ["null", "null"],
    ["объект не той схемы", '{"inviter_name":"Игорь"}'],
  ] as const) {
    test(`2xx «${label}» на GET /invites/{token} — ApiError bad_response`, async () => {
      const server = new FakeServer();
      const api = new Api({
        initData: () => INIT_DATA,
        fetch: (i, init) => (String(i).includes("/invites/") ? Promise.resolve(new Response(body, { status: 200 })) : server.fetch(i, init)),
      });
      await api.login();
      const e = await caught(api.invite("A".repeat(43)));
      expect(e).toMatchObject({ status: 200, code: "bad_response" });
      expect(e.message).toBe("Сервер ответил непонятно. Попробуй ещё раз чуть позже.");
    });
  }

  test("204 без тела на GET /me (контракт: только 200) — ApiError bad_response (D5)", async () => {
    const { server, api } = setup();
    await api.login();
    server.inject("GET", "/me", 204);
    await expect(api.me()).rejects.toMatchObject({ status: 204, code: "bad_response" });
  });

  test("m-2: одновременные 401 — один повторный вход на всех, каждый запрос повторён один раз", async () => {
    const { server, api } = setup();
    server.onboarded().pendingCouple();
    await api.login();
    server.expireSession();
    const results = await Promise.all([api.me(), api.couple(), api.me()]);
    expect(results[1]?.status).toBe("pending");
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
    expect(server.calls("GET", "/me")).toHaveLength(4);
    expect(server.calls("GET", "/couple")).toHaveLength(2);
  });

  test("m-2: вход уже идёт — защищённый запрос ждёт его, а не начинает свой", async () => {
    const { server, api } = setup();
    const login = api.login();
    const me = api.me();
    await Promise.all([login, me]);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(1);
  });

  test("m-1: тексты зависят от операции — инициатору не советуют «попросить партнёра»", async () => {
    const { server, api } = setup();
    server.onboarded();
    await api.login();
    const reissue = await caught(api.reissueInvite()); // пары нет — 404 not_found
    expect(reissue.code).toBe("not_found");
    expect(reissue.message).not.toMatch(/Попроси партн/);
    expect(reissue.message).toMatch(/Создай новую ссылку/);
    const invite = await caught(api.invite("Z".repeat(43)));
    expect(invite.message).toBe("Приглашение не найдено. Попроси партнёра прислать новую ссылку.");
    server.inject("GET", "/me", 404, { code: "not_found", message: "x" });
    expect((await caught(api.me())).message).not.toMatch(/Приглашение/);
  });

  for (const [seconds, text] of [
    ["45", "через 45 с."],
    ["60", "через 1 мин."],
    ["299", "через 5 мин."],
    ["3600", "через 1 ч."],
    ["86400", "через 24 ч."],
  ] as const) {
    test(`m-11: Retry-After ${seconds} — «${text}»`, async () => {
      const { server, api } = setup();
      server.onboarded();
      await api.login();
      server.inject("POST", "/couple", 429, { code: "rate_limited", message: "x" }, { "Retry-After": seconds });
      const e = await caught(api.createCouple());
      expect(e.retryAfter).toBe(Number(seconds));
      expect(e.message.endsWith(text)).toBe(true);
    });
  }
});
