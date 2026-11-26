/**
 * A8, задача 1.5, раунд 3 — строгие схемы ответов (n-4, D5, D7, D8) против контракта `api/openapi.yaml`.
 * Граничные корректные ответы (лишние поля — контракт их не запрещает; минимальный набор обязательных полей;
 * null там, где тип `[string, null]`; 201 у POST /couple) проходят на каждом пути. Мусор (не тот тип на
 * верхнем уровне и во вложенных полях) — `ApiError bad_response`, а не падение экрана.
 */
import { Api, ApiError } from "./client";
import { helpList } from "../screens/Help";
import { FakeServer, PARTNER_TOKEN } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";

const UUID = "00000000-0000-4000-8000-000000000001";
const EXPIRES = "2026-12-03T12:00:00Z";
const ME_MIN = { id: UUID, onboarding_completed: true, has_active_couple: false };
const EXTRA = { future_field: { nested: [1, 2] }, another: null };

async function loggedIn() {
  const server = new FakeServer().onboarded();
  const api = new Api({ initData: () => INIT_DATA, fetch: (i, init) => server.fetch(i, init) });
  await api.login();
  return { server, api };
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

type Path = { label: string; method: string; path: string; status: number; call: (api: Api) => Promise<unknown>; ok: unknown[] };

const PATHS: Path[] = [
  {
    label: "GET /me",
    method: "GET",
    path: "/me",
    status: 200,
    call: (a) => a.me(),
    ok: [ME_MIN, { ...ME_MIN, ...EXTRA, name: "", address_form: "", partner_name: "", timezone: "" }],
  },
  {
    label: "PUT /me/onboarding",
    method: "PUT",
    path: "/me/onboarding",
    status: 200,
    call: (a) =>
      a.saveOnboarding({
        name: "Аня",
        address_form: "Аня",
        partner_name: "Игорь",
        timezone: "UTC",
        consent_terms: true,
        consent_data: true,
        consent_sensitive: true,
        age_confirmed: true,
      }),
    ok: [ME_MIN, { ...ME_MIN, ...EXTRA, name: null }],
  },
  {
    label: "GET /couple",
    method: "GET",
    path: "/couple",
    status: 200,
    call: (a) => a.couple(),
    ok: [
      { id: UUID, status: "pending", members: [] },
      { id: UUID, status: "dissolved", members: [{ role: "initiator", is_me: true }], ...EXTRA },
      { id: UUID, status: "active", members: [{ role: "initiator", is_me: true, name: null, x: 1 }, { role: "partner", is_me: false, name: "Игорь" }] },
    ],
  },
  {
    label: "POST /couple",
    method: "POST",
    path: "/couple",
    status: 201,
    call: (a) => a.createCouple(),
    ok: [{ couple_id: UUID, link: "https://t.me/bot?startapp=" + "A".repeat(43), expires_at: EXPIRES, ...EXTRA }],
  },
  {
    label: "POST /couple/invite",
    method: "POST",
    path: "/couple/invite",
    status: 201,
    call: (a) => a.reissueInvite(),
    ok: [{ couple_id: UUID, link: "https://t.me/bot?startapp=" + "B".repeat(43), expires_at: EXPIRES }],
  },
  {
    label: "GET /invites/{token}",
    method: "GET",
    path: `/invites/${PARTNER_TOKEN}`,
    status: 200,
    call: (a) => a.invite(PARTNER_TOKEN),
    ok: [{ inviter_name: null, expires_at: EXPIRES }, { inviter_name: "", expires_at: EXPIRES, ...EXTRA }],
  },
  {
    label: "POST /invites/{token}/accept",
    method: "POST",
    path: `/invites/${PARTNER_TOKEN}/accept`,
    status: 200,
    call: (a) => a.acceptInvite(PARTNER_TOKEN),
    ok: [{ id: UUID, status: "active", members: [{ role: "partner", is_me: true }] }],
  },
  {
    label: "GET /help/contacts",
    method: "GET",
    path: "/help/contacts",
    status: 200,
    call: (a) => a.helpContacts(),
    ok: [[], [{ name: "112", phone: "112", description: "", ...EXTRA }]],
  },
];

const JUNK: [string, unknown][] = [
  ["null", null],
  ["массив вместо объекта", [{}]],
  ["строка", "ok"],
  ["число", 0],
  ["true", true],
];

describe("A8 р.3 · граничные корректные ответы проходят на каждом пути", () => {
  for (const p of PATHS) {
    p.ok.forEach((body, i) => {
      test(`${p.label} ${p.status} #${i + 1} — успех, тело без изменений`, async () => {
        const { server, api } = await loggedIn();
        server.inject(p.method, p.path, p.status, body);
        await expect(p.call(api)).resolves.toEqual(body);
      });
    });
  }

  test("POST /auth/telegram: Session с лишними полями и Me с минимумом — вход", async () => {
    const server = new FakeServer();
    server.inject("POST", "/auth/telegram", 200, { access_token: "t", expires_in: 1, me: ME_MIN, refresh: "x" });
    const api = new Api({ initData: () => INIT_DATA, fetch: server.fetch });
    await expect(api.login()).resolves.toEqual(ME_MIN);
    expect(api.hasSession).toBe(true);
  });
});

describe("A8 р.3 · мусор на верхнем уровне — bad_response на каждом пути", () => {
  for (const p of PATHS) {
    for (const [label, body] of JUNK) {
      if (p.path === "/help/contacts" && Array.isArray(body)) continue; // массив — верный вид списка
      test(`${p.label}: ${label} — bad_response`, async () => {
        const { server, api } = await loggedIn();
        server.inject(p.method, p.path, p.status, body);
        expect(await caught(p.call(api))).toMatchObject({ code: "bad_response" });
      });
    }
  }
});

describe("A8 р.3 · мусор во вложенных полях (поля, которые читают экраны)", () => {
  const NESTED: [string, Path["label"], unknown][] = [
    ['Me.onboarding_completed = "false" (строка истинна в JS)', "GET /me", { ...ME_MIN, onboarding_completed: "false" }],
    ["Me.has_active_couple = null", "PUT /me/onboarding", { ...ME_MIN, has_active_couple: null }],
    ["Me.address_form массивом", "GET /me", { ...ME_MIN, address_form: ["Аня"] }],
    ['Couple.status = "PENDING" (не из перечня)', "GET /couple", { id: UUID, status: "PENDING", members: [] }],
    ["Couple.members объектом", "GET /couple", { id: UUID, status: "pending", members: {} }],
    ["Couple: участник null", "POST /invites/{token}/accept", { id: UUID, status: "active", members: [null] }],
    ['Couple: is_me = "true"', "GET /couple", { id: UUID, status: "active", members: [{ role: "partner", is_me: "true" }] }],
    ["Couple: три участника (maxItems 2)", "GET /couple", { id: UUID, status: "active", members: Array(3).fill({ role: "partner", is_me: false }) }],
    ["InviteCreated.link числом", "POST /couple", { couple_id: UUID, link: 1, expires_at: EXPIRES }],
    ["InviteCreated без expires_at", "POST /couple/invite", { couple_id: UUID, link: "https://t.me/x" }],
    ["InvitePreview без inviter_name (обязателен)", "GET /invites/{token}", { expires_at: EXPIRES }],
    ["InvitePreview.inviter_name массивом", "GET /invites/{token}", { inviter_name: ["Игорь"], expires_at: EXPIRES }],
    ["InvitePreview.expires_at = null", "GET /invites/{token}", { inviter_name: "Игорь", expires_at: null }],
  ];
  for (const [label, pathLabel, body] of NESTED) {
    test(`${pathLabel}: ${label} — bad_response`, async () => {
      const p = PATHS.find((x) => x.label === pathLabel)!;
      const { server, api } = await loggedIn();
      server.inject(p.method, p.path, p.status, body);
      expect(await caught(p.call(api))).toMatchObject({ code: "bad_response" });
    });
  }

  test("session.me без has_active_couple — bad_response, токен не сохранён", async () => {
    const server = new FakeServer();
    server.inject("POST", "/auth/telegram", 200, { access_token: "t", expires_in: 1, me: { id: UUID, onboarding_completed: true } });
    const api = new Api({ initData: () => INIT_DATA, fetch: server.fetch });
    expect(await caught(api.login())).toMatchObject({ code: "bad_response" });
    expect(api.hasSession).toBe(false);
  });

  test("HelpContact: мусорные записи отброшены, верные сохранены по порядку, 112 ссылкой в конце", () => {
    const good = { name: "Телефон доверия", phone: "8 (800) 2000-122", description: "Бесплатно" };
    const list = helpList([
      null,
      "112",
      { name: "A", phone: 112, description: "x" },
      { name: "B", phone: "   ", description: "x" },
      { name: "C", phone: "---", description: "x" },
      { name: "D", phone: "112", description: "x", round_the_clock: "да" },
      { name: ["E"], phone: "112", description: "x" },
      good,
      good,
    ]);
    expect(list.map((c) => c.name)).toEqual(["Телефон доверия", "Единый номер экстренных служб"]);
    expect(list[1].phone).toBe("112");
  });
});
