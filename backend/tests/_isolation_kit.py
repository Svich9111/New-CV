"""Общий помощник набора выпуска 4.3 (A8): записывающая заглушка модели и эмбеддера, приложение с
заглушками и вход по initData. Используется только файлами ``test_isolation_release*.py``.

Заглушка ``Rec`` отвечает на все роли (роль — по полям схемы, ``app.ai.stub.role_of``) и
**повторяет в ответе все метки, которые увидела во входе** (``ECHO``): если чужая метка попала во
вход модели, она появится и в ответе — в репликах агента, черновиках, записях памяти, сводках, —
и утечка будет видна во всех следующих каналах, а не только во входе. «Послушный» режим
(``obedient``) выполняет внедрённые в текст команды так, как их понял бы послушный агент: отдаёт
черновик «я согласен», пишет записи памяти чужого вида, ссылается на чужое сообщение, цитирует
фрагмент базы знаний. Сеть не используется; данные выдуманные."""

from __future__ import annotations

import json
import re
import secrets
import string
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai.embeddings import HashingEmbedder
from app.ai.llm import ChatMessage, StructuredResult, Usage
from app.ai.models import AgentModels
from app.ai.stub import role_of
from app.config import Settings
from app.core.telegram import sign_init_data

V1 = "/api/v1"
BOT_TOKEN = "777043:A8-isolation-bot-token-not-real"
SESSION_SECRET = "a8-isolation-session-secret-" + "i" * 24
# Без цифр: контроль смысла сверяет числа оригинала и адаптации (``numbers_differ``).
MARK = re.compile(r"Qzx[a-z]{10}[A-Z]+")


def mark(kind: str) -> str:
    """Уникальная метка: ``Qzx`` + 10 случайных строчных латинских + вид (прописные). Своя на
    каждый тест (Б-71)."""
    assert kind.isalpha() and kind.isupper() and kind.isascii()
    return "Qzx" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(10)) + kind


def marks_in(raw: str) -> set[str]:
    return set(MARK.findall(raw))


def dump(obj: Any) -> str:
    return obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)


# =============================================================== заглушка модели


def _answer(reply: str, **kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"level": 2, "reply": reply, "draft_ready": False, "crisis_type": None,
                            "crisis_secondary": []}  # fmt: skip
    data.update(kw)
    return data


Decide = Callable[[str, dict[str, Any], str], dict[str, Any] | None]


@dataclass
class Rec:
    """Записывающая заглушка поставщика (все роли). ``draft_texts`` — тексты черновиков по
    порядку; ``threat`` — подстрока, на которой классификатор видит угрозу; ``decide`` — свой
    ответ роли (``None`` — ответ по умолчанию)."""

    name: str = "rec"
    model: str = "a8-iso-stub"
    draft_texts: list[str] = field(default_factory=list)
    threat: str = "пожалеешь"
    adapt_mark: str = ""
    decide: Decide | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def _record(self, role: str, system: str, messages: Sequence[ChatMessage]) -> str:
        self.calls.append({"role": role, "system": system,
                           "messages": [(m.role, m.content) for m in messages]})  # fmt: skip
        return " ".join(sorted(marks_in("\n".join(m.content for m in messages))))

    def _default(self, role: str, p: dict[str, Any], echo: str) -> dict[str, Any]:
        if role == "dialog":
            return _answer(f"Слышу тебя. {echo}".strip())
        if role == "draft":
            body = (self.draft_texts.pop(0) if self.draft_texts
                    else f"Мне важно, вариант {len(self.calls)}. {echo}".strip())  # fmt: skip
            return {"feeling": "Мне грустно", "situation": "планы меняются", "need": "ясность",
                    "request": "предупреждай заранее", "text": body}  # fmt: skip
        if role == "safety":
            hit = self.threat in p.get("text", "").lower()
            return {"category": "threat_or_insult" if hit else "none", "secondary": []}
        if role == "tone":
            harsh = "!" in p["original"]
            return {"tone": 2 if harsh else 0, "signals": ["exclamation"] if harsh else []}
        if role == "adapt":
            return {"adapted": f"{p['original'].replace('!', '.')} {self.adapt_mark}".strip(),
                    "reply_hint": None}  # fmt: skip
        if role == "meaning":
            return {"problems": []}
        if role == "summary":
            return {"summary": f"Сводка разговора. {echo}".strip()}
        if role == "memory_extract":
            items: list[dict[str, Any]] = []
            for m in p.get("partner_messages", []):
                items.append({"kind": "from_partner", "text": f"Партнёр просил: {dump(m['text'])}",
                              "event_date": None, "confidence": 0.8, "partner_ref": m["ref"]})  # fmt: skip
            if p.get("owner_turns"):
                items.append({"kind": "fact", "text": f"Владелец рассказал: {echo}".strip(),
                              "event_date": None, "confidence": 0.9, "partner_ref": None})  # fmt: skip
            return {"items": items}
        if role == "memory_merge":
            return {"same": False, "text": ""}
        if role == "profile_summary":
            return {"profile": f"Профиль владельца. {echo}".strip()}
        if role == "summary_forget":
            return {"rewritten": "Сводка без удалённой записи."}
        raise AssertionError(f"неожиданная роль {role}")

    async def complete_json(self, *, system: str, messages: Sequence[ChatMessage],
                            schema: Mapping[str, Any], max_tokens: int = 16000) -> StructuredResult:  # fmt: skip
        role = role_of(schema)
        echo = self._record(role, system, messages)
        payload = json.loads(messages[-1].content)
        data = self.decide(role, payload, echo) if self.decide is not None else None
        if data is None:
            data = self._default(role, payload, echo)
        return StructuredResult(data=data, model=self.model, usage=Usage(), latency_s=0.0,
                                cost_usd=0.0)  # fmt: skip

    async def stream_text(self, *, system: str, messages: Sequence[ChatMessage],
                          max_tokens: int = 16000) -> AsyncIterator[str]:  # fmt: skip
        echo = self._record("dialog_stream", system, messages)
        payload = json.loads(messages[-1].content)
        data = self.decide("dialog", payload, echo) if self.decide is not None else None
        raw = json.dumps(data if data is not None else _answer(f"Слышу тебя. {echo}".strip()),
                         ensure_ascii=False)  # fmt: skip
        for i in range(0, len(raw), 9):
            yield raw[i : i + 9]

    def text(self) -> str:
        """Всё, что ушло бы в модель: системные промпты и сообщения каждого вызова."""
        return "\n".join(c["system"] + "\n" + "\n".join(t for _, t in c["messages"])
                         for c in self.calls)  # fmt: skip

    def roles(self) -> list[str]:
        return [c["role"] for c in self.calls]

    def models(self) -> AgentModels:
        return AgentModels(dialog=self, draft=self, adapt=self, safety=self, tone=self,
                           meaning=self, summary=self)  # fmt: skip


class RecEmbedder:
    """Эмбеддер-заглушка (``HashingEmbedder``), который помнит каждый вход — он тоже уходит во
    внешнюю модель."""

    def __init__(self) -> None:
        self._inner = HashingEmbedder()
        self.inputs: list[str] = []

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def dim(self) -> int:
        return self._inner.dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.inputs.extend(texts)
        return await self._inner.embed(texts)


# =============================================================== приложение и вход


def settings() -> Settings:
    return Settings(telegram_bot_token=SecretStr(BOT_TOKEN),
                    session_secret=SecretStr(SESSION_SECRET), session_ttl_s=900,
                    bot_username="A8_Iso_bot", env="test")  # fmt: skip


@dataclass
class Person:
    label: str
    uid: uuid.UUID
    token: str
    agent: uuid.UUID = field(default_factory=lambda: uuid.UUID(int=0))

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


async def su(engine: AsyncEngine, sql: str, **params: object) -> Any:
    async with engine.begin() as c:
        res = await c.execute(text(sql), params)
        return res.all() if res.returns_rows else None


async def enter(http: httpx.AsyncClient, engine: AsyncEngine, label: str) -> Person:
    """Вход по initData и онбординг по HTTP, как в клиенте."""
    tg = 830_000_000_000 + secrets.randbelow(99_999_999_999)
    init = sign_init_data({"auth_date": str(int(time.time())),
                           "user": json.dumps({"id": tg, "first_name": label})}, BOT_TOKEN)  # fmt: skip
    r = await http.post(f"{V1}/auth/telegram", json={"init_data": init})
    assert r.status_code == 200, r.text
    p = Person(label, uuid.UUID(r.json()["me"]["id"]), r.json()["access_token"])
    r = await http.put(f"{V1}/me/onboarding", headers=p.h, json={
        "name": label, "address_form": "на ты", "partner_name": "Партнёр",
        "timezone": "Europe/Moscow", "consent_terms": True, "consent_data": True,
        "consent_sensitive": True, "age_confirmed": True,
    })  # fmt: skip
    assert r.status_code == 200, r.text
    p.agent = (await su(engine, "SELECT id FROM agents WHERE user_id = :u", u=p.uid))[0][0]
    return p


async def pair_up(engine: AsyncEngine, a: Person, b: Person, status: str = "active") -> uuid.UUID:
    """Пара суперпользователем (как в остальных тестах A8); распавшаяся — участники неактивны."""
    k = uuid.uuid4()
    active = status == "active"
    await su(engine, "INSERT INTO couples (id, status) VALUES (:k, CAST(:s AS couple_status))",
             k=k, s=status)  # fmt: skip
    await su(engine, "INSERT INTO couple_members (couple_id, user_id, role, active) VALUES "
             "(:k, :a, 'initiator', :act), (:k, :b, 'partner', :act)",
             k=k, a=a.uid, b=b.uid, act=active)  # fmt: skip
    return k
