/**
 * Блоки протокола §9.3 автору остановленного сообщения для каждого сочетания признаков и главного типа — выход
 * `crisis.plan(types, primary=main, withheld=True).blocks` (`backend/app/ai/crisis.py`), те же 32 случая, что
 * `backend/tests/test_crisis_blocks.py` (`CASES`). Типы — как в БД и контракте (`threat_outgoing`). Данные для
 * тестов клиента: экран показывает ровно серверный набор (АР-57). При правке таблицы правил сервера — пересобрать.
 *
 * `first` — первая строка хранимого набора, если она не главная (Б-103, АР-57 п.4): главная повышена до `life_threat`,
 * первой стоит категория модели, и блок после экстренного — её (`CrisisResponse.ordered_types`). Без `first` первая —
 * главная. `types` таких строк — в порядке хранения. Набор строк сверяет страж
 * `backend/tests/test_crisis_blocks_a8.py::test_frontend_crisis_cases_match_server_table`.
 */
import type { CrisisBlock } from "../couple/crisis";
import type { PartnerMessage } from "../api/types";

type Crisis = NonNullable<PartnerMessage["withheld_crisis"]>;

export const SERVER_CRISIS_CASES: readonly { types: readonly Crisis[]; main: Crisis; first?: Crisis; blocks: readonly CrisisBlock[] }[] = [
  { types: ["threat_outgoing"], main: "threat_outgoing", blocks: ["outgoing_threat"] },
  { types: ["violence"], main: "violence", blocks: ["not_sent", "violence"] },
  { types: ["self_harm"], main: "self_harm", blocks: ["not_sent", "self_harm"] },
  { types: ["life_threat"], main: "life_threat", blocks: ["not_sent", "life_threat"] },
  { types: ["threat_outgoing", "violence"], main: "threat_outgoing", blocks: ["outgoing_threat_mixed", "author_safety"] },
  { types: ["threat_outgoing", "violence"], main: "violence", blocks: ["outgoing_threat_mixed", "author_safety"] },
  { types: ["threat_outgoing", "self_harm"], main: "threat_outgoing", blocks: ["not_sent", "self_harm", "outgoing_threat_brief"] },
  { types: ["threat_outgoing", "self_harm"], main: "self_harm", blocks: ["not_sent", "self_harm", "outgoing_threat_brief"] },
  { types: ["threat_outgoing", "life_threat"], main: "threat_outgoing", blocks: ["author_safety_life", "outgoing_threat_mixed"] },
  { types: ["threat_outgoing", "life_threat"], main: "life_threat", blocks: ["author_safety_life", "outgoing_threat_mixed"] },
  { types: ["violence", "self_harm"], main: "violence", blocks: ["not_sent", "violence", "self_harm"] },
  { types: ["violence", "self_harm"], main: "self_harm", blocks: ["not_sent", "self_harm", "violence"] },
  { types: ["violence", "life_threat"], main: "violence", blocks: ["not_sent", "life_threat", "violence"] },
  { types: ["violence", "life_threat"], main: "life_threat", blocks: ["not_sent", "life_threat", "violence"] },
  { types: ["self_harm", "life_threat"], main: "self_harm", blocks: ["not_sent", "life_threat", "self_harm"] },
  { types: ["self_harm", "life_threat"], main: "life_threat", blocks: ["not_sent", "life_threat", "self_harm"] },
  { types: ["threat_outgoing", "violence", "self_harm"], main: "threat_outgoing", blocks: ["not_sent", "self_harm", "outgoing_threat_brief", "author_safety"] },
  { types: ["threat_outgoing", "violence", "self_harm"], main: "violence", blocks: ["not_sent", "self_harm", "outgoing_threat_brief", "author_safety"] },
  { types: ["threat_outgoing", "violence", "self_harm"], main: "self_harm", blocks: ["not_sent", "self_harm", "outgoing_threat_brief", "author_safety"] },
  { types: ["threat_outgoing", "violence", "life_threat"], main: "threat_outgoing", blocks: ["author_safety_life", "outgoing_threat_mixed"] },
  { types: ["threat_outgoing", "violence", "life_threat"], main: "violence", blocks: ["author_safety_life", "outgoing_threat_mixed"] },
  { types: ["threat_outgoing", "violence", "life_threat"], main: "life_threat", blocks: ["author_safety_life", "outgoing_threat_mixed"] },
  { types: ["threat_outgoing", "self_harm", "life_threat"], main: "threat_outgoing", blocks: ["not_sent", "author_safety_life", "self_harm", "outgoing_threat_brief"] },
  { types: ["threat_outgoing", "self_harm", "life_threat"], main: "self_harm", blocks: ["not_sent", "author_safety_life", "self_harm", "outgoing_threat_brief"] },
  { types: ["threat_outgoing", "self_harm", "life_threat"], main: "life_threat", blocks: ["not_sent", "author_safety_life", "self_harm", "outgoing_threat_brief"] },
  { types: ["violence", "self_harm", "life_threat"], main: "violence", blocks: ["not_sent", "life_threat", "violence", "self_harm"] },
  { types: ["violence", "self_harm", "life_threat"], main: "self_harm", blocks: ["not_sent", "life_threat", "self_harm", "violence"] },
  { types: ["violence", "self_harm", "life_threat"], main: "life_threat", blocks: ["not_sent", "life_threat", "violence", "self_harm"] },
  { types: ["threat_outgoing", "violence", "self_harm", "life_threat"], main: "threat_outgoing", blocks: ["not_sent", "author_safety_life", "self_harm", "outgoing_threat_brief"] },
  { types: ["threat_outgoing", "violence", "self_harm", "life_threat"], main: "violence", blocks: ["not_sent", "author_safety_life", "self_harm", "outgoing_threat_brief"] },
  { types: ["threat_outgoing", "violence", "self_harm", "life_threat"], main: "self_harm", blocks: ["not_sent", "author_safety_life", "self_harm", "outgoing_threat_brief"] },
  { types: ["threat_outgoing", "violence", "self_harm", "life_threat"], main: "life_threat", blocks: ["not_sent", "author_safety_life", "self_harm", "outgoing_threat_brief"] },
  // Б-103: главная life_threat, первой в наборе — категория модели (её блок — после экстренного).
  { types: ["violence", "life_threat"], main: "life_threat", first: "violence", blocks: ["not_sent", "life_threat", "violence"] },
  { types: ["self_harm", "life_threat"], main: "life_threat", first: "self_harm", blocks: ["not_sent", "life_threat", "self_harm"] },
  { types: ["violence", "self_harm", "life_threat"], main: "life_threat", first: "violence", blocks: ["not_sent", "life_threat", "violence", "self_harm"] },
  { types: ["self_harm", "violence", "life_threat"], main: "life_threat", first: "self_harm", blocks: ["not_sent", "life_threat", "self_harm", "violence"] },
];
