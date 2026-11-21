"""Загрузка промптов агентов по версии (задача 0.5; правила версий — ``prompts/README.md``).

Файл промпта — ``prompts/<роль>.v<N>.md``: заголовок для людей и блоки
``<!-- prompt:<блок> --> … <!-- /prompt:<блок> -->``; модели отправляется только текст блока.
Каталог — ``FEELINGS_PROMPTS_DIR`` или ``prompts/`` в корне репозитория.

Набор версий, с которым работает код, — ``PromptSet``: версия каждой роли фиксируется в
результате вызова (по ней видно, каким промптом сделан ответ; прогон ``evals/`` — по набору)."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR_ENV = "FEELINGS_PROMPTS_DIR"
DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"

ROLES = ("dialog", "draft", "safety", "tone", "adapt", "meaning", "crisis")
_VERSION = re.compile(r"^v[0-9]+$")


class PromptError(Exception):
    """Нет файла или блока промпта — ошибка настройки, а не модели."""


@dataclass(frozen=True)
class Prompt:
    role: str
    version: str
    text: str

    @property
    def name(self) -> str:
        return f"{self.role}.{self.version}"

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class PromptSet:
    """Версии промптов по ролям. ``V0`` — первая версия промптов продукта (задача 0.5)."""

    dialog: str = "v0"
    draft: str = "v0"
    safety: str = "v0"
    tone: str = "v0"
    adapt: str = "v0"
    meaning: str = "v0"
    crisis: str = "v0"

    def version(self, role: str) -> str:
        if role not in ROLES:
            raise PromptError(f"неизвестная роль промпта {role!r}")
        return str(getattr(self, role))

    def label(self) -> str:
        """Короткая метка набора: «v0», если все роли одной версии, иначе по ролям."""
        versions = {self.version(r) for r in ROLES}
        if len(versions) == 1:
            return versions.pop()
        return ",".join(f"{r}.{self.version(r)}" for r in ROLES)


V0 = PromptSet()
# Черновики без прогона эталонного набора (DECISIONS АР-26): не по умолчанию, не в 3.2/продукт до
# прогона без регрессий. V0 — только прототип 0.5 (консоль, набор), тоже не в 3.2 до «не хуже».
# V1: safety.v1 (уже «насилие»), meaning.v1 (искажение — с фрагментами).
V1 = PromptSet(safety="v1", meaning="v1")
# V2 (ревью 0.5 раунд 1): safety.v2 (кто действует — автор или другой), meaning.v2
# (added_feeling, проверка подсказки), adapt.v1 (чувство — только из оригинала).
V2 = PromptSet(safety="v2", meaning="v2", adapt="v1")
DRAFT_SETS: tuple[PromptSet, ...] = (V1, V2)  # статус «черновик без прогона» (АР-26)


def prompts_dir() -> Path:
    return Path(os.environ.get(PROMPTS_DIR_ENV) or DEFAULT_PROMPTS_DIR)


@lru_cache(maxsize=64)
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(f"нет файла промпта {path.name}") from exc


def load(role: str, version: str, block: str = "system", directory: Path | None = None) -> Prompt:
    """Текст блока ``block`` из ``<роль>.<версия>.md``."""
    if role not in ROLES:
        raise PromptError(f"неизвестная роль промпта {role!r}")
    if not _VERSION.match(version):
        raise PromptError(f"версия промпта {version!r} — не vN")
    path = (directory or prompts_dir()) / f"{role}.{version}.md"
    text = _read(path)
    match = re.search(
        rf"<!-- prompt:{re.escape(block)} -->\n(.*?)\n<!-- /prompt:{re.escape(block)} -->",
        text,
        flags=re.DOTALL,
    )
    if not match or not match.group(1).strip():
        raise PromptError(f"{path.name}: нет блока prompt:{block}")
    return Prompt(role=role, version=version, text=match.group(1).strip())


def system_prompt(role: str, prompt_set: PromptSet = V0) -> Prompt:
    return load(role, prompt_set.version(role))
