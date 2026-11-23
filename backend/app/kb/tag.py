"""Метки фрагментов базы знаний: тема и уровень проблемы §6.4 (задача 2.6).

Подход (ННО, ЭФТ, Готтман, теория привязанности…) задаёт администратор при загрузке
(``kb_documents.approach``). Тему (из словаря ``TOPICS``) и уровень 1–5 (шкала §6.4) ставит модель
``kb_tag.v0`` (черновик без прогона, АР-26) пачками фрагментов; ответ проверяется в коде:
неизвестные темы отбрасываются, уровень вне 1–5 — пусто. Любой сбой модели — пустые метки
(фрагмент всё равно индексируется и ищется по смыслу без фильтров)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.ai.calls import CallLog, complete, nullable, strict_object
from app.ai.llm import LLMError
from app.ai.models import AgentModels
from app.ai.prompts import load

TAG_VERSION = "v0"  # черновик без прогона (АР-26)
BATCH = 8
MAX_TOPICS = 3
# Словарь тем: темы обращений пар (ТЗ §4.2, §6.4), не диагнозы.
TOPICS: tuple[str, ...] = (
    "чувства",
    "потребности",
    "просьба",
    "обида",
    "ссора",
    "конфликт",
    "примирение",
    "доверие",
    "ревность",
    "измена",
    "расставание",
    "близость",
    "границы",
    "общение",
    "слушание",
    "быт",
    "деньги",
    "дети",
    "родственники",
    "работа",
    "усталость",
    "стресс",
    "привязанность",
    "безопасность",
)

TAG_SCHEMA: dict[str, Any] = strict_object(
    {
        "tags": {
            "type": "array",
            "items": strict_object(
                {
                    "ref": {"type": "string"},
                    "topics": {"type": "array", "items": {"type": "string", "enum": list(TOPICS)}},
                    "level": nullable({"type": "integer", "enum": [1, 2, 3, 4, 5]}),
                }
            ),
        }
    }
)


@dataclass(frozen=True)
class Tags:
    topics: tuple[str, ...] = ()
    level: int | None = None


EMPTY = Tags()


def _validate(refs: Sequence[str]) -> Any:
    def check(data: dict[str, Any]) -> dict[str, Tags]:
        items = data["tags"]
        if not isinstance(items, list):
            raise TypeError("tags")
        out: dict[str, Tags] = {}
        for item in items:
            if not isinstance(item, dict) or item.get("ref") not in refs:
                continue
            topics = item.get("topics")
            level = item.get("level")
            good = tuple(
                dict.fromkeys(t for t in topics if t in TOPICS) if isinstance(topics, list) else ()
            )[:MAX_TOPICS]
            ok_level = level if isinstance(level, int) and not isinstance(level, bool) else None
            out[str(item["ref"])] = Tags(good, ok_level if ok_level in range(1, 6) else None)
        return out

    return check


async def tag_chunks(
    models: AgentModels | None,
    chunks: Sequence[str],
    *,
    approach: str | None = None,
    log: CallLog | None = None,
    version: str = TAG_VERSION,
) -> list[Tags]:
    """Метки по порядку фрагментов; без модели или при сбое — пустые метки."""
    if models is None:
        return [EMPTY] * len(chunks)
    log = log if log is not None else CallLog()
    provider = models.summary if models.summary is not None else models.safety
    out: list[Tags] = []
    for start in range(0, len(chunks), BATCH):
        part = chunks[start : start + BATCH]
        refs = [f"f{i + 1}" for i in range(len(part))]
        try:
            got: dict[str, Tags] = await complete(
                provider,
                prompt=load("kb_tag", version),
                payload={
                    "approach": approach or "",
                    "fragments": [{"ref": r, "text": t} for r, t in zip(refs, part, strict=True)],
                },
                schema=TAG_SCHEMA,
                validate=_validate(refs),
                log=log,
                max_tokens=1500,
            )
        except LLMError:
            got = {}
        out += [got.get(r, EMPTY) for r in refs]
    return out
