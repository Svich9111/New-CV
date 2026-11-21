"""0.7: контракт api/openapi.yaml валиден и покрывает пользовательские ФТ первой очереди."""

from pathlib import Path
from typing import Any

import yaml
from openapi_spec_validator import validate

from app.main import app

SPEC_PATH = Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"


def _spec() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    return data


def test_contract_is_valid_openapi() -> None:
    validate(_spec())


# ФТ → (метод, путь). ФТ-12, 14, 16, 17 (кроме контактов), 18 — серверные процессы без эндпоинтов.
FT_ENDPOINTS = {
    "ФТ-01": ("post", "/auth/telegram"),
    "ФТ-02": ("put", "/me/onboarding"),
    "ФТ-03": ("post", "/invites/{token}/accept"),
    "ФТ-04": ("post", "/couple/leave"),
    "ФТ-05": ("post", "/chat/messages"),
    "ФТ-06": ("post", "/chat/voice"),
    "ФТ-07": ("post", "/drafts/{draft_id}/regenerate"),
    "ФТ-08": ("post", "/drafts/{draft_id}/send"),
    "ФТ-10": ("get", "/couple/messages/{message_id}/original"),
    "ФТ-11": ("get", "/couple/messages"),
    "ФТ-13": ("delete", "/memory/{memory_id}"),
    "ФТ-15": ("post", "/admin/kb/documents"),
    "ФТ-17": ("get", "/help/contacts"),
    "ФТ-19": ("get", "/subscription"),
    "ФТ-20": ("get", "/admin/metrics"),
    "ФТ-21": ("delete", "/me"),
}


def test_contract_covers_functional_requirements() -> None:
    paths = _spec()["paths"]
    missing = [ft for ft, (m, p) in FT_ENDPOINTS.items() if m not in paths.get(p, {})]
    assert missing == []


def test_only_auth_and_health_are_public() -> None:
    public = [
        (m, p)
        for p, ops in _spec()["paths"].items()
        for m, op in ops.items()
        if isinstance(op, dict) and op.get("security") == []
    ]
    assert sorted(public) == [("get", "/health"), ("post", "/auth/telegram")]


def test_implemented_routes_exist_in_contract() -> None:
    """Сервер не публикует ничего сверх контракта."""
    paths = _spec()["paths"]
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1"):
            continue
        rel = path.removeprefix("/api/v1")
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
            assert method.lower() in paths.get(rel, {}), (method, path)
