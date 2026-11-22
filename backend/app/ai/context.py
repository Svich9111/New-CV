"""Сборка контекста агента владельца по ТЗ §6.1 (задача 2.1; §6, §9.2 п.1).

Состав и порядок блоков (§6.1):

1. системный промпт (``dialog.vN``) — отдельным системным сообщением;
2. профиль владельца (имя/обращение, имя партнёра из своего онбординга) и сводка памяти
   (``agents.profile_summary_enc``);
3. записи памяти по смыслу текущей реплики (``Embedder``; поиск в БД функцией
   ``app_memory_nearest`` по векторам той же модели, АР-35; без эмбеддинга или при его
   ошибке — по свежести), не больше ``ContextBudget.memory_items``; удалённые — никогда;
4. фрагменты базы знаний по теме (``KnowledgeBase``: в API — ``DbKnowledgeBase`` с моделью
   эмбеддера, функция ``app_kb_nearest``; до 2.6 база пуста);
5. краткое содержание предыдущих реплик (``conversations.summary_enc``) и **все** реплики после
   отметки сводки в пределах бюджета (без разрыва между сводкой и репликами, ``turns_read_limit``);
6. одобренные и доставленные сообщения текущей пары, относящиеся к теме (по смыслу при наличии
   эмбеддера, иначе последние), — как их видел владелец.

Источник — только данные владельца через слой доступа (``repositories.chat``: ``user_scope`` +
явный отбор по владельцу + RLS). Данных партнёра (его памяти, чата, агента, профиля) здесь нет и
быть не может: их не отдаёт база, а у ``OwnerContext`` нет для них полей.

Бюджет (``ContextBudget.max_chars``) — символы системного промпта и JSON входа модели (оценка
токенов: ≈ символы / 3 для русского). При превышении блоки сокращаются в порядке, от наименее
нужного: база знаний → сообщения пары (старые первыми) → память (наименее близкие первыми) →
старые реплики → сводка диалога → сводка профиля. Системный промпт и последняя реплика владельца
не выбрасываются никогда. В статистике сборки (``ContextStats``) — только id и счётчики, без
текстов (CLAUDE.md)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.calls import as_json
from app.ai.dialog import (
    SAFETY_TURNS,
    ContextDialog,
    OwnerContext,
    PairMessage,
    Turn,
    ordered_payload,
)
from app.ai.embeddings import Embedder, EmbeddingError, check_dim, cosine
from app.ai.knowledge import KnowledgeBase, KnowledgeFragment
from app.ai.prompts import V0, Prompt, PromptSet, system_prompt
from app.core.crypto import Keyring
from app.repositories import chat
from app.repositories.chat import ContextRows, PairRow

ELLIPSIS = "…"


def _min_turn_chars() -> int:
    """Наименьший вклад одной реплики в JSON входа (непустой текст в 1 символ)."""
    turn = {"from": "agent", "text": "x"}
    one = as_json({"dialog": [turn]})
    two = as_json({"dialog": [turn, turn]})
    return len(two) - len(one)


# Инвариант «сводка + реплики без разрыва» (ТЗ §6.1 п.5; ревью 2.1 MAJOR-1): в контекст читаются
# **все** реплики после отметки сводки, сколько их ни есть, — но не больше, чем физически
# помещается в бюджет даже при самых коротких репликах. Реплика старше этого предела в контекст
# не вошла бы ни при каком усечении, поэтому предел чтения разрыва не создаёт: всё, чего нет в
# контексте, — либо в сводке, либо выброшено бюджетом (``stats.dropped["dialog"]``). От настроек
# сжатия (``summary.CompressionPolicy``) и от его сбоев сборка не зависит.
MIN_TURN_CHARS = _min_turn_chars()


def turns_read_limit(max_chars: int) -> int:
    return max(max_chars, 0) // MIN_TURN_CHARS + 1


@dataclass(frozen=True)
class ContextBudget:
    """Пределы контекста. ``max_chars`` — системный промпт + JSON входа (≈ 8–10 тыс. токенов)."""

    max_chars: int = 24_000
    memory_items: int = 8
    knowledge_items: int = 5  # §6.4: 3–5 фрагментов
    pair_messages: int = 5
    pair_candidates: int = 20
    item_chars: int = 800  # одна запись памяти, фрагмент, сообщение пары
    turn_chars: int = 4_000  # реплика (контракт POST /chat/messages: до 4000)
    summary_chars: int = 2_000
    profile_chars: int = 2_000


@dataclass(frozen=True)
class ContextStats:
    """Что вошло в контекст — без текстов (для журналов и тестов)."""

    chars: int
    max_chars: int
    turn_ids: tuple[uuid.UUID, ...]
    memory_ids: tuple[uuid.UUID, ...]
    memory_search: Literal["vector", "recency", "none"]
    knowledge_ids: tuple[str, ...]
    pair_message_ids: tuple[uuid.UUID, ...]
    has_profile: bool
    has_dialog_summary: bool
    dropped: dict[str, int] = field(default_factory=dict)  # блок → сколько выброшено/усечено
    unavailable: dict[str, int] = field(default_factory=dict)  # не расшифровалось
    embedding_error: bool = False
    over_budget: bool = False  # даже без всего лишнего последняя реплика не влезла


@dataclass(frozen=True)
class BuiltContext:
    """Готовый вход агента: системный промпт, контекст владельца и реплики диалога."""

    system: Prompt
    context: OwnerContext
    dialog: ContextDialog  # реплики после усечения; ``dialog.safety`` — вход классификатора
    conversation_id: uuid.UUID
    stats: ContextStats
    # Последние реплики владельца до усечения (не больше ``SAFETY_TURNS``) — вход второй линии
    # безопасности: классификатор видит их, даже если бюджет выбросил их из диалога (тест О-2).
    safety_dialog: tuple[Turn, ...] = ()

    def payload(self) -> dict[str, Any]:
        """JSON входа модели в порядке §6.1 (п.2–6)."""
        return ordered_payload(self.context, self.dialog)


def clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + ELLIPSIS


def size(system: str, context: OwnerContext, dialog: Sequence[Turn]) -> int:
    """Символы системного промпта и JSON входа — как они уйдут в модель (``calls.as_json``)."""
    return len(system) + len(as_json(ordered_payload(context, dialog)))


@dataclass
class _Parts:
    """Изменяемые списки блоков для усечения; ``ids`` идут параллельно текстам."""

    memory: list[tuple[uuid.UUID, str]]
    knowledge: list[tuple[str, str]]
    pair: list[tuple[uuid.UUID, PairMessage]]  # по времени, старые первыми
    turns: list[tuple[uuid.UUID, Turn]]
    profile: str
    summary: str


def _context(base: OwnerContext, parts: _Parts) -> OwnerContext:
    return replace(
        base,
        memory=tuple(t for _, t in parts.memory),
        knowledge=tuple(t for _, t in parts.knowledge),
        pair_messages=tuple(m for _, m in parts.pair),
        profile_summary=parts.profile,
        dialog_summary=parts.summary,
    )


def fit(
    system: str, base: OwnerContext, parts: _Parts, max_chars: int
) -> tuple[OwnerContext, tuple[Turn, ...], dict[str, int], bool]:
    """Усечение по бюджету (порядок — в описании модуля). → (контекст, реплики, выброшено,
    не влезло)."""
    dropped: dict[str, int] = {}

    def over() -> bool:
        return size(system, _context(base, parts), [t for _, t in parts.turns]) > max_chars

    def count(block: str) -> None:
        dropped[block] = dropped.get(block, 0) + 1

    while over():
        if parts.knowledge:
            parts.knowledge.pop()  # наименее подходящий — последним
            count("knowledge")
        elif parts.pair:
            parts.pair.pop(0)  # самое старое
            count("pair_messages")
        elif parts.memory:
            parts.memory.pop()  # наименее близкая/свежая — последней
            count("memory")
        elif len(parts.turns) > 1:
            parts.turns.pop(0)  # самая старая реплика; последняя (владельца) остаётся
            count("dialog")
        elif parts.summary:
            parts.summary = (
                clip(parts.summary, len(parts.summary) // 2) if (len(parts.summary) > 80) else ""
            )
            count("dialog_summary")
        elif parts.profile:
            parts.profile = (
                clip(parts.profile, len(parts.profile) // 2) if (len(parts.profile) > 80) else ""
            )
            count("profile")
        else:
            break
    context = _context(base, parts)
    turns = tuple(t for _, t in parts.turns)
    return context, turns, dropped, size(system, context, turns) > max_chars


async def _embed_one(embedder: Embedder, query: str) -> list[float]:
    [vector] = await embedder.embed([query])
    return check_dim(vector)


async def _rank_pair(
    rows: Sequence[PairRow],
    vector: Sequence[float] | None,
    embedder: Embedder | None,
    limit: int,
) -> list[PairRow]:
    """«Относящиеся к теме»: по близости к реплике, если есть эмбеддер; иначе последние.
    Результат — по времени (старые первыми), как идёт переписка."""
    chosen = list(rows[:limit])  # rows — новые первыми
    if vector is not None and embedder is not None and len(rows) > limit:
        try:
            vectors = await embedder.embed([r.text for r in rows])
            scored = sorted(
                zip(rows, vectors, strict=True),
                key=lambda rv: -cosine(vector, check_dim(rv[1])),
            )
            chosen = [r for r, _ in scored[:limit]]
        except EmbeddingError:
            pass  # по свежести
    return sorted(chosen, key=lambda r: (r.sent_at, r.id))


def _turns(rows: ContextRows, turn_chars: int) -> list[tuple[uuid.UUID, Turn]]:
    turns = [
        (r.id, Turn("owner" if r.author == "owner" else "agent", clip(r.text, turn_chars)))
        for r in rows.turns
        if r.text is not None and r.text.strip()
    ]
    if not turns or turns[-1][1].author != "owner":
        # API пишет реплику владельца до сборки контекста (ТЗ §10); без неё отвечать не на что.
        raise ValueError("последняя реплика диалога — не владельца")
    return turns


async def build_context(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    query: str,
    embedder: Embedder | None = None,
    knowledge: KnowledgeBase | None = None,
    budget: ContextBudget | None = None,
    prompt_set: PromptSet = V0,
) -> BuiltContext:
    """Контекст агента владельца для ответа на реплику ``query`` (её текст уже сохранён в
    диалоге). Ошибки: ``chat.OwnerGone`` (нет владельца/агента), ``LookupError`` (диалог не
    свой), ``ValueError`` (последняя реплика — не владельца). Ошибка эмбеддинга не роняет
    сборку: память — по свежести (``stats.embedding_error``)."""
    budget = budget or ContextBudget()
    system = system_prompt("dialog", prompt_set)
    vector: list[float] | None = None
    embedding_error = False
    if embedder is not None:
        try:
            vector = await _embed_one(embedder, query)
        except EmbeddingError:
            embedding_error = True
    rows = await chat.read_context(
        sm,
        keys,
        user_id,
        conversation_id,
        query_vector=vector,
        query_model=embedder.model if embedder is not None and vector is not None else None,
        memory_limit=budget.memory_items,
        turns_limit=turns_read_limit(budget.max_chars),
        pair_limit=budget.pair_candidates,
    )
    fragments: list[KnowledgeFragment] = []
    if knowledge is not None and budget.knowledge_items > 0:
        fragments = (await knowledge.search(query, vector=vector, limit=budget.knowledge_items))[
            : budget.knowledge_items
        ]
    pair = await _rank_pair(rows.pair, vector, embedder, budget.pair_messages)
    parts = _Parts(
        memory=[(m.id, clip(m.text, budget.item_chars)) for m in rows.memory],
        knowledge=[(f.id, clip(f.text, budget.item_chars)) for f in fragments],
        pair=[
            (
                p.id,
                PairMessage(
                    "outgoing" if p.outgoing else "incoming", clip(p.text, budget.item_chars)
                ),
            )
            for p in pair
        ],
        turns=_turns(rows, budget.turn_chars),
        profile=clip(rows.profile_summary or "", budget.profile_chars),
        summary=clip(rows.summary.text if rows.summary else "", budget.summary_chars),
    )
    safety_dialog = tuple(t for _, t in parts.turns if t.author == "owner")[-SAFETY_TURNS:]
    base = OwnerContext(owner_name=rows.owner_name, partner_name=rows.partner_name)
    context, dialog, dropped, over = fit(system.text, base, parts, budget.max_chars)
    memory_search: Literal["vector", "recency", "none"] = "none"
    if parts.memory:
        memory_search = "vector" if any(m.found_by == "vector" for m in rows.memory) else "recency"
    stats = ContextStats(
        chars=size(system.text, context, dialog),
        max_chars=budget.max_chars,
        turn_ids=tuple(i for i, _ in parts.turns),
        memory_ids=tuple(i for i, _ in parts.memory),
        memory_search=memory_search,
        knowledge_ids=tuple(i for i, _ in parts.knowledge),
        pair_message_ids=tuple(i for i, _ in parts.pair),
        has_profile=bool(context.profile_summary),
        has_dialog_summary=bool(context.dialog_summary),
        dropped=dropped,
        unavailable=dict(rows.unavailable),
        embedding_error=embedding_error,
        over_budget=over,
    )
    return BuiltContext(
        system=system,
        context=context,
        dialog=ContextDialog(dialog, safety_dialog),
        conversation_id=conversation_id,
        stats=stats,
        safety_dialog=safety_dialog,
    )
