"""0.7: контракт api/openapi.yaml валиден и покрывает пользовательские ФТ первой очереди."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import yaml
from openapi_spec_validator import validate

from app.help import help_contacts
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
    "ФТ-19": ("get", "/couple/subscription"),  # 4.4: подписка — свойство пары
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
    # /help/contacts — публичный (Б-58, задача 2.0v): помощь доступна и без сессии (ТЗ §9.3).
    assert sorted(public) == [
        ("get", "/health"),
        ("get", "/help/contacts"),
        ("post", "/auth/telegram"),
    ]


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
    # 409 conflict_retry — 40P01/40001 (АР-24, общий обработчик app/errors.py).
    ("post", "/couple/leave"): {"204", "401", "404", "409"},
    ("get", "/invites/{token}"): {"200", "401", "404", "410"},
    ("post", "/invites/{token}/accept"): {"200", "401", "403", "404", "409", "410"},
    # 4.5 этап 2 (app/api/account.py): без требования онбординга (АР-17, АР-25) — 403 нет;
    # 409 conflict_retry — 40P01/40001 (АР-24, общий обработчик app/errors.py).
    ("delete", "/me"): {"204", "401", "409"},
    ("get", "/me/export"): {"200", "401", "409"},
    # 2.1 этап 2 (app/api/chat.py, tests/test_chat_api.py; АР-32). 402 не отдаётся до этапа 4;
    # 409 conflict_retry — только из транзакции сохранения реплики владельца (АР-24).
    ("post", "/chat/messages"): {"200", "401", "403", "409", "422", "429"},
    ("get", "/chat/messages"): {"200", "401", "403", "422"},
    # АР-33 (app/api/help.py): без онбординга и подписки; с 2.0v — и без сессии (`security: []`,
    # Б-58), 401 не отдаётся.
    ("get", "/help/contacts"): {"200"},
    # 2.4 этап 2 (app/api/memory.py, tests/test_memory_api.py; АР-36): без онбординга (АР-17,
    # АР-25) — 403 нет; 401 — вход по общей схеме bearer, 422 у GET — неверный `type`.
    ("get", "/memory"): {"200", "401", "422"},
    ("delete", "/memory/{memory_id}"): {"204", "401", "404"},
    # 2.6 этап 2 (app/api/admin_kb.py, tests/test_admin_kb_api.py; АР-39): вход администратора —
    # 401 (нет/неизвестный токен), 403 admin_only (сессия Mini App); 413 — и предел тела запроса.
    ("get", "/admin/kb/documents"): {"200", "401", "403"},
    ("post", "/admin/kb/documents"): {"202", "401", "403", "409", "413", "415", "422"},
    ("delete", "/admin/kb/documents/{document_id}"): {"204", "401", "403", "404"},
    # 3.3 (app/api/couple_messages.py, tests/test_couple_messages_api.py; АР-11, АР-25, АР-40):
    # все три — 401 и 403 onboarding_required; лента — 422 (курсор, limit); «прочитано» — 404.
    ("get", "/couple/messages"): {"200", "401", "403", "422"},
    ("get", "/couple/messages/{message_id}/original"): {"200", "401", "403", "404", "409"},
    ("post", "/couple/messages/{message_id}/read"): {"204", "401", "403", "404"},
    # 3.1 (app/api/drafts.py, tests/test_drafts_api.py; АР-40). 402 у send — с этапа 4.
    ("post", "/drafts"): {"200", "201", "401", "403", "409", "422", "503"},
    ("get", "/drafts/{draft_id}"): {"200", "401", "403", "404"},
    ("patch", "/drafts/{draft_id}"): {"200", "401", "403", "404", "409", "422"},
    ("post", "/drafts/{draft_id}/regenerate"): {"200", "401", "403", "404", "409", "422", "503"},
    ("post", "/drafts/{draft_id}/reject"): {"204", "401", "403", "404", "409"},
    ("post", "/drafts/{draft_id}/send"): {"202", "401", "403", "404", "409", "422"},
}


def test_couple_messages_declare_served_errors() -> None:
    paths = _spec()["paths"]
    assert {"401", "403", "422"} <= set(paths["/couple/messages"]["get"]["responses"])
    original = paths["/couple/messages/{message_id}/original"]["get"]["responses"]
    assert {"401", "403"} <= set(original)
    read = paths["/couple/messages/{message_id}/read"]["post"]["responses"]
    assert {"401", "403", "404"} <= set(read)


def test_couple_messages_allow_unavailable_text_and_no_store() -> None:
    spec = _spec()
    text_schema = spec["components"]["schemas"]["PartnerMessage"]["properties"]["text"]
    assert text_schema.get("$ref") == "#/components/schemas/ExportText"
    feed_200 = spec["paths"]["/couple/messages"]["get"]["responses"]["200"]
    assert feed_200["headers"]["Cache-Control"]["schema"]["const"] == "no-store"


def test_memory_declares_401_and_422() -> None:
    paths = _spec()["paths"]
    assert {"401", "422"} <= set(paths["/memory"]["get"]["responses"])
    assert "401" in paths["/memory/{memory_id}"]["delete"]["responses"]


def test_help_contacts_is_public_in_contract() -> None:
    """Б-58 (задача 2.0v): контакты помощи доступны без сессии — путь публичный, 401/403 не
    объявлены (ТЗ §9.3 «всегда»; прежний страж `test_help_contacts_declares_401` снят)."""
    op = _spec()["paths"]["/help/contacts"]["get"]
    assert op["security"] == []
    assert set(op["responses"]) == {"200"}


async def test_help_contacts_served_without_session() -> None:
    """Сервер следует контракту: без заголовка и с негодным токеном — 200 и тот же список."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for headers in ({}, {"Authorization": "Bearer nope"}):
            resp = await client.get("/api/v1/help/contacts", headers=headers)
            assert resp.status_code == 200, (headers, resp.status_code)
            assert resp.json() == help_contacts()
            assert resp.headers["Cache-Control"] == "no-store"


def test_account_endpoints_do_not_declare_onboarding_required() -> None:
    """АР-25: удаление и выгрузка доступны без онбординга; 409 — ответ ConflictRetry."""
    paths = _spec()["paths"]
    for method, path in (("delete", "/me"), ("get", "/me/export")):
        responses = paths[path][method]["responses"]
        assert "403" not in responses, path
        assert responses["409"] == {"$ref": "#/components/responses/ConflictRetry"}, path
    export_200 = paths["/me/export"]["get"]["responses"]["200"]
    assert export_200["headers"]["Cache-Control"]["schema"]["const"] == "no-store"


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


def test_chat_stream_contract() -> None:
    """2.1 этап 2 (АР-32): события SSE личного чата, коды ошибок, ответы до потока, история."""
    spec = _spec()
    schemas = spec["components"]["schemas"]
    post = spec["paths"]["/chat/messages"]["post"]
    stream = post["responses"]["200"]
    assert stream["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    events = {
        _resolve(spec, ref)["x-sse-event"]: _resolve(spec, ref)
        for ref in stream["content"]["text/event-stream"]["schema"]["oneOf"]
    }
    assert {name: set(s["required"]) for name, s in events.items()} == {
        "delta": {"text"},
        "done": {"message_id", "suggest_draft"},
        "crisis": {"message_id", "text", "replace", "contacts"},
        "error": {"code", "message"},
    }
    assert all(s["additionalProperties"] is False for s in events.values())
    contacts = events["crisis"]["properties"]["contacts"]
    assert contacts["items"] == {"$ref": "#/components/schemas/HelpContact"}
    assert contacts["minItems"] == 1  # кризис — с контактами всегда (§9.3)
    assert schemas["ChatErrorCode"]["enum"] == [
        "refusal",
        "unavailable",
        "quota",
        "bad_output",
        "internal",
    ]
    responses = post["responses"]
    assert {"200", "401", "402", "403", "409", "422", "429"} <= set(responses)
    assert responses["403"] == {"$ref": "#/components/responses/OnboardingRequired"}
    assert responses["409"] == {"$ref": "#/components/responses/ConflictRetry"}
    assert "Не отдаётся до этапа 4" in responses["402"]["description"]
    limited = _resolve(spec, responses["429"])
    assert limited["headers"]["Retry-After"]["required"] is True
    assert _example_codes(limited) == ["rate_limited"]
    assert "402 и 429 в этом случае не возвращаются" in post["description"]
    known = set(schemas["Error"]["properties"]["code"]["examples"])
    assert {"rate_limited", "subscription_required", "validation_error", "conflict_retry"} <= known

    get = spec["paths"]["/chat/messages"]["get"]
    assert get["responses"]["200"]["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    assert get["responses"]["403"] == {"$ref": "#/components/responses/OnboardingRequired"}
    message = schemas["ChatMessage"]
    assert message["properties"]["author"]["enum"] == ["owner", "agent"]
    assert message["properties"]["text"]["$ref"] == "#/components/schemas/ExportText"


# --- 3.1: черновики (АР-40) ------------------------------------------------------------------

DRAFT_OPERATIONS = (
    ("post", "/drafts"),
    ("get", "/drafts/{draft_id}"),
    ("patch", "/drafts/{draft_id}"),
    ("post", "/drafts/{draft_id}/regenerate"),
    ("post", "/drafts/{draft_id}/reject"),
    ("post", "/drafts/{draft_id}/send"),
)


def test_drafts_contract() -> None:
    """3.1 (АР-40): кризис — 2xx с протоколом и контактами (клиент всегда показывает помощь),
    различается по `kind`; ошибки черновика и модели объявлены; версии в правке и отправке."""
    spec = _spec()
    paths, schemas = spec["paths"], spec["components"]["schemas"]
    create = paths["/drafts"]["post"]["responses"]
    assert create["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Draft"
    }
    assert create["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DraftCrisis"
    }
    regen = paths["/drafts/{draft_id}/regenerate"]["post"]["responses"]
    assert regen["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DraftOutcome"
    }
    outcome = schemas["DraftOutcome"]
    assert outcome["discriminator"]["propertyName"] == "kind"
    assert set(outcome["discriminator"]["mapping"]) == {"draft", "crisis"}
    crisis = schemas["DraftCrisis"]
    assert crisis["properties"]["kind"]["const"] == "crisis"
    assert crisis["properties"]["contacts"]["minItems"] == 1
    assert crisis["properties"]["contacts"]["items"] == {"$ref": "#/components/schemas/HelpContact"}
    assert {"kind", "text", "contacts"} <= set(crisis["required"])
    assert schemas["Draft"]["properties"]["kind"]["const"] == "draft"
    for method, path in DRAFT_OPERATIONS:
        responses = paths[path][method]["responses"]
        assert responses["401"] == {"$ref": "#/components/responses/Unauthorized"}, path
        assert responses["403"] == {"$ref": "#/components/responses/OnboardingRequired"}, path
    for path in ("/drafts", "/drafts/{draft_id}/regenerate"):
        responses = paths[path]["post"]["responses"]
        assert responses["503"] == {"$ref": "#/components/responses/DraftModelError"}
        assert responses["422"] == {"$ref": "#/components/responses/DraftInvalid"}
    patch_body = paths["/drafts/{draft_id}"]["patch"]["requestBody"]["content"]
    assert patch_body["application/json"]["schema"]["required"] == ["text", "expected_version"]
    send = paths["/drafts/{draft_id}/send"]["post"]
    assert send["requestBody"]["content"]["application/json"]["schema"]["required"] == [
        "expected_version"
    ]
    assert "processing" in send["description"] and "АР-40" in send["description"]
    known = set(schemas["Error"]["properties"]["code"]["examples"])
    assert {
        "not_editable",
        "no_turns",
        "bad_reply_target",
        "draft_quote",
        "same_variant",
        "llm_unavailable",
        "llm_refusal",
        "version_conflict",
        "couple_not_active",
    } <= known


def test_drafts_are_served() -> None:
    served = {(m, p) for p, ms in _served_routes(app.routes) for m in ms}
    for method, path in DRAFT_OPERATIONS:
        assert (method.upper(), "/api/v1" + path) in served


# --- 4.4: подписка и оплата Telegram Stars (АР-50) --------------------------------------------


def test_subscription_contract() -> None:
    """4.4 этап 1 (A1): статус, срок и лимиты — обоим партнёрам; счёт для openInvoice с ценой
    из настроек сервера; 402/409/429/503 объявлены; старые пути /subscription сняты."""
    spec = _spec()
    paths, schemas = spec["paths"], spec["components"]["schemas"]
    assert "/subscription" not in paths and "/subscription/invoice" not in paths
    get = paths["/couple/subscription"]["get"]
    assert get["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Subscription"
    }
    assert get["responses"]["200"]["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    assert get["responses"]["403"] == {"$ref": "#/components/responses/OnboardingRequired"}
    sub = schemas["Subscription"]
    assert sub["additionalProperties"] is False
    assert set(sub["required"]) == {"status", "has_access", "trial_used", "limits", "plans"}
    assert sub["properties"]["status"]["enum"] == ["none", "trial", "active", "expired", "canceled"]
    limits = schemas["SubscriptionLimits"]
    assert set(limits["required"]) == {
        "chat_daily_limit", "chat_used_today", "window_resets_at", "can_send_messages",
    }  # fmt: skip
    offer = schemas["SubscriptionOffer"]
    assert offer["properties"]["currency"]["const"] == "XTR"
    post = paths["/couple/subscription/invoice"]["post"]
    body = post["requestBody"]["content"]["application/json"]["schema"]
    # Цену клиент не передаёт — только тариф (Д6: цена — в настройках).
    assert body["required"] == ["plan"] and set(body["properties"]) == {"plan"}
    assert body["additionalProperties"] is False
    assert schemas["SubscriptionPlan"]["enum"] == ["month", "year"]
    created = post["responses"]["201"]["content"]["application/json"]["schema"]
    assert created == {"$ref": "#/components/schemas/SubscriptionInvoice"}
    invoice = schemas["SubscriptionInvoice"]
    assert {"invoice_id", "invoice_link", "amount", "currency", "expires_at"} <= set(
        invoice["required"]
    )
    assert invoice["properties"]["currency"]["const"] == "XTR"
    responses = post["responses"]
    assert {"201", "401", "403", "409", "422", "429", "503"} <= set(responses)
    assert _example_codes(_resolve(spec, responses["409"])) == [
        "couple_not_active",
        "conflict_retry",
    ]
    limited = _resolve(spec, responses["429"])
    assert limited["headers"]["Retry-After"]["required"] is True
    assert _example_codes(limited) == ["rate_limited"]
    assert _example_codes(_resolve(spec, responses["503"])) == ["service_unavailable"]
    required = _resolve(spec, {"$ref": "#/components/responses/SubscriptionRequired"})
    assert _example_codes(required) == ["subscription_required"]
    assert "§11 п.5" in required["description"]  # кризис и помощь — без подписки
    # 402 — там, где действие требует подписки: отправка сообщения и реплика агенту.
    assert "402" in paths["/drafts/{draft_id}/send"]["post"]["responses"]
    assert "402" in paths["/chat/messages"]["post"]["responses"]
    assert "402" not in post["responses"]  # оплатить можно и без доступа
