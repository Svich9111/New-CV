"""Проверка ответа JSON-схемой из api/openapi.yaml (components.schemas)."""

from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

SPEC: dict[str, Any] = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "api" / "openapi.yaml").read_text(encoding="utf-8")
)
_REGISTRY = Registry().with_resource("spec", DRAFT202012.create_resource(SPEC))


def assert_matches(instance: Any, schema_name: str) -> None:
    schema = {"$ref": f"spec#/components/schemas/{schema_name}"}
    Draft202012Validator(
        schema, registry=_REGISTRY, format_checker=Draft202012Validator.FORMAT_CHECKER
    ).validate(instance)
