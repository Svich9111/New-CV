"""3.4, предложение A1 № 5 (АР-45, дополнение 3.4) и Б-103: протокол §9.3 автору остановленного
сообщения — по **всему** набору признаков, ровно как ``crisis.respond`` в конвейере.

- ``withheld_crisis_blocks`` для каждого сочетания признаков сообщения и каждой главной строки
  совпадает с ``crisis.respond(..., withheld=True).blocks``, а текст ответа — с текстами этих
  блоков; перечисление контракта = объединение блоков всех сочетаний (ни больше, ни меньше);
- БД (0021): набор — только типы (``safety_event_type[]``), только у ``safety``, главный тип в
  наборе, без ``underage``; 22023 на нарушение; дубли схлопываются; права и RLS не расширены
  (``feelings_app`` колонку не читает); лента отдаёт набор только автору;
- API: автору — блоки по набору; до 0021 (набор NULL) — null; получателю и посторонней —
  ни сообщения, ни полей; текстов в новых полях и журналах нет;
- воркер: сообщение с несколькими признаками → в ленте автора те же блоки, что в ответе
  конвейера (``Delivery.author_notice.blocks``);
- Б-103: угроза жизни — главная кодом; ``underage`` не затирает известный признак;
- миграция 0021: upgrade → downgrade → upgrade, тела и права функций 0014 восстанавливаются
  точно, ``alembic check`` чист.

Данные выдуманные; тексты помечены метками — утечки ищутся поиском меток."""

from __future__ import annotations

import itertools
import logging
import uuid
from typing import Any, get_args

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.ai import crisis, pipeline
from app.ai.prompts import V0, V2, load
from app.api.couple_messages import CrisisBlock, PartnerMessage
from app.db import sqlstate, user_scope
from app.repositories import pipeline_work as pw
from app.repositories.chat import SAFETY_EVENT_TYPES
from app.repositories.couple_messages import crisis_blocks
from tests import _pipeline_helpers as helpers
from tests import test_couple_messages_api as base
from tests import test_pipeline_worker as worker_tests
from tests.conftest import alembic
from tests.contract import SPEC
from tests.test_couple_messages_api import FEED, Env, U, _error, _pair, _person, feed, send
from tests.test_pipeline_worker import KEYS, Model, author_view, honest, worker
from tests.test_pipeline_worker import send as worker_send

env = base.env  # фикстура ленты (клиент, ключи, БД)
pair = worker_tests.pair  # фикстура пары воркера (чужие строки очереди запаркованы)

MESSAGE_TYPES: tuple[crisis.CrisisType, ...] = (
    "outgoing_threat",
    "violence",
    "self_harm",
    "life_threat",
)
DB = SAFETY_EVENT_TYPES  # crisis → safety_event_type
SETS = [
    combo for n in range(1, 5) for combo in itertools.combinations(MESSAGE_TYPES, n)
]  # fmt: skip
CASES = [(combo, main) for combo in SETS for main in combo]  # 32 сочетания (набор, главная)


def _ids(case: tuple[tuple[str, ...], str]) -> str:
    return f"{'+'.join(case[0])}@{case[1]}"


# =========================================================================== правила (без БД)


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_blocks_equal_respond_for_every_combination(case: tuple[tuple[str, ...], str]) -> None:
    """Блоки ленты = блоки ответа конвейера (тот же ``crisis.plan``) по набору и главной из БД;
    порядок блоков — как в ``crisis.respond``, текст ответа — тексты этих блоков (v2)."""
    types, main = case
    want = crisis.respond(types, primary=main, prompt_set=V2, withheld=True)
    got = crisis_blocks(DB[main], [DB[t] for t in reversed(types)])  # порядок в БД не важен
    assert got == want.blocks and got
    assert want.text == "\n\n".join(load("crisis", "v2", block=b).text for b in got)
    assert set(got) <= set(get_args(CrisisBlock))
    # Повторы в наборе и главная вне порядка не меняют ответ.
    dup = [DB[t] for t in (*types, *types)]
    assert crisis_blocks(DB[main], dup) == got


def test_block_enum_is_exactly_union_of_all_combinations() -> None:
    """Перечисление схемы ответа = контракт = объединение блоков всех сочетаний: ни лишнего,
    ни недостающего; вариантов личного чата (``*_chat``) и ``underage`` в нём нет."""
    union = {
        b for types, main in CASES for b in crisis.plan(types, primary=main, withheld=True).blocks
    }
    model = set(get_args(CrisisBlock))
    prop = SPEC["components"]["schemas"]["PartnerMessage"]["properties"]["withheld_crisis_blocks"]
    assert union == model == set(prop["items"]["enum"])
    assert not any(b.endswith("_chat") for b in union) and "underage" not in union
    assert prop["type"] == ["array", "null"] and prop.get("minItems") == 1
    assert (
        "withheld_crisis_blocks" not in SPEC["components"]["schemas"]["PartnerMessage"]["required"]
    )
    schema = PartnerMessage.model_json_schema()["properties"]["withheld_crisis_blocks"]
    assert {"type": "null"} in schema["anyOf"]


def test_unknown_or_inconsistent_set_gives_null() -> None:
    """Набора нет (до 0021), главная вне набора, ``underage`` главной, мусор — ``None``: клиент
    показывает консервативный набор (никакого «придуманного» протокола)."""
    assert crisis_blocks("violence", None) is None
    assert crisis_blocks(None, ["violence"]) is None
    assert crisis_blocks("violence", []) is None
    assert crisis_blocks("self_harm", ["violence"]) is None
    assert crisis_blocks("underage", ["underage", "violence"]) is None
    assert crisis_blocks("nonsense", ["nonsense"]) is None
    assert crisis_blocks("violence", ["underage", "violence"]) == ("not_sent", "violence")


# --------------------------------------------------------------------------- Б-103


@pytest.mark.parametrize("prompt_set", [V0, V2], ids=["V0", "V2"])
@pytest.mark.parametrize(
    ("category", "secondary", "main"),
    [
        ("threat_or_insult", ["life_threat"], "life_threat"),
        ("violence", ["life_threat", "self_harm"], "life_threat"),
        ("self_harm", ["life_threat"], "life_threat"),
        ("none", ["violence", "life_threat"], "life_threat"),
        ("underage", ["violence"], "violence"),  # underage не затирает известный признак
        ("underage", ["self_harm", "outgoing_threat"], "self_harm"),
        ("violence", ["self_harm"], "violence"),  # без угрозы жизни — категория модели
    ],
)
async def test_b103_life_threat_primary_by_code(
    prompt_set: Any, category: str, secondary: list[str], main: str
) -> None:
    """Б-103: при признаке угрозы жизни главная — ``life_threat`` кодом (модель поставила
    другую); ``underage`` у сообщения — не строка конвейера и главную не занимает; в ответе
    автору — весь набор."""
    sec = ["threat_or_insult" if s == "outgoing_threat" else s for s in secondary]

    def answer(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        if role == "safety":
            return {"category": category, "secondary": sec}
        return honest(role, payload)

    delivery = await pipeline.process(
        _models(answer), pipeline.approve("Выдуманный текст", confirmed=True), prompt_set=prompt_set
    )
    notice = delivery.author_notice
    assert notice is not None and notice.primary == main
    known = {pipeline.MESSAGE_CRISIS.get(c, c) for c in (category, *sec)} - {"none", "underage"}
    assert set(notice.types) == known
    assert delivery.safety_event == main
    assert pw.crisis_type(notice.primary, (*notice.types, *delivery.safety_events)) == DB[main]


def _models(answer: Any) -> Any:
    from app.ai.models import AgentModels

    return AgentModels.single(Model(answer))


def test_b103_crisis_type_rules(caplog: pytest.LogCaptureFixture) -> None:
    """``crisis_type``: угроза жизни среди признаков — главная; ``underage`` пропускается и не
    затирает вторичный признак; неизвестно ничего — осторожный ``life_threat`` с
    предупреждением (без текста)."""
    caplog.set_level(logging.WARNING)
    assert pw.crisis_type("underage", ("underage", "violence")) == "violence"
    assert pw.crisis_type("underage", ("underage", "outgoing_threat")) == "threat_outgoing"
    assert pw.crisis_type("violence", ("self_harm", "life_threat")) == "life_threat"
    assert pw.crisis_type("outgoing_threat", ("violence",)) == "threat_outgoing"
    assert pw.crisis_type(None, ("self_harm", "violence")) == "self_harm"
    assert "crisis type unknown" not in caplog.text
    assert pw.crisis_type("underage", ("underage", "underage")) == "life_threat"
    assert "pipeline: crisis type unknown, cautious protocol used" in caplog.text
    assert pw.crisis_types(["outgoing_threat", "underage", "violence", "violence", "x"]) == [
        "threat_outgoing",
        "violence",
    ]


# =========================================================================== БД (0021)


async def _stopped(env: Env, who: U, *, types: list[str] | None, main: str) -> uuid.UUID:
    """Сообщение автора, остановленное безопасностью слоем доступа воркера (захват → 6-арг.
    ``pipeline_withhold_leased``)."""
    mid = await send(env, who, env.mark(f"stop-{main}"))
    c = await helpers.claim(env.engine, mid)
    assert c is not None
    ok = await pw.withhold(async_sessionmaker(env.engine), c, "safety", (main,), main, types)
    assert ok is True
    return mid


async def _trio(env: Env) -> tuple[U, U, U]:
    anna, igor = await _person(env, "Анна", "Игорь"), await _person(env, "Игорь", "Анна")
    olga = await _person(env, "Ольга", "Пётр")
    await _pair(env, anna, igor)
    return anna, igor, olga


@pytest.mark.parametrize(
    "case",
    [
        (("outgoing_threat", "self_harm", "violence"), "outgoing_threat"),
        (("violence", "self_harm"), "violence"),
        (("life_threat", "violence", "outgoing_threat", "self_harm"), "life_threat"),
        (("violence",), "violence"),
    ],
    ids=_ids,
)
async def test_feed_gives_author_exact_blocks_recipient_nothing(
    env: Env, case: tuple[tuple[str, ...], str]
) -> None:
    """Автору — блоки ``crisis.respond`` по набору (порядок показа), главный тип; получателю и
    посторонней — ни сообщения, ни полей; оригинал остановленного им — 404; текста сообщения в
    новых полях нет."""
    types, main = case
    anna, igor, olga = await _trio(env)
    mid = await _stopped(env, anna, types=list(types), main=main)
    want = crisis.respond(types, primary=main, withheld=True).blocks

    item = {i["id"]: i for i in (await feed(env, anna))["items"]}[str(mid)]
    assert (item["withheld_reason"], item["withheld_crisis"]) == ("safety", DB[main])
    assert item["withheld_crisis_blocks"] == list(want)
    assert not any(m in str(item["withheld_crisis_blocks"]) for m in env.marks)

    for who in (igor, olga):
        page = await feed(env, who)
        assert str(mid) not in str(page)
        assert all(i.get("withheld_crisis_blocks") is None for i in page["items"])
        _error(
            await env.client.get(f"{FEED}/{mid}/original", headers=who.headers), 404, "not_found"
        )


async def test_unknown_set_is_null_and_other_reasons_have_no_blocks(env: Env) -> None:
    """Набор неизвестен (5-аргументный вызов, как до 0021) — ``null`` (клиент: консервативный
    набор по ``withheld_crisis``); у других причин и у доставленных блоков нет."""
    anna, igor, _olga = await _trio(env)
    old = await _stopped(env, anna, types=None, main="violence")
    refused = await send(env, anna, env.mark("refused"))
    assert await helpers.withhold(env.engine, refused, "model_refusal") is True
    ok = await send(env, anna, env.mark("ok"))
    await base.deliver(env, ok, env.mark("shown"), adapted=False)
    mine = {i["id"]: i for i in (await feed(env, anna))["items"]}
    assert mine[str(old)]["withheld_crisis"] == "violence"
    assert mine[str(old)]["withheld_crisis_blocks"] is None
    assert mine[str(refused)]["withheld_crisis_blocks"] is None
    assert mine[str(ok)]["withheld_crisis_blocks"] is None
    got = (await feed(env, igor))["items"]
    assert [i["id"] for i in got] == [str(ok)] and got[0]["withheld_crisis_blocks"] is None


async def test_function_normalizes_and_rejects_bad_sets(env: Env) -> None:
    """``pipeline_withhold_leased`` (0021): набор — только при ``safety``, непустой, без NULL и
    ``underage``, с главным типом (иначе 22023, сообщение не меняется); дубли схлопываются,
    порядок — перечисления; набор только из типов (колонка ``safety_event_type[]``)."""
    anna, _igor, _olga = await _trio(env)
    mid = await send(env, anna, env.mark("fn"))
    c = await helpers.claim(env.engine, mid)
    assert c is not None
    call = (
        "SELECT pipeline_withhold_leased(:m, :t, CAST(:r AS varchar), NULL, "
        "CAST(CAST(:c AS text) AS safety_event_type), CAST(CAST(:ct AS text[]) AS "
        "safety_event_type[]))"
    )
    bad = [
        ("model_refusal", None, ["violence"]),
        ("safety", "violence", []),
        ("safety", "violence", ["self_harm"]),
        ("safety", "violence", ["violence", None]),
        ("safety", "violence", ["violence", "underage"]),
        ("safety", None, ["violence"]),
    ]
    for reason, main, ct in bad:
        async with env.engine.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text("SET LOCAL ROLE feelings_worker"))
                with pytest.raises(DBAPIError) as err:
                    await conn.execute(
                        text(call), {"m": mid, "t": c.token, "r": reason, "c": main, "ct": ct}
                    )
                assert sqlstate(err.value) == "22023", (reason, main, ct)
            finally:
                await tx.rollback()
    async with env.engine.connect() as conn, conn.begin():
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        ok = await conn.execute(
            text(call),
            {"m": mid, "t": c.token, "r": "safety", "c": "self_harm",
             "ct": ["self_harm", "threat_outgoing", "self_harm", "violence"]},
        )  # fmt: skip
        assert ok.scalar_one() is True
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT withheld_crisis::text AS main, withheld_crisis_types::text[] AS types "
                    "FROM partner_messages WHERE id = :m"
                ),
                {"m": mid},
            )
        ).one()
        col = (
            await conn.execute(
                text(
                    "SELECT udt_name FROM information_schema.columns WHERE table_name = "
                    "'partner_messages' AND column_name = 'withheld_crisis_types'"
                )
            )
        ).scalar_one()
    assert (row.main, row.types) == ("self_harm", ["violence", "threat_outgoing", "self_harm"])
    assert col == "_safety_event_type"  # массив перечисления: текста в колонке быть не может


async def test_check_constraint_guards_direct_writes(env: Env) -> None:
    """CHECK 0021 (вторая граница, мимо функции): набор без главного типа, с ``underage``,
    без главного в наборе, пустой — 23514."""
    anna, _igor, _olga = await _trio(env)
    mid = await send(env, anna, env.mark("check"))
    cases = [
        ("processing", None, None, "{violence}"),
        ("withheld", "safety", "violence", "{}"),
        ("withheld", "safety", "violence", "{self_harm}"),
        ("withheld", "safety", "violence", "{violence,underage}"),
        ("withheld", "safety", "violence", "{violence,NULL}"),
    ]
    for status, reason, main, ct in cases:
        async with env.engine.connect() as conn:
            tx = await conn.begin()
            try:
                with pytest.raises(DBAPIError) as err:
                    await conn.execute(
                        text(
                            "UPDATE partner_messages SET status = CAST(:s AS "
                            "partner_message_status), withheld_reason = :r, withheld_crisis = "
                            "CAST(:c AS safety_event_type), withheld_crisis_types = "
                            "CAST(CAST(:ct AS text) AS safety_event_type[]) WHERE id = :m"
                        ),
                        {"s": status, "r": reason, "c": main, "ct": ct, "m": mid},
                    )
                assert sqlstate(err.value) == "23514", (status, ct)
            finally:
                await tx.rollback()


async def test_app_role_cannot_read_column_directly(env: Env) -> None:
    """Права не расширены: ``feelings_app`` (RLS ``pm_select``) колонку набора не читает — только
    функция ленты, и только автору; у воркера прав на таблицу нет."""
    anna, igor, _olga = await _trio(env)
    await _stopped(env, anna, types=["violence", "self_harm"], main="violence")
    for who in (anna, igor):
        with pytest.raises(DBAPIError) as err:
            async with user_scope(env.sm, who.id) as s:
                await s.execute(text("SELECT withheld_crisis_types FROM partner_messages"))
        assert sqlstate(err.value) == "42501"
    async with env.engine.connect() as conn:
        for role in ("feelings_app", "feelings_worker", "feelings_api", "feelings_admin"):
            can = await conn.execute(
                text(
                    "SELECT has_column_privilege(:r, 'partner_messages', "
                    "'withheld_crisis_types', 'SELECT')"
                ),
                {"r": role},
            )
            assert can.scalar_one() is False, role
    # Функция ленты отдаёт набор только автору (получатель остановленного не видит вовсе).
    for who, want in ((anna, ["violence", "self_harm"]), (igor, None)):
        async with user_scope(env.sm, who.id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT withheld_crisis_types::text[] AS ct "
                        "FROM app_couple_feed_page(100, NULL, NULL) WHERE status = 'withheld'"
                    )
                )
            ).all()
        assert [r.ct for r in rows] == ([want] if want else [])


async def test_logs_have_no_texts(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    anna, igor, olga = await _trio(env)
    await _stopped(env, anna, types=["outgoing_threat", "self_harm"], main="outgoing_threat")
    for who in (anna, igor, olga):
        await feed(env, who)
    records = [r for r in caplog.records if not r.name.startswith("httpx")]
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in records)
    assert not any(m in logged for m in env.marks)


# =========================================================================== воркер


CRISIS_MIXED = "Выдуманное: ещё раз — пожалеешь; и мне не хочется жить."


async def test_worker_stores_full_set_and_feed_matches_notice(
    sessionmaker: Any, engine: AsyncEngine, pair: Any
) -> None:
    """Воркер: классификатор дал главную ``threat_or_insult`` и вторичные ``self_harm``,
    ``violence`` → в БД главный тип и весь набор; лента автора — ровно блоки ответа конвейера
    (не консервативный набор по главному типу); события — без текста."""
    anna, _igor, _ = pair

    def answer(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        if role == "safety":
            return {"category": "threat_or_insult", "secondary": ["self_harm", "violence"]}
        return honest(role, payload)

    mid = await worker_send(sessionmaker, anna, CRISIS_MIXED)
    [out] = await worker(engine, Model(answer)).run_once()
    assert (out.status, out.reason) == ("withheld", "safety")
    want = crisis.respond(
        ["outgoing_threat", "self_harm", "violence"], primary="outgoing_threat", withheld=True
    )
    view = (await author_view(sessionmaker, anna.user_id))[mid]
    assert view.withheld_crisis == "threat_outgoing"
    async with engine.connect() as c:
        row = (
            await c.execute(
                text(
                    "SELECT withheld_crisis_types::text[] AS ct, row_to_json(m)::text AS dump "
                    "FROM partner_messages m WHERE id = :m"
                ),
                {"m": mid},
            )
        ).one()
    assert sorted(row.ct) == ["self_harm", "threat_outgoing", "violence"]
    assert "пожалеешь" not in row.dump and "жить" not in row.dump
    from app.repositories import couple_messages

    items, _ = await couple_messages.read_page(sessionmaker, KEYS, anna.user_id, limit=100)
    [mine] = [i for i in items if i.id == mid]
    assert mine.withheld_crisis_blocks == want.blocks
    assert want.blocks == ("not_sent", "self_harm", "outgoing_threat_brief", "author_safety")


# =========================================================================== миграция


_FN_STATE = """
SELECT p.oid::regprocedure::text AS sig, pg_get_functiondef(p.oid) AS body,
       coalesce(p.proacl::text, '') AS acl, p.prosecdef, p.proconfig::text AS cfg,
       pg_get_userbyid(p.proowner) AS owner
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.proname IN ('pipeline_withhold_leased', 'app_couple_feed_page')
ORDER BY 1
"""


async def _state(engine: AsyncEngine) -> tuple[list[tuple[Any, ...]], list[str]]:
    async with engine.connect() as c:
        fns = [tuple(r) for r in await c.execute(text(_FN_STATE))]
        cols = [
            str(r[0])
            for r in await c.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'partner_messages' ORDER BY ordinal_position"
                )
            )
        ]
    return fns, cols


async def test_migration_0021_upgrade_downgrade_upgrade(
    env: Env, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """0021 → 0020 → 0021. Эталон 0020 собран настоящими миграциями (0013 → 0020): после
    downgrade 0021 функции остановки и ленты (тело, права, SECURITY DEFINER, ``search_path``,
    владелец) и колонки ``partner_messages`` совпадают с ним точно; главный тип остановленного
    сохраняется; снова upgrade — состояние головы точно, набор — NULL (как до 0021), блоков нет;
    ``alembic check`` чист."""
    anna, _igor, _olga = await _trio(env)
    await _stopped(env, anna, types=["violence", "self_harm"], main="violence")
    head = await _state(engine)
    assert "withheld_crisis_types" in head[1]
    assert [f[0] for f in head[0]] == [
        "app_couple_feed_page(integer,timestamp with time zone,uuid)",
        "pipeline_withhold_leased(uuid,uuid,character varying,safety_event_type[],"
        "safety_event_type,safety_event_type[])",
    ]

    async def move(*steps: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        for step in steps:
            alembic(*step.split())
        await engine.dispose()
        await app_engine.dispose()
        return await _state(engine)

    try:
        # Эталон 0020 — функции 0014 и колонки, собранные исходными ревизиями.
        ref = await move("downgrade 0013", "upgrade 0020")
        assert await move("upgrade head") == head
        at20 = await move("downgrade 0020")
        assert at20 == ref
        assert "withheld_crisis_types" not in at20[1]
        assert [f[0] for f in at20[0]][1].endswith("safety_event_type[],safety_event_type)")
        assert all(f[3] is True and "search_path=public, pg_temp" in str(f[4]) for f in at20[0])
        assert await move("upgrade head") == head
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    alembic("check")


async def test_migration_0021_downgrade_keeps_primary_type(
    env: Env, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Downgrade 0021 теряет только набор признаков: главный тип остаётся; после upgrade набор
    NULL — лента отдаёт автору ``withheld_crisis`` и ``withheld_crisis_blocks: null``."""
    anna, _igor, _olga = await _trio(env)
    mid = await _stopped(env, anna, types=["violence", "self_harm"], main="violence")
    try:
        alembic("downgrade", "0020")
        await engine.dispose()
        await app_engine.dispose()
        async with engine.connect() as c:
            kept = (
                await c.execute(
                    text("SELECT withheld_crisis::text FROM partner_messages WHERE id = :m"),
                    {"m": mid},
                )
            ).scalar_one()
        assert kept == "violence"
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    mine = {i["id"]: i for i in (await feed(env, anna))["items"]}
    assert mine[str(mid)]["withheld_crisis"] == "violence"
    assert mine[str(mid)]["withheld_crisis_blocks"] is None
