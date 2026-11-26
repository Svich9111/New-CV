/**
 * A4, задача 1.5, раунд 3 — строгая проверка вида успешных ответов по `api/openapi.yaml` (n-4, D5, D7, D8 из
 * docs/reviews/1.5-review-r2.md и 1.5-test-r2.md). Позитивные случаи — граничные корректные ответы контракта:
 * ужесточение не должно отбросить ни одного из них.
 */
import { Api, ApiError, isHelpContact } from "./client";
import { FakeServer, PARTNER_TOKEN } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";

const UUID = "00000000-0000-4000-8000-000000000001";
const EXPIRES = "2026-12-03T12:00:00Z";
const ME_MIN = { id: UUID, onboarding_completed: false, has_active_couple: false };

function apiFor(server: FakeServer): Api {
  return new Api({ initData: () => INIT_DATA, fetch: (i, init) => server.fetch(i, init) });
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

async function loggedIn(server = new FakeServer().onboarded()): Promise<{ server: FakeServer; api: Api }> {
  const api = apiFor(server);
  await api.login();
  return { server, api };
}

describe("n-4: Session и Me — по контракту (первый экран выбирается по двум булевым полям)", () => {
  test("граница: Me только с обязательными полями и Session с ним — вход успешен", async () => {
    const server = new FakeServer();
    server.inject("POST", "/auth/telegram", 200, { access_token: "t", expires_in: 3600, me: ME_MIN });
    await expect(apiFor(server).login()).resolves.toEqual(ME_MIN);
  });

  test("граница: name/address_form/partner_name = null, timezone строкой — успех", async () => {
    const me = { ...ME_MIN, name: null, address_form: null, partner_name: null, timezone: "Europe/Moscow" };
    const { server, api } = await loggedIn();
    server.inject("GET", "/me", 200, me);
    await expect(api.me()).resolves.toEqual(me);
  });

  const BAD_ME: [string, unknown][] = [
    ["пустой объект", {}],
    ["без onboarding_completed", { id: UUID, has_active_couple: false }],
    ["без has_active_couple", { id: UUID, onboarding_completed: true }],
    ["onboarding_completed строкой", { ...ME_MIN, onboarding_completed: "true" }],
    ["has_active_couple числом", { ...ME_MIN, has_active_couple: 1 }],
    ["id числом", { ...ME_MIN, id: 1 }],
    ["name объектом", { ...ME_MIN, name: { x: 1 } }],
    ["partner_name числом", { ...ME_MIN, partner_name: 5 }],
    ["timezone = null (контракт: string)", { ...ME_MIN, timezone: null }],
  ];

  for (const [label, me] of BAD_ME) {
    test(`вход: session.me ${label} — bad_response`, async () => {
      const server = new FakeServer();
      server.inject("POST", "/auth/telegram", 200, { access_token: "t", expires_in: 3600, me });
      expect(await caught(apiFor(server).login())).toMatchObject({ status: 200, code: "bad_response" });
    });

    test(`GET /me и PUT /me/onboarding: ${label} — bad_response`, async () => {
      const { server, api } = await loggedIn();
      server.inject("GET", "/me", 200, me);
      expect((await caught(api.me())).code).toBe("bad_response");
      server.inject("PUT", "/me/onboarding", 200, me);
      const req = { name: "А", address_form: "А", partner_name: "И", consent_terms: true, consent_data: true, consent_sensitive: true, age_confirmed: true } as const;
      expect((await caught(api.saveOnboarding(req))).code).toBe("bad_response");
    });
  }

  test("вход: пустой access_token — bad_response", async () => {
    const server = new FakeServer();
    server.inject("POST", "/auth/telegram", 200, { access_token: "", expires_in: 3600, me: ME_MIN });
    expect((await caught(apiFor(server).login())).code).toBe("bad_response");
  });
});

describe("n-4: Couple — статус из перечня, участники поэлементно", () => {
  const OK: [string, unknown][] = [
    ["pending, members: []", { id: UUID, status: "pending", members: [] }],
    ["dissolved", { id: UUID, status: "dissolved", members: [{ role: "initiator", name: "Аня", is_me: true }] }],
    [
      "active, имя участника null и без поля name",
      { id: UUID, status: "active", members: [{ role: "initiator", name: null, is_me: true }, { role: "partner", is_me: false }] },
    ],
  ];
  for (const [label, couple] of OK) {
    test(`граница: GET /couple ${label} — успех; POST /invites/…/accept тоже`, async () => {
      const { server, api } = await loggedIn();
      server.inject("GET", "/couple", 200, couple);
      await expect(api.couple()).resolves.toEqual(couple);
      server.inject("POST", `/invites/${PARTNER_TOKEN}/accept`, 200, couple);
      await expect(api.acceptInvite(PARTNER_TOKEN)).resolves.toEqual(couple);
    });
  }

  const BAD: [string, unknown][] = [
    ["без id", { status: "pending", members: [] }],
    ["неизвестный статус", { id: UUID, status: "waiting", members: [] }],
    ["участников три", { id: UUID, status: "active", members: Array(3).fill({ role: "partner", is_me: false }) }],
    ["участник без is_me", { id: UUID, status: "pending", members: [{ role: "initiator" }] }],
    ["роль не из перечня", { id: UUID, status: "pending", members: [{ role: "owner", is_me: true }] }],
    ["имя участника объектом", { id: UUID, status: "pending", members: [{ role: "initiator", is_me: true, name: {} }] }],
    ["участник null", { id: UUID, status: "pending", members: [null] }],
  ];
  for (const [label, couple] of BAD) {
    test(`GET /couple и accept: ${label} — bad_response`, async () => {
      const { server, api } = await loggedIn();
      server.inject("GET", "/couple", 200, couple);
      expect((await caught(api.couple())).code).toBe("bad_response");
      server.inject("POST", `/invites/${PARTNER_TOKEN}/accept`, 200, couple);
      expect((await caught(api.acceptInvite(PARTNER_TOKEN))).code).toBe("bad_response");
    });
  }
});

describe("InviteCreated и InvitePreview (D8)", () => {
  test("граница: POST /couple 201 и POST /couple/invite 201 по контракту — успех", async () => {
    const created = { couple_id: UUID, link: "https://t.me/test_bot?startapp=x", expires_at: EXPIRES };
    const { server, api } = await loggedIn();
    server.inject("POST", "/couple", 201, created);
    await expect(api.createCouple()).resolves.toEqual(created);
    server.inject("POST", "/couple/invite", 201, created);
    await expect(api.reissueInvite()).resolves.toEqual(created);
  });

  test("InviteCreated без couple_id — bad_response", async () => {
    const { server, api } = await loggedIn();
    server.inject("POST", "/couple", 201, { link: "https://t.me/x", expires_at: EXPIRES });
    expect((await caught(api.createCouple())).code).toBe("bad_response");
  });

  test("граница: inviter_name = null — успех", async () => {
    const { server, api } = await loggedIn();
    server.inject("GET", `/invites/${PARTNER_TOKEN}`, 200, { inviter_name: null, expires_at: EXPIRES });
    await expect(api.invite(PARTNER_TOKEN)).resolves.toEqual({ inviter_name: null, expires_at: EXPIRES });
  });

  for (const [label, body] of [
    ["inviter_name объектом", { inviter_name: { x: 1 }, expires_at: EXPIRES }],
    ["inviter_name числом", { inviter_name: 5, expires_at: EXPIRES }],
    ["без inviter_name (обязательное поле)", { expires_at: EXPIRES }],
    ["expires_at числом", { inviter_name: "Игорь", expires_at: 1 }],
  ] as const) {
    test(`GET /invites/{token}: ${label} — bad_response`, async () => {
      const { server, api } = await loggedIn();
      server.inject("GET", `/invites/${PARTNER_TOKEN}`, 200, body);
      expect((await caught(api.invite(PARTNER_TOKEN))).code).toBe("bad_response");
    });
  }
});

describe("D7: HelpContact — запись по схеме контракта", () => {
  test("граница: без round_the_clock — запись годится", () => {
    expect(isHelpContact({ name: "Линия", phone: "8-800-000-00-00", description: "Описание." })).toBe(true);
  });

  for (const [label, c] of [
    ["description объектом", { name: "A", phone: "112", description: { x: 1 } }],
    ["без description", { name: "A", phone: "112" }],
    ["round_the_clock строкой", { name: "A", phone: "112", description: "x", round_the_clock: "да" }],
    ["phone из пробелов", { name: "A", phone: "  ", description: "x" }],
    ["name числом", { name: 1, phone: "112", description: "x" }],
    ["массив", ["A", "112", "x"]],
  ] as const) {
    test(`${label} — запись отбрасывается`, () => {
      expect(isHelpContact(c)).toBe(false);
    });
  }
});
