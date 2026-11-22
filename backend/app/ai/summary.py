"""Сжатие истории личного чата (ТЗ §6.1 п.5: «последние реплики и краткое содержание
предыдущих»; задача 2.1).

Когда реплик после отметки сводки становится больше ``CompressionPolicy.trigger_turns`` или их
текст длиннее ``trigger_chars``, все реплики, кроме последних ``keep_turns``, вместе с прежней
сводкой сжимаются моделью (промпт ``summary.v0`` — черновик без прогона, АР-26) в новую сводку.
Она записывается шифрованной в ``conversations.summary_enc`` с отметкой последней вошедшей
реплики (``repositories.chat``): реплики до отметки в контекст больше не идут.

Запуск — после ответа агента, вне потока ответа (фоновая задача API, этап 2): сжатие не
задерживает первый символ. Контекст не превышает бюджет и без сжатия (``context.fit``), поэтому
ошибка модели здесь не ошибка для пользователя: сводка остаётся прежней, повтор — при следующей
реплике. Удалённый владелец — штатный исход (Б-44). Журналов с текстами модуль не пишет."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.calls import CallLog, CallRecord, complete, strict_object, text_field
from app.ai.llm import LLMError
from app.ai.models import AgentModels
from app.ai.prompts import Prompt, load
from app.core.crypto import Keyring
from app.repositories import chat
from app.repositories.chat import HistorySummary, OwnerGone
from app.repositories.texts import ConversationText

SUMMARY_VERSION = "v0"  # черновик без прогона (АР-26)
SUMMARY_SCHEMA: dict[str, Any] = strict_object({"summary": {"type": "string"}})
MAX_TOKENS_SUMMARY = 1500


@dataclass(frozen=True)
class CompressionPolicy:
    trigger_turns: int = 40
    trigger_chars: int = 16_000
    keep_turns: int = 12
    summary_max_chars: int = 2_000  # жёсткий предел в коде (промпт просит до 1200)
    # Пачка одного раунда (ревью MAJOR-2): самые старые реплики, не больше ``batch_turns`` и
    # ``batch_chars`` (но хотя бы одна). После долгой серии сбоев хвост догоняется несколькими
    # раундами (``rounds`` за вызов и следующие вызовы), вход модели не растёт без предела.
    batch_turns: int = 40
    batch_chars: int = 20_000
    rounds: int = 3

    def __post_init__(self) -> None:
        if self.keep_turns < 1 or self.trigger_turns <= self.keep_turns:
            raise ValueError("keep_turns ≥ 1 и trigger_turns > keep_turns")
        if self.batch_turns < 1 or self.batch_chars < 1 or self.rounds < 1:
            raise ValueError("batch_turns, batch_chars, rounds ≥ 1")


@dataclass(frozen=True)
class CompressionResult:
    status: Literal["skipped", "saved", "conflict", "error", "gone"]
    summarized_turns: int = 0
    error: str | None = None  # вид ошибки модели (LLMError.kind), без текста
    rounds: int = 0  # сколько раундов записано
    more: bool = False  # хвост ещё длиннее порога — следующий вызов продолжит
    calls: tuple[CallRecord, ...] = ()
    prompt: str | None = None


def summary_prompt(version: str = SUMMARY_VERSION) -> Prompt:
    return load("summary", version)


def needs_compression(turns: list[ConversationText], policy: CompressionPolicy) -> bool:
    chars = sum(len(t.text or "") for t in turns)
    return len(turns) > policy.trigger_turns or (
        chars > policy.trigger_chars and len(turns) > policy.keep_turns
    )


def batch(turns: list[ConversationText], policy: CompressionPolicy) -> list[ConversationText]:
    """Пачка раунда: самые старые реплики вне последних ``keep_turns``, в пределах пачки."""
    return _take(turns[: -policy.keep_turns], policy)


def _take(old: list[ConversationText], policy: CompressionPolicy) -> list[ConversationText]:
    """Первые реплики в пределах ``batch_turns`` и ``batch_chars`` (но хотя бы одна)."""
    out: list[ConversationText] = []
    chars = 0
    for t in old[: policy.batch_turns]:
        size = len(t.text or "")
        if out and chars + size > policy.batch_chars:
            break
        out.append(t)
        chars += size
    return out


def read_limit(policy: CompressionPolicy) -> int:
    """Сколько первых реплик после отметки читать за раунд: хватает и на пачку, и на решение
    «пора ли» (если хвост не длиннее порога, он прочитан целиком) — R2-MINOR-3."""
    return max(policy.trigger_turns, policy.batch_turns)


def plan(
    total: int, head: list[ConversationText], policy: CompressionPolicy
) -> list[ConversationText] | None:
    """Пачка раунда по числу реплик после отметки (``total``) и первым из них (``head``,
    ``read_limit``); ``None`` — сжимать не пора."""
    if total > policy.trigger_turns:
        return _take(head[: max(total - policy.keep_turns, 0)], policy) or None
    if not needs_compression(head, policy):
        return None
    return batch(head, policy) or None


def summary_payload(previous: str, turns: list[ConversationText]) -> dict[str, Any]:
    """Вход модели: прежняя сводка и старые реплики (нерасшифрованные пропускаются)."""
    return {
        "previous_summary": previous,
        "dialog": [
            {"from": "owner" if t.author == "owner" else "agent", "text": t.text}
            for t in turns
            if t.text
        ],
    }


async def summarize(
    models: AgentModels,
    previous: str,
    turns: list[ConversationText],
    *,
    log: CallLog,
    version: str = SUMMARY_VERSION,
    max_chars: int = CompressionPolicy.summary_max_chars,
) -> str:
    """Новая сводка (``LLMError`` — как у слоя модели)."""
    prompt = summary_prompt(version)

    def validate(data: dict[str, Any]) -> str:
        value = text_field(data, "summary")
        return value if len(value) <= max_chars else value[: max_chars - 1].rstrip() + "…"

    provider = models.summary if models.summary is not None else models.safety
    return await complete(
        provider,
        prompt=prompt,
        payload=summary_payload(previous, turns),
        schema=SUMMARY_SCHEMA,
        validate=validate,
        log=log,
        max_tokens=MAX_TOKENS_SUMMARY,
    )


async def compress_history(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    models: AgentModels,
    *,
    policy: CompressionPolicy | None = None,
    version: str = SUMMARY_VERSION,
) -> CompressionResult:
    """Сжать историю, если пора. Одновременное сжатие тем же диалогом — ``conflict`` (запись
    сравнивает отметку, ничего не теряется); ошибка модели — ``error`` (сводка прежняя)."""
    policy = policy or CompressionPolicy()
    prompt_name = summary_prompt(version).name
    log = CallLog()
    done_rounds = 0
    summarized = 0
    for _ in range(policy.rounds):
        try:
            summary, total, head = await chat.read_history_head(
                sm, keys, user_id, conversation_id, limit=read_limit(policy)
            )
        except (OwnerGone, LookupError):
            return CompressionResult(
                "gone", summarized, calls=tuple(log.records), rounds=done_rounds
            )
        old = plan(total, head, policy)
        if old is None:
            status: Literal["skipped", "saved"] = "saved" if done_rounds else "skipped"
            return CompressionResult(
                status,
                summarized,
                calls=tuple(log.records),
                prompt=prompt_name if done_rounds else None,
                rounds=done_rounds,
            )
        try:
            text = await summarize(
                models,
                summary.text if summary else "",
                old,
                log=log,
                version=version,
                max_chars=policy.summary_max_chars,
            )
        except LLMError as exc:
            return CompressionResult(
                "error",
                summarized,
                error=exc.kind,
                calls=tuple(log.records),
                prompt=prompt_name,
                rounds=done_rounds,
                more=True,
            )
        last = old[-1]
        saved = await chat.save_history_summary(
            sm,
            keys,
            user_id,
            conversation_id,
            HistorySummary(text, last.created_at, last.id),
            expected_through_id=summary.through_id if summary else None,
        )
        if not saved:
            return CompressionResult(
                "conflict" if not done_rounds else "saved",
                summarized,
                calls=tuple(log.records),
                prompt=prompt_name,
                rounds=done_rounds,
            )
        done_rounds += 1
        summarized += len(old)
    try:
        _, total, head = await chat.read_history_head(
            sm, keys, user_id, conversation_id, limit=read_limit(policy)
        )
        more = plan(total, head, policy) is not None
    except (OwnerGone, LookupError):
        more = False
    return CompressionResult(
        "saved",
        summarized,
        calls=tuple(log.records),
        prompt=prompt_name,
        rounds=done_rounds,
        more=more,
    )
