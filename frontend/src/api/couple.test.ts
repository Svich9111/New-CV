/**
 * A4, задача 3.4 — черновики и лента пары по контракту: строгие проверки `Draft`, `DraftCrisis`, `DraftOutcome`
 * (по `kind`), `PartnerMessage` (статусы, причины, `withheld_crisis` только при `safety`), страницы ленты; методы
 * `Api` на заглушке сервера (статус ответа и `kind` должны совпасть, `send` — только 202/`processing`).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Api, ApiError, isHelpContact } from "./client";
import {
  MESSAGE_STATUSES,
  WITHHELD_CRISES,
  WITHHELD_REASONS,
  isCoupleFeed,
  isDraft,
  isDraftCrisis,
  isMessageOriginal,
  isPartnerMessage,
  parseDraftOutcome,
} from "./couple";
import { CRISIS_BLOCKS, crisisBlocks } from "../couple/crisis";
import { CONTACTS, FakeServer } from "../test/fakeApi";
import { INIT_DATA } from "../test/fakeTelegram";

const ID = "d0000000-0000-4000-8000-000000000001";
const MID = "e0000000-0000-4000-8000-000000000001";
const draft = { kind: "draft", id: ID, text: "Текст", version: 1, status: "draft", reply_to_id: null };
const msg = {
  id: MID,
  direction: "outgoing",
  text: "Моё",
  is_adapted: false,
  harsh_tone_warning: false,
  status: "delivered",
  withheld_reason: null,
  withheld_crisis: null,
  reply_to_id: null,
  sent_at: "2026-11-25T19:00:00Z",
  read_at: null,
};
const strip = (v: Record<string, unknown>) => {
  for (const k of Object.keys(v)) if (v[k] === undefined) delete v[k];
  return v;
};

describe("isDraft", () => {
  const draftOk: [string, Record<string, unknown>][] = [
    ["базовый", {}],
    ["без reply_to_id", { reply_to_id: undefined }],
    ["ответ — uuid", { reply_to_id: MID }],
    ["пустой текст — строка", { text: "" }],
    ...["draft", "approved", "rejected", "blocked", "sent"].map((s): [string, Record<string, unknown>] => [`статус ${s}`, { status: s }]),
  ];
  test.each(draftOk)("корректный: %s", (_n, patch) => expect(isDraft(strip({ ...draft, ...patch }))).toBe(true));

  test.each([
    ["kind crisis", { kind: "crisis" }],
    ["без kind", { kind: undefined }],
    ["id не uuid", { id: "1" }],
    ["текст объектом", { text: { unavailable: true, reason: "auth" } }],
    ["версия 0", { version: 0 }],
    ["версия дробная", { version: 1.5 }],
    ["версия строкой", { version: "1" }],
    ["статус не из перечня", { status: "DRAFT" }],
    ["reply_to_id не uuid", { reply_to_id: "x" }],
  ])("неверный: %s", (_n, patch) => expect(isDraft(strip({ ...draft, ...patch }))).toBe(false));
});

describe("isDraftCrisis и parseDraftOutcome (дискриминатор kind)", () => {
  const crisis = { kind: "crisis", text: "Протокол", contacts: CONTACTS, draft_id: null };
  test("строго: корректный, draft_id uuid/нет поля", () => {
    expect(isDraftCrisis(crisis)).toBe(true);
    expect(isDraftCrisis({ ...crisis, draft_id: ID })).toBe(true);
    expect(isDraftCrisis(strip({ ...crisis, draft_id: undefined }))).toBe(true);
  });
  test.each([
    ["пустой текст", { text: "" }],
    ["нет контактов", { contacts: [] }],
    ["лишнее поле", { extra: 1 }],
    ["draft_id не uuid", { draft_id: "x" }],
  ])("строго неверный: %s", (_n, patch) => expect(isDraftCrisis({ ...crisis, ...patch })).toBe(false));

  test("draft — только строго по схеме; crisis — при любом виде (помощь всегда, §9.3)", () => {
    expect(parseDraftOutcome(draft, isHelpContact)).toEqual({ kind: "draft", draft });
    expect(parseDraftOutcome({ ...draft, version: 0 }, isHelpContact)).toBeNull();
    expect(parseDraftOutcome({ kind: "other" }, isHelpContact)).toBeNull();
    expect(parseDraftOutcome(null, isHelpContact)).toBeNull();
    expect(parseDraftOutcome({ kind: "crisis" }, isHelpContact)).toEqual({ kind: "crisis", text: null, contacts: [], draftId: null });
    expect(parseDraftOutcome({ ...crisis, contacts: [{ bad: 1 }, CONTACTS[0]], draft_id: ID, extra: 1 }, isHelpContact)).toEqual({
      kind: "crisis",
      text: "Протокол",
      contacts: [CONTACTS[0]],
      draftId: ID,
    });
  });
});

describe("isPartnerMessage", () => {
  test("перечни контракта", () => {
    expect(MESSAGE_STATUSES).toEqual(["processing", "withheld", "delivered", "read", "answered"]);
    expect(WITHHELD_REASONS).toEqual(["safety", "model_refusal", "pipeline_error", "attempts_exhausted", "couple_ended"]);
    expect(WITHHELD_CRISES).toEqual(["violence", "threat_outgoing", "self_harm", "life_threat"]);
  });

  const okCases: [string, Record<string, unknown>][] = [
    ["базовый", {}],
    ["без необязательных полей", { harsh_tone_warning: undefined, withheld_reason: undefined, withheld_crisis: undefined, reply_to_id: undefined, read_at: undefined }],
    ["UnavailableText", { text: { unavailable: true, reason: "context" } }],
    ["read_at дата", { status: "read", read_at: "2026-11-26T10:00:00+03:00" }],
    ...MESSAGE_STATUSES.map((s) => [`своё ${s}`, { status: s }] as [string, Record<string, unknown>]),
    ...["delivered", "read", "answered"].map((s) => [`полученное ${s}`, { direction: "incoming", status: s }] as [string, Record<string, unknown>]),
    ...WITHHELD_REASONS.map((r) => [`withheld/${r}`, { status: "withheld", withheld_reason: r }] as [string, Record<string, unknown>]),
    ...WITHHELD_CRISES.map((c) => [`safety/${c}`, { status: "withheld", withheld_reason: "safety", withheld_crisis: c }] as [string, Record<string, unknown>]),
    ["safety без типа", { status: "withheld", withheld_reason: "safety", withheld_crisis: null }],
    ["withheld без причины (до 0013)", { status: "withheld" }],
  ];
  test.each(okCases)("корректное: %s", (_n, patch) => expect(isPartnerMessage(strip({ ...msg, ...patch }))).toBe(true));

  test.each([
    ["id не uuid", { id: "1" }],
    ["направление не из перечня", { direction: "both" }],
    ["статус не из перечня", { status: "sent" }],
    ["is_adapted строкой", { is_adapted: "true" }],
    ["harsh_tone_warning числом", { harsh_tone_warning: 1 }],
    ["текст числом", { text: 1 }],
    ["UnavailableText без причины", { text: { unavailable: true } }],
    ["sent_at не дата", { sent_at: "вчера" }],
    ["read_at не дата", { read_at: "1" }],
    ["полученное processing", { direction: "incoming", status: "processing" }],
    ["полученное withheld", { direction: "incoming", status: "withheld" }],
    ["причина у доставленного", { status: "delivered", withheld_reason: "safety" }],
    ["причина не из перечня", { status: "withheld", withheld_reason: "other" }],
    ["тип кризиса без safety", { status: "withheld", withheld_reason: "model_refusal", withheld_crisis: "violence" }],
    ["тип кризиса у доставленного", { withheld_crisis: "self_harm" }],
    ["тип кризиса не из перечня", { status: "withheld", withheld_reason: "safety", withheld_crisis: "other" }],
    ["reply_to_id не uuid", { reply_to_id: "x" }],
  ])("неверное: %s", (_n, patch) => expect(isPartnerMessage(strip({ ...msg, ...patch }))).toBe(false));
});

describe("isCoupleFeed и isMessageOriginal", () => {
  test("страница: items, next_cursor — строка/null/нет поля; повтор id — не по контракту", () => {
    expect(isCoupleFeed({ items: [], next_cursor: null })).toBe(true);
    expect(isCoupleFeed({ items: [msg] })).toBe(true);
    expect(isCoupleFeed({ items: [msg], next_cursor: "c" })).toBe(true);
    expect(isCoupleFeed({ items: [msg], next_cursor: "" })).toBe(false);
    expect(isCoupleFeed({ items: [msg, msg] })).toBe(false);
    expect(isCoupleFeed({ items: [{ ...msg, status: "x" }] })).toBe(false);
    expect(isCoupleFeed({ next_cursor: null })).toBe(false);
    expect(isCoupleFeed([])).toBe(false);
  });
  test("оригинал: строка или UnavailableText", () => {
    expect(isMessageOriginal({ text: "a" })).toBe(true);
    expect(isMessageOriginal({ text: { unavailable: true, reason: "auth" } })).toBe(true);
    expect(isMessageOriginal({ text: 1 })).toBe(false);
    expect(isMessageOriginal({})).toBe(false);
  });
});

describe("Api на заглушке: статусы и kind совпадают, send — только 202/processing", () => {
  async function api(setup?: (s: FakeServer) => void) {
    const server = new FakeServer().onboarded().pendingCouple();
    server.partnerAccepts();
    server.addChat("owner", "Реплика");
    setup?.(server);
    const a = new Api({ initData: () => INIT_DATA, fetch: server.fetch });
    await a.login();
    return { a, server };
  }
  const code = async (p: Promise<unknown>) => {
    try {
      await p;
      return "ok";
    } catch (e) {
      return e instanceof ApiError ? `${e.status} ${e.code}` : "other";
    }
  };

  test("draftCreate: 201 draft, 200 crisis; 201+crisis и 200+draft — bad_response", async () => {
    const { a, server } = await api();
    expect((await a.draftCreate()).kind).toBe("draft");
    server.draftReply({ kind: "crisis" });
    expect((await a.draftCreate()).kind).toBe("crisis");
    server.inject("POST", "/drafts", 201, { kind: "crisis", text: "x", contacts: CONTACTS });
    expect(await code(a.draftCreate())).toBe("201 bad_response");
    server.inject("POST", "/drafts", 200, draft);
    expect(await code(a.draftCreate())).toBe("200 bad_response");
  });

  test("draftSend: 202 processing outgoing; 200 или не processing — bad_response; тело только expected_version", async () => {
    const { a, server } = await api();
    const d = server.addDraft();
    const m = await a.draftSend(d.id, 1);
    expect(m).toMatchObject({ direction: "outgoing", status: "processing", text: d.text });
    expect(server.log.at(-1)!.body).toEqual({ expected_version: 1 });
    const d2 = server.addDraft();
    server.inject("POST", `/drafts/${d2.id}/send`, 200, { ...m, id: MID });
    expect(await code(a.draftSend(d2.id, 1))).toBe("200 bad_response");
    server.inject("POST", `/drafts/${d2.id}/send`, 202, { ...m, id: MID, status: "delivered" });
    expect(await code(a.draftSend(d2.id, 1))).toBe("202 bad_response");
    expect(await code(a.draftSend(d2.id, 5))).toBe("409 version_conflict");
  });

  test("draftReject: 204 и повтор 204; отправленный — 409 not_editable; 200 с телом — bad_response", async () => {
    const { a, server } = await api();
    const d = server.addDraft();
    await a.draftReject(d.id);
    await a.draftReject(d.id);
    const s = server.addDraft({ status: "sent" });
    expect(await code(a.draftReject(s.id))).toBe("409 not_editable");
    server.inject("POST", `/drafts/${d.id}/reject`, 200, {});
    expect(await code(a.draftReject(d.id))).toBe("200 bad_response");
  });

  test("draftPatch и draftGet; regenerate — 200 draft/crisis", async () => {
    const { a, server } = await api();
    const d = server.addDraft();
    const p = await a.draftPatch(d.id, "Новый", 1);
    expect(p).toMatchObject({ text: "Новый", version: 2 });
    expect((await a.draftGet(d.id)).version).toBe(2);
    expect(await code(a.draftPatch(d.id, "x", 1))).toBe("409 version_conflict");
    const r = await a.draftRegenerate(d.id, "мягче");
    expect(r.kind).toBe("draft");
    server.draftReply({ kind: "crisis" });
    const c = await a.draftRegenerate(d.id);
    expect(c).toMatchObject({ kind: "crisis", draftId: d.id });
    server.inject("POST", `/drafts/${d.id}/regenerate`, 201, draft);
    expect(await code(a.draftRegenerate(d.id))).toBe("201 bad_response");
  });

  test("ошибки — тексты на «ты» по операции, разговор цел", async () => {
    const { a, server } = await api();
    server.draftReply({ kind: "status", status: 503, body: { code: "llm_unavailable", message: "Агент недоступен. Попробуйте позже" } });
    const e = (await a.draftCreate().catch((x: unknown) => x)) as ApiError;
    expect(e.message).toContain("Разговор сохранён");
    expect(e.message).not.toContain("Попробуйте");
  });

  test("лента, оригинал (409 conflict_retry — один повтор), read 204", async () => {
    const { a, server } = await api((s) => s.seedFeed());
    const page = await a.coupleMessages({ limit: 2 });
    expect(page.items).toHaveLength(2);
    expect(page.next_cursor).toBeTruthy();
    const rest = await a.coupleMessages({ cursor: page.next_cursor, limit: 2 });
    expect(rest.items).toHaveLength(1);
    server.feedCursorKey = 9;
    expect(await code(a.coupleMessages({ cursor: page.next_cursor }))).toBe("422 validation_error");
    const inc = server.feed.find((f) => f.author === "partner")!;
    server.inject("GET", `/couple/messages/${inc.id}/original`, 409, { code: "conflict_retry", message: "x" });
    expect(await a.messageOriginal(inc.id)).toBe(inc.original);
    await a.messageRead(inc.id);
    const mine = server.feed.find((f) => f.author === "me")!;
    expect(await code(a.messageRead(mine.id))).toBe("404 not_found");
  });
});

describe("Протокол помощи под остановленным сообщением — шаблон crisis.v2 слово в слово", () => {
  const prompt = readFileSync(join(__dirname, "../../../prompts/crisis.v2.md"), "utf8");
  const block = (name: string) => {
    const m = new RegExp(`<!-- prompt:${name} -->\\n([\\s\\S]*?)\\n<!-- /prompt:${name} -->`).exec(prompt);
    return m?.[1].trim();
  };
  test.each(Object.keys(CRISIS_BLOCKS))("блок %s совпадает с prompts/crisis.v2.md", (name) => {
    expect(CRISIS_BLOCKS[name as keyof typeof CRISIS_BLOCKS]).toBe(block(name));
  });
  test("консервативный выбор блоков по главному типу (ревью 3.4 MAJOR-1): «не отправлено» первым всегда", () => {
    expect(crisisBlocks("threat_outgoing")).toEqual(["not_sent", "outgoing_threat_brief", "author_safety"]);
    expect(crisisBlocks("violence")).toEqual(["not_sent", "author_safety"]);
    expect(crisisBlocks("self_harm")).toEqual(["not_sent", "self_harm", "author_safety"]);
    expect(crisisBlocks("life_threat")).toEqual(["not_sent", "author_safety_life"]);
    expect(crisisBlocks(null)).toEqual(["not_sent", "author_safety"]);
    expect(crisisBlocks(undefined)).toEqual(["not_sent", "author_safety"]);
  });
  test("блоков, которые crisis.respond запрещает при смешанном наборе, на клиенте нет вовсе", () => {
    // violence («вина не на тебе»), life_threat («твоя жизнь в опасности») — при угрозе от автора; полный
    // outgoing_threat / outgoing_threat_mixed («вернёмся к разговору») — при self_harm (R2-MAJOR-1, R5-MAJOR-1).
    for (const name of ["violence", "life_threat", "outgoing_threat", "outgoing_threat_mixed"]) expect(name in CRISIS_BLOCKS).toBe(false);
    const all = Object.values(CRISIS_BLOCKS).join("\n");
    for (const phrase of ["Вина за это не на тебе", "Твоя жизнь сейчас в опасности", "вернёмся к разговору", "подумаем, как сказать"])
      expect(all).not.toContain(phrase);
  });
});
