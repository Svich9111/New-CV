"""0.7: контракт api/openapi.yaml валиден и покрывает пользовательские ФТ первой очереди."""

from collections.abc import Iterator
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


# Служебные маршруты вне контракта клиента Mini App: их вызывает не клиент, а внешняя система.
# Вебхук Telegram (задача 1.4) — подлинность по секретному заголовку
# X-Telegram-Bot-Api-Secret-Token (app/bot/webhook.py). Новые — только явным добавлением сюда.
SERVICE_ROUTES = {("POST", "/api/v1/telegram/webhook")}


def _served_routes(routes: Any, prefix: str = "") -> Iterator[tuple[str, frozenset[str]]]:
    """Все маршруты приложения с полными путями. FastAPI ≥ 0.165 не разворачивает
    include_router в app.routes (объект _IncludedRouter без path) — без обхода проверка
    видела бы только /health."""
    for route in routes:
        ctx = getattr(route, "include_context", None)
        if ctx is not None:
            yield from _served_routes(ctx.included_router.routes, prefix + ctx.prefix)
        elif hasattr(route, "path"):
            yield prefix + route.path, frozenset(getattr(route, "methods", None) or ())


def test_implemented_routes_exist_in_contract() -> None:
    """Сервер не публикует ничего сверх контракта, кроме явно перечисленных служебных маршрутов."""
    paths = _spec()["paths"]
    seen_service = set()
    served = list(_served_routes(app.routes))
    assert ("/api/v1/auth/telegram", frozenset({"POST"})) in served  # обход видит include_router
    for path, methods in served:
        if not path.startswith("/api/v1"):
            continue
        rel = path.removeprefix("/api/v1")
        for method in methods - {"HEAD", "OPTIONS"}:
            if (method, path) in SERVICE_ROUTES:
                seen_service.add((method, path))
                continue
            assert method.lower() in paths.get(rel, {}), (method, path)
    # Список не устаревает: каждый служебный маршрут действительно есть на сервере.
    assert seen_service == SERVICE_ROUTES


def test_service_routes_are_not_in_client_contract() -> None:
    paths = _spec()["paths"]
    for _, path in SERVICE_ROUTES:
        assert path.removeprefix("/api/v1") not in paths


# Коды, которые сервер реально отдаёт (app/api/auth.py, app/api/deps.py; тесты test_auth_api.py,
# test_onboarding.py). Ревью 1.2 MAJOR-1, тест 1.2 D-6: каждый должен быть объявлен в контракте.
SERVED_STATUSES = {
    ("post", "/auth/telegram"): {"200", "401", "403", "422", "503"},
    ("put", "/me/onboarding"): {"200", "401", "403", "422"},
    # 1.3 (app/api/couple.py; ревью 1.3 MAJOR-1, тест 1.3 D-1). 503 — нет имени бота.
    ("get", "/couple"): {"200", "401", "403", "404"},
    ("post", "/couple"): {"201", "401", "403", "409", "503"},
    ("post", "/couple/invite"): {"201", "401", "403", "404", "409", "503"},
    # АР-17: выход — после входа, без требования онбординга (403 не отдаётся).
    ("post", "/couple/leave"): {"204", "401", "404"},
    ("get", "/invites/{token}"): {"200", "401", "404", "410"},
    ("post", "/invites/{token}/accept"): {"200", "401", "403", "404", "409", "410"},
}


def test_leave_does_not_declare_onboarding_required() -> None:
    """АР-17: выход из пары не требует онбординга — 403 в контракте не объявлен."""
    responses = _spec()["paths"]["/couple/leave"]["post"]["responses"]
    assert "403" not in responses


def test_couple_error_codes_are_listed() -> None:
    """Коды ошибок, которые отдают эндпоинты пары (app/api/couple.py), — в Error.code и в
    примерах соответствующих ответов (ревью 1.3 MINOR-1)."""
    spec = _spec()
    known = set(spec["components"]["schemas"]["Error"]["properties"]["code"]["examples"])
    served = {
        "not_found",
        "own_invite",
        "couple_not_pending",
        "already_in_couple",
        "invite_expired",
        "invite_used",
        "onboarding_required",
        "unauthorized",
        "service_unavailable",
    }
    assert served <= known, served - known
    responses = spec["components"]["responses"]
    by_response = {
        "NotFound": ["not_found"],
        "Conflict": ["already_in_couple", "own_invite", "couple_not_pending"],
        "Gone": ["invite_expired", "invite_used"],
        "InvitesUnavailable": ["service_unavailable"],
    }
    for name, codes in by_response.items():
        assert _example_codes(responses[name]) == codes, name


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
