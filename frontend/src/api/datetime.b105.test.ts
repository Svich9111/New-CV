/**
 * Б-105 (A8-0021-3): `format: date-time` контракта (`api/openapi.yaml`) — строгая RFC 3339 из общего модуля
 * `datetime.ts` и в админ-клиенте (`KbDocument.created_at`, `indexed_at`), и у приглашений Mini App
 * (`InviteCreated.expires_at`, `InvitePreview.expires_at`). Раньше: `Date.parse` (принимал «Nov 5», «1») и просто строка.
 */
import { toKbDocument } from "../admin/api";
import { Api, ApiError } from "./client";
import { FakeServer, PARTNER_TOKEN } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";

const UUID = "00000000-0000-4000-8000-000000000001";
const OK_DT = ["2026-12-03T12:00:00Z", "2026-12-03T12:00:00.123+03:00", "2026-12-31T23:59:60Z"];
const BAD_DT = ["Nov 5", "1", "2026", "2026-12-03", "2026-12-03T12:00:00", "2026-02-30T12:00:00Z", " 2026-12-03T12:00:00Z", ""];
const BAD_TYPE: unknown[] = [null, 0, {}, ["2026-12-03T12:00:00Z"]];

describe("Б-105 · toKbDocument: created_at и indexed_at — строгая date-time", () => {
  const doc = {
    id: "d0000000-0000-4000-8000-000000000001",
    title: "Выдуманная книга",
    rights_basis: "license",
    index_status: "indexed",
    created_at: "2026-11-20T10:00:00Z",
  };
  test.each(OK_DT)("created_at %s — принимается", (created_at) => expect(toKbDocument({ ...doc, created_at })).not.toBeNull());
  test.each([...BAD_DT, ...BAD_TYPE, undefined])("created_at %j — отказ", (created_at) =>
    expect(toKbDocument({ ...doc, created_at })).toBeNull(),
  );
  test.each([...OK_DT, null, undefined])("indexed_at %j — принимается", (indexed_at) =>
    expect(toKbDocument({ ...doc, indexed_at })).not.toBeNull(),
  );
  test.each([...BAD_DT, 0, {}])("indexed_at %j — отказ", (indexed_at) => expect(toKbDocument({ ...doc, indexed_at })).toBeNull());
});

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
  throw new Error("ожидалась ApiError, получен успех");
}

type Case = { label: string; method: string; path: string; status: number; call: (a: Api) => Promise<unknown>; body: (e: unknown) => unknown };
const LINK = "https://t.me/bot?startapp=" + "A".repeat(43);
const CASES: Case[] = [
  { label: "POST /couple", method: "POST", path: "/couple", status: 201, call: (a) => a.createCouple(), body: (expires_at) => ({ couple_id: UUID, link: LINK, expires_at }) },
  { label: "POST /couple/invite", method: "POST", path: "/couple/invite", status: 201, call: (a) => a.reissueInvite(), body: (expires_at) => ({ couple_id: UUID, link: LINK, expires_at }) },
  {
    label: "GET /invites/{token}",
    method: "GET",
    path: `/invites/${PARTNER_TOKEN}`,
    status: 200,
    call: (a) => a.invite(PARTNER_TOKEN),
    body: (expires_at) => ({ inviter_name: "Игорь", expires_at }),
  },
];

describe("Б-105 · приглашения: expires_at — строгая date-time", () => {
  for (const c of CASES) {
    test.each(OK_DT)(`${c.label}: expires_at %s — успех`, async (expires_at) => {
      const { server, api } = await loggedIn();
      server.inject(c.method, c.path, c.status, c.body(expires_at));
      await expect(c.call(api)).resolves.toEqual(c.body(expires_at));
    });
    test.each([...BAD_DT, ...BAD_TYPE])(`${c.label}: expires_at %j — bad_response`, async (expires_at) => {
      const { server, api } = await loggedIn();
      server.inject(c.method, c.path, c.status, c.body(expires_at));
      expect(await caught(c.call(api))).toMatchObject({ code: "bad_response" });
    });
  }
});
