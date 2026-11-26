/**
 * A8, задача 1.5 — негативные сценарии API-клиента (ФТ-01, ТЗ §9.1 п.5, контракт api/openapi.yaml).
 * Дефекты помечены `A8-1.5-Dn` и записаны через `it.fails`: тест зеленеет, пока дефект есть, и
 * краснеет после исправления — тогда `it.fails` заменяется на `it`.
 */
import { Api, ApiError } from "./client";
import { FakeServer, json } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";

function apiFor(server: FakeServer, initData = INIT_DATA): Api {
  return new Api({ initData: () => initData, fetch: (i, init) => server.fetch(i, init) });
}

async function caught(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p;
  } catch (e) {
    if (e instanceof ApiError) return e;
    throw e;
  }
  throw new Error("ожидалась ApiError");
}

describe("A8 · приватность токена и initData (ТЗ §9.1 п.5, карточка «Приватность»)", () => {
  test("после входа и запросов токена и initData нет ни в хранилищах, ни в cookie, ни в адресе", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    await api.me();
    await api.couple();
    const token = server.token!;
    const leaks = [
      JSON.stringify({ ...localStorage }),
      JSON.stringify({ ...sessionStorage }),
      document.cookie,
      window.location.href,
      document.body.innerHTML,
    ].join("\n");
    expect(leaks).not.toContain(token);
    expect(leaks).not.toContain("hash=fake");
    expect(leaks).not.toContain(INIT_DATA);
  });

  test("initData уходит только в POST /auth/telegram, в других запросах его нет", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    await api.me();
    await api.helpContacts();
    for (const l of server.log.filter((x) => x.path !== "/auth/telegram")) {
      expect(JSON.stringify(l)).not.toContain("hash=fake");
    }
  });

  test("ошибка не содержит initData и токен в тексте", async () => {
    const server = new FakeServer().inject("POST", "/auth/telegram", 401, { code: "invalid_init_data", message: INIT_DATA });
    const e = await caught(apiFor(server).login());
    expect(e.message).not.toContain("hash=fake");
  });
});

describe("A8 · 401 без бесконечного цикла (карточка «Вход по контракту»)", () => {
  test("защищённый путь всегда 401: один повторный вход и один повтор, затем ошибка", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    server.inject("GET", "/me", 401, { code: "unauthorized", message: "x" }, undefined, 100);
    const e = await caught(api.me());
    expect(e.status).toBe(401);
    expect(server.calls("GET", "/me")).toHaveLength(2);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
  });

  test("повторный вход отвечает 401 — запрос не повторяется, вход не зацикливается", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    server.inject("GET", "/couple", 401, { code: "unauthorized", message: "x" });
    server.inject("POST", "/auth/telegram", 401, { code: "invalid_init_data", message: "x" }, undefined, 100);
    const e = await caught(api.couple());
    expect(e.code).toBe("invalid_init_data");
    expect(server.calls("GET", "/couple")).toHaveLength(1);
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(2);
    expect(api.hasSession).toBe(false);
  });

  test("401 + conflict_retry вместе: не больше 4 запросов к пути и 2 входов", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    let n = 0;
    const base = server.fetch;
    server.fetch = async (input, init) => {
      const url = String(input);
      if (url.endsWith("/couple") && (init?.method ?? "GET") === "POST") {
        n++;
        return n % 2 ? json(401, { code: "unauthorized", message: "x" }) : json(409, { code: "conflict_retry", message: "x" });
      }
      return base(input, init);
    };
    await caught(api.createCouple());
    expect(n).toBeLessThanOrEqual(4);
    expect(server.calls("POST", "/auth/telegram").length).toBeLessThanOrEqual(2);
  });

  test("публичный /help/contacts с 401 не вызывает вход", async () => {
    const server = new FakeServer().inject("GET", "/help/contacts", 401, { code: "unauthorized", message: "x" });
    await caught(apiFor(server).helpContacts());
    expect(server.calls("POST", "/auth/telegram")).toHaveLength(0);
  });

  test("пустой initData — вход отправляет пустую строку как есть, сервер отвечает 401", async () => {
    const server = new FakeServer();
    const e = await caught(apiFor(server, "").login());
    expect(server.calls("POST", "/auth/telegram")[0].body).toEqual({ init_data: "" });
    expect(e.status).toBe(401);
  });
});

describe("A8 · 429 и Retry-After", () => {
  test("Retry-After: 30 и тело rate_limited — 30 с в тексте", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    server.inject("POST", "/couple", 429, { code: "rate_limited", message: "x" }, { "Retry-After": "30" });
    const e = await caught(api.createCouple());
    expect(e).toMatchObject({ status: 429, code: "rate_limited", retryAfter: 30 });
    expect(e.message).toMatch(/через 30 с\.$/);
  });

  for (const h of ["0", "-5", "abc", "Wed, 21 Oct 2026 07:28:00 GMT", ""]) {
    test(`Retry-After «${h}» — без числа в тексте, без NaN`, async () => {
      const server = new FakeServer().onboarded();
      const api = apiFor(server);
      await api.login();
      server.inject("POST", "/couple", 429, { code: "rate_limited", message: "x" }, { "Retry-After": h });
      const e = await caught(api.createCouple());
      expect(e.retryAfter).toBeUndefined();
      expect(e.message).not.toMatch(/NaN|undefined|через/);
      expect(e.message).toMatch(/Слишком много запросов/);
    });
  }

  test("429 без Retry-After и без тела — rate_limited, без числа", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    server.inject("POST", "/couple", 429);
    const e = await caught(api.createCouple());
    expect(e).toMatchObject({ status: 429, code: "rate_limited" });
    expect(e.retryAfter).toBeUndefined();
    expect(e.message).not.toMatch(/через/);
  });

  test("429 не повторяется автоматически", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    server.inject("POST", "/couple", 429, undefined, { "Retry-After": "1" }, 5);
    await caught(api.createCouple());
    expect(server.calls("POST", "/couple")).toHaveLength(1);
  });
});

describe("A8 · 5xx, обрыв, таймаут", () => {
  for (const status of [500, 502, 503, 504]) {
    test(`${status} без тела — internal, понятный текст на «ты»`, async () => {
      const server = new FakeServer().onboarded();
      const api = apiFor(server);
      await api.login();
      server.fetch = async () => new Response("<html><script>alert(1)</script></html>", { status, headers: { "Content-Type": "text/html" } });
      const e = await caught(api.me());
      expect(e.status).toBe(status);
      expect(e.code).toBe("internal");
      expect(e.message).toBe("Что-то пошло не так. Попробуй ещё раз.");
    });
  }

  test("тело ошибки с HTML/скриптом в message и незнакомым code — текст как есть (React экранирует), без исполнения", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    server.inject("GET", "/me", 500, { code: "weird_code", message: "<img src=x onerror=alert(1)>" });
    const e = await caught(api.me());
    expect(e.message).toBe("<img src=x onerror=alert(1)>");
  });

  test("обрыв связи (fetch бросает) — status 0, network; без повтора входа", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    const base = server.fetch;
    let calls = 0;
    server.fetch = async (i, init) => {
      if (String(i).endsWith("/me")) {
        calls++;
        throw new TypeError("Failed to fetch");
      }
      return base(i, init);
    };
    const e = await caught(api.me());
    expect(e).toMatchObject({ status: 0, code: "network" });
    expect(calls).toBe(1);
  });

  test("AbortError (запрос прерван) — тоже network", async () => {
    const server = new FakeServer();
    server.fetch = async () => {
      throw new DOMException("aborted", "AbortError");
    };
    const e = await caught(apiFor(server).login());
    expect(e.code).toBe("network");
  });

  // A8-1.5-D1: у запросов нет таймаута (нет AbortSignal/AbortController). Повисший запрос оставляет
  // экран «Входим через Telegram…» без причины и без «Попробовать снова» (карточка: «обрыв связи при
  // входе — причина подписью, повтор»). Ожидается: через разумное время — ApiError network/timeout.
  it("A8-1.5-D1: повисший запрос завершается ошибкой по таймауту", async () => {
    vi.useFakeTimers();
    try {
      let signal: AbortSignal | null | undefined;
      const api = new Api({
        initData: () => INIT_DATA,
        fetch: (_i, init) => {
          signal = init?.signal;
          return new Promise<Response>((_res, rej) => {
            init?.signal?.addEventListener("abort", () => rej(new DOMException("timeout", "AbortError")));
          });
        },
      });
      const p = caught(api.login());
      await vi.advanceTimersByTimeAsync(120_000);
      expect(signal).toBeTruthy();
      const e = await p;
      expect(e.status).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("A8 · успешный ответ не того вида (2xx без JSON)", () => {
  // A8-1.5-D2: на 2xx клиент возвращает `null`, если тело не JSON (прокси/заглушка отдали HTML с 200).
  // Последствия в интерфейсе: «Помощь» навсегда остаётся на «Загружаем контакты…» без запасного 112
  // (Help.tsx: `list.length` на null — исключение внутри then, отклонение никто не ловит), экран
  // входящего приглашения падает при отрисовке (`state.invite.inviter_name`), `GET /couple` → «пары нет».
  // Ожидается: 2xx с телом не по схеме — ApiError (ТЗ §9.3 «помощь доступна всегда»).
  it("A8-1.5-D2: GET /help/contacts 200 text/html — ошибка, а не null", async () => {
    const server = new FakeServer();
    server.fetch = async () => new Response("<!doctype html><html></html>", { status: 200, headers: { "Content-Type": "text/html" } });
    await expect(apiFor(server).helpContacts()).rejects.toBeInstanceOf(ApiError);
  });

  it("A8-1.5-D2: POST /auth/telegram 200 text/html — ApiError, а не TypeError", async () => {
    const server = new FakeServer();
    server.fetch = async () => new Response("<html></html>", { status: 200 });
    await expect(apiFor(server).login()).rejects.toBeInstanceOf(ApiError);
  });

  // Факт по D2 обращён после исправления (A4, р.1): раньше здесь фиксировалось `null`.
  test("факт по D2: 200 с HTML на /help/contacts — ApiError bad_response, а не null", async () => {
    const server = new FakeServer();
    server.fetch = async () => new Response("<html></html>", { status: 200, headers: { "Content-Type": "text/html" } });
    const e = await caught(apiFor(server).helpContacts());
    expect(e).toMatchObject({ status: 200, code: "bad_response" });
  });
});

describe("A8 · параметры запроса", () => {
  test("каждый запрос: credentials omit, cache no-store; Authorization только на защищённых путях", async () => {
    const inits: { url: string; init: RequestInit }[] = [];
    const server = new FakeServer().onboarded();
    const base = server.fetch;
    server.fetch = async (i, init = {}) => {
      inits.push({ url: String(i), init });
      return base(i, init);
    };
    const api = apiFor(server);
    await api.login();
    await api.me();
    await api.couple();
    await api.helpContacts();
    await caught(api.invite("A".repeat(43)));
    for (const { url, init } of inits) {
      expect(init.credentials).toBe("omit");
      expect(init.cache).toBe("no-store");
      const auth = new Headers(init.headers).get("Authorization");
      if (url.endsWith("/auth/telegram") || url.endsWith("/help/contacts")) expect(auth).toBeNull();
      else expect(auth).toMatch(/^Bearer test-session-/);
    }
  });

  test("токен приглашения с кавычками/слэшами кодируется и не меняет путь", async () => {
    const server = new FakeServer().onboarded();
    const api = apiFor(server);
    await api.login();
    await caught(api.invite(`../couple"?x=1#`));
    const last = server.log[server.log.length - 1];
    expect(last.path).toMatch(/^\/invites\/[^/]+$/);
    expect(last.path).not.toContain("/couple");
  });
});
