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


# Коды, которые сервер реально отдаёт (app/api/auth.py, app/api/deps.py; тесты test_auth_api.py,
# test_onboarding.py). Ревью 1.2 MAJOR-1, тест 1.2 D-6: каждый должен быть объявлен в контракте.
SERVED_STATUSES = {
    ("post", "/auth/telegram"): {"200", "401", "403", "422", "503"},
    ("put", "/me/onboarding"): {"200", "401", "403", "422"},
}


def _resolve(spec: dict[str, Any], obj: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in obj:
        node: Any = spec
        for part in obj["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        obj = node
    return obj


def _example_codes(response: dict[str, Any]) -> list[str]:
    examples = response["content"]["application/json"]["examples"]
    return [e["value"]["code"] for e in examples.values()]


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _no_duplicates(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    keys = [loader.construct_object(k) for k, _ in node.value]
    dup = {k for k in keys if keys.count(k) > 1}
    assert not dup, (dup, node.start_mark.line + 1)
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def test_contract_has_no_duplicate_keys() -> None:
    """safe_load молча берёт последний из повторённых ключей — повтор скрывает правку."""
    yaml.load(SPEC_PATH.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)  # noqa: S506


def test_served_statuses_are_declared_in_contract() -> None:
    spec = _spec()
    for (method, path), statuses in SERVED_STATUSES.items():
        declared = set(spec["paths"][path][method]["responses"])
        assert statuses <= declared, (method, path, statuses - declared)


def test_onboarding_error_codes_are_described() -> None:
    """403 онбординга — код underage (с примером); onboarding_required — отдельный ответ для
    эндпоинтов, требующих онбординга. Оба кода — в общем списке Error.code."""
    spec = _spec()
    forbidden = _resolve(spec, spec["paths"]["/me/onboarding"]["put"]["responses"]["403"])
    assert _example_codes(forbidden) == ["underage"]
    required = _resolve(spec, {"$ref": "#/components/responses/OnboardingRequired"})
    assert _example_codes(required) == ["onboarding_required"]
    known = spec["components"]["schemas"]["Error"]["properties"]["code"]["examples"]
    assert {"underage", "onboarding_required", "unauthorized"} <= set(known)
    tz = spec["components"]["schemas"]["OnboardingRequest"]["properties"]["timezone"]
    assert tz["maxLength"] == 64  # колонка users.timezone — String(64)
