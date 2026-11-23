"""Контакты служб помощи (ТЗ §9.3, §11 п.5; ФТ-17; АР-33).

Источник — файл ``app/help_contacts.json`` в репозитории, а не БД: список нужен и тогда, когда
БД недоступна (событие ``crisis`` в ``POST /chat/messages`` отдаёт его целиком). Читается один
раз при импорте модуля и проверяется: хотя бы одна запись, у каждой непустые ``name``,
``phone`` и ``description``. Порядок записей — порядок файла; тот же список и в том же порядке
отдают ``GET /help/contacts`` и событие ``crisis`` (контракт ``ChatStreamCrisis.contacts``).

До проверки оператором в файле только номер 112 и пометка ``verified_by_operator: false``
(АР-33); запуск в продуктив без проверенного списка запрещён — ``check_help_contacts_config``
не даёт стартовать сервису с ``FEELINGS_ENV`` вне dev/test/local."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import ConfigError, Settings

HELP_CONTACTS_PATH = Path(__file__).resolve().parent / "help_contacts.json"
NON_PRODUCTION_ENVS = frozenset({"dev", "test", "local"})


@dataclass(frozen=True)
class HelpContacts:
    contacts: tuple[dict[str, Any], ...]
    verified_by_operator: bool


def parse_help_contacts(raw: str) -> HelpContacts:
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("contacts"), list):
        raise ConfigError("help_contacts.json: нет списка contacts")
    contacts: list[dict[str, Any]] = []
    for item in data["contacts"]:
        if not isinstance(item, dict):
            raise ConfigError("help_contacts.json: запись — не объект")
        entry: dict[str, Any] = {}
        for key in ("name", "phone", "description"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"help_contacts.json: пустое поле {key}")
            entry[key] = value
        if "round_the_clock" in item:
            if not isinstance(item["round_the_clock"], bool):
                raise ConfigError("help_contacts.json: round_the_clock — не логическое")
            entry["round_the_clock"] = item["round_the_clock"]
        contacts.append(entry)
    if not contacts:
        raise ConfigError("help_contacts.json: список пуст (кризисный ответ — с контактами всегда)")
    return HelpContacts(tuple(contacts), data.get("verified_by_operator") is True)


HELP_CONTACTS = parse_help_contacts(HELP_CONTACTS_PATH.read_text(encoding="utf-8"))


def help_contacts() -> list[dict[str, Any]]:
    """Копия списка (вызывающий может её менять, общий список — нет)."""
    return [dict(c) for c in HELP_CONTACTS.contacts]


def check_help_contacts_config(settings: Settings, contacts: HelpContacts = HELP_CONTACTS) -> None:
    """АР-33: в продуктив — только с проверенным оператором списком."""
    if settings.env not in NON_PRODUCTION_ENVS and not contacts.verified_by_operator:
        raise ConfigError("app/help_contacts.json is not verified by the operator (АР-33)")
