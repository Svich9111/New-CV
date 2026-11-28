/**
 * A8, связка 0021 / АР-57 (задача 3.4, раунд 3 клиента): независимая проверка `src/couple/crisis.ts` и таблицы
 * `src/test/crisisCases.ts` (ТЗ §9.3; ФТ-11; АР-45, АР-57).
 *
 * - таблица клиента — все 32 сочетания «набор × главная», без повторов, и 4 строки Б-103 с иной первой строкой набора; правила §9.3, проверяемые без кода сервера
 *   (сверка с `crisis.plan` — серверный страж `backend/tests/test_crisis_blocks_a8.py`);
 * - любое нарушение контракта в `withheld_crisis_blocks` — откат на консервативный набор этого сообщения;
 * - у полученного, у не остановленного и при другой причине блоков нет никогда.
 */
import type { PartnerMessage } from "../api/types";
import { SERVER_CRISIS_CASES } from "../test/crisisCases";
import { CRISIS_BLOCK_NAMES, crisisBlocks, serverCrisisBlocks, withheldCrisisBlocks } from "./crisis";

const TYPES = ["threat_outgoing", "violence", "self_harm", "life_threat"] as const;

const msg = (patch: Record<string, unknown>): PartnerMessage =>
  ({
    id: "e0000000-0000-4000-8000-000000000001",
    direction: "outgoing",
    text: "Выдуманный текст",
    is_adapted: false,
    harsh_tone_warning: false,
    status: "withheld",
    withheld_reason: "safety",
    withheld_crisis: "violence",
    withheld_crisis_blocks: null,
    reply_to_id: null,
    sent_at: "2026-11-25T19:00:00Z",
    read_at: null,
    ...patch,
  }) as PartnerMessage;

describe("A8 · таблица crisisCases.ts — полнота и правила §9.3 (АР-57)", () => {
  test("ровно 32 сочетания: каждое непустое подмножество 4 признаков × каждая главная из него, без повторов", () => {
    const want = new Set<string>();
    for (let mask = 1; mask < 16; mask++) {
      const set = TYPES.filter((_, i) => mask & (1 << i));
      for (const main of set) want.add(`${[...set].sort().join("+")}@${main}`);
    }
    const got = SERVER_CRISIS_CASES.filter((c) => c.first === undefined).map((c) => `${[...c.types].sort().join("+")}@${c.main}`);
    expect(got).toHaveLength(32);
    expect(new Set(got).size).toBe(32);
    expect(new Set(got)).toEqual(want);
  });

  test("Б-103 (раунд 2): 4 строки с первой — категорией модели при главной life_threat; блоки те же, после экстренного — блок первой", () => {
    const lead = SERVER_CRISIS_CASES.filter((c) => c.first !== undefined);
    expect(lead.map((c) => `${c.types.join("+")}@${c.main}`).sort()).toEqual(
      [
        "self_harm+life_threat@life_threat",
        "self_harm+violence+life_threat@life_threat",
        "violence+life_threat@life_threat",
        "violence+self_harm+life_threat@life_threat",
      ].sort(),
    );
    for (const c of lead) {
      expect(c.main).toBe("life_threat");
      expect(c.types[0]).toBe(c.first);
      expect(c.types).not.toContain("threat_outgoing");
      // §9.3 стр. 4: «звони» — сразу после «не отправлено»; затем — блок категории модели.
      expect(c.blocks.slice(0, 3)).toEqual(["not_sent", "life_threat", c.first]);
      // Те же блоки, что у строки «набор × главная»: меняется только порядок.
      const base = SERVER_CRISIS_CASES.find(
        (b) => b.first === undefined && b.main === c.main && [...b.types].sort().join() === [...c.types].sort().join(),
      )!;
      expect([...c.blocks].sort()).toEqual([...base.blocks].sort());
    }
    // Различный первый элемент одного набора — различный порядок блоков на экране.
    const vsl = lead.filter((c) => c.types.length === 3).map((c) => c.blocks.join());
    expect(new Set(vsl).size).toBe(2);
  });

  test.each(SERVER_CRISIS_CASES.map((c) => [`${c.types.join("+")}@${c.main}${c.first ? `^${c.first}` : ""}`, c] as const))(
    "%s — правила §9.3",
    (_n, c) => {
      const has = (t: (typeof TYPES)[number]) => c.types.includes(t);
      const b = [...c.blocks];
      expect(b.length).toBeGreaterThan(0);
      expect(new Set(b).size).toBe(b.length);
      for (const x of b) expect(CRISIS_BLOCK_NAMES).toContain(x);
      // Строка 3: при самоповреждении — всегда блок поддержки.
      if (has("self_harm")) expect(b).toContain("self_harm");
      // Строка 4: при угрозе жизни «немедленно звони» — в начале (после «не отправлено»).
      if (has("life_threat"))
        expect(b.filter((x) => x !== "not_sent")[0]).toMatch(/^(life_threat|author_safety_life)$/);
      if (has("threat_outgoing")) {
        // Правило «А»: блоков пострадавшему автору угрозы нет; ровно один блок отправителю.
        expect(b).not.toContain("violence");
        expect(b).not.toContain("life_threat");
        expect(b.filter((x) => x.startsWith("outgoing_threat"))).toHaveLength(1);
        // При самоповреждении — без «вернёмся к разговору» (R5-MAJOR-1).
        if (has("self_harm")) expect(b).not.toContain("outgoing_threat");
        if (has("self_harm")) expect(b).not.toContain("outgoing_threat_mixed");
        // При насилии/угрозе жизни — помощь не теряется (author_safety / author_safety_life).
        if (has("violence") || has("life_threat")) expect(b.some((x) => x.startsWith("author_safety"))).toBe(true);
      } else {
        // Правило «Б»: «не отправлено» первым, блок каждой строки, без блоков отправителю.
        expect(b[0]).toBe("not_sent");
        for (const t of c.types) expect(b).toContain(t);
        expect(b.some((x) => x.startsWith("outgoing_threat") || x.startsWith("author_safety"))).toBe(false);
      }
    },
  );
});

describe("A8 · нарушение контракта withheld_crisis_blocks — откат на консервативный набор (АР-57)", () => {
  const BAD: [string, unknown][] = [
    ["null", null],
    ["undefined", undefined],
    ["число", 3],
    ["true", true],
    ["строка-имя", "not_sent"],
    ["объект-массивоподобный", { 0: "not_sent", length: 1 }],
    ["вложенный массив", [["not_sent"]]],
    ["null в массиве", ["not_sent", null]],
    ["регистр", ["NOT_SENT"]],
    ["пробел", [" not_sent"]],
    ["__proto__", ["__proto__"]],
    ["constructor", ["constructor"]],
    ["hasOwnProperty", ["hasOwnProperty"]],
    ["valueOf", ["not_sent", "valueOf"]],
    ["вариант чата смешанный", ["outgoing_threat_mixed_chat"]],
    ["underage", ["not_sent", "underage"]],
    ["повтор не подряд", ["author_safety", "not_sent", "author_safety"]],
    ["объект имени", [{ name: "not_sent" }]],
  ];
  test.each(BAD)("%s — сервер не принят, набор по withheld_crisis, источник conservative", (_n, bad) => {
    expect(serverCrisisBlocks(bad)).toBeNull();
    for (const kind of ["threat_outgoing", "violence", "self_harm", "life_threat", null] as const) {
      expect(withheldCrisisBlocks(msg({ withheld_crisis: kind, withheld_crisis_blocks: bad }))).toEqual({
        blocks: crisisBlocks(kind),
        source: "conservative",
      });
    }
  });

  test("консервативный набор всегда начинается с «не отправлено» и не пуст", () => {
    for (const kind of ["threat_outgoing", "violence", "self_harm", "life_threat", null, undefined] as const) {
      const b = crisisBlocks(kind);
      expect(b[0]).toBe("not_sent");
      expect(b.length).toBeGreaterThan(1);
    }
  });
});

describe("A8 · блоки только автору остановленного безопасностью (АР-57, принцип 4)", () => {
  const ALL = [...CRISIS_BLOCK_NAMES];
  test.each([
    ["полученное остановленное (нарушение контракта)", { direction: "incoming" }],
    [
      "полученное доставленное",
      {
        direction: "incoming",
        status: "delivered",
        withheld_reason: null,
        withheld_crisis: null,
      },
    ],
    ["своё в обработке", { status: "processing", withheld_reason: null, withheld_crisis: null }],
    ["своё доставленное", { status: "delivered", withheld_reason: null, withheld_crisis: null }],
    ["своё прочитанное", { status: "read", withheld_reason: null, withheld_crisis: null }],
    ["отказ модели", { withheld_reason: "model_refusal", withheld_crisis: null }],
    ["ошибка конвейера", { withheld_reason: "pipeline_error", withheld_crisis: null }],
    ["пара распалась", { withheld_reason: "couple_ended", withheld_crisis: null }],
    ["причина неизвестна", { withheld_reason: null, withheld_crisis: null }],
  ])("%s — блоков нет, даже если сервер прислал все 9", (_n, patch) => {
    expect(withheldCrisisBlocks(msg({ ...patch, withheld_crisis_blocks: ALL })).blocks).toEqual([]);
  });

  test("набор сервера показывается как есть, без «подмешивания» консервативного", () => {
    const m = msg({
      withheld_crisis: "threat_outgoing",
      withheld_crisis_blocks: ["outgoing_threat"],
    });
    expect(withheldCrisisBlocks(m)).toEqual({
      blocks: ["outgoing_threat"],
      source: "server",
    });
  });
});
