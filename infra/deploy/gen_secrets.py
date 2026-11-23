"""Заполняет пустые секреты в infra/deploy/.env стенда и ничего секретного не печатает.

Запуск (из корня репозитория, python3 ≥ 3.10, без зависимостей):

    python3 -I infra/deploy/gen_secrets.py

- нет .env — создаётся из .env.prod.example с правами 600;
- генерируются только ПУСТЫЕ значения; заданные не меняются (повторный запуск безопасен,
  ротация — вручную по docs/ADMIN.md);
- токен администратора пишется в infra/deploy/admin-token.txt (600), в .env — только его SHA-256;
- в вывод попадают только имена заполненных переменных.

Вручную остаются: FEELINGS_DOMAIN, FEELINGS_ACME_EMAIL, FEELINGS_TELEGRAM_BOT_TOKEN,
FEELINGS_BOT_USERNAME, OPENROUTER_API_KEY."""

import base64
import hashlib
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"
EXAMPLE = HERE / ".env.prod.example"
ADMIN_TOKEN_FILE = HERE / "admin-token.txt"


def _password() -> str:
    return secrets.token_urlsafe(32)


def _long_secret() -> str:
    return secrets.token_urlsafe(48)


def _encryption_keys() -> str:
    return "k1:" + base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def _fingerprint_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


GENERATORS: dict[str, Callable[[], str]] = {
    "POSTGRES_PASSWORD": _password,
    "FEELINGS_API_DB_PASSWORD": _password,
    "FEELINGS_WORKER_DB_PASSWORD": _password,
    "FEELINGS_ADMIN_DB_PASSWORD": _password,
    "FEELINGS_TELEGRAM_WEBHOOK_SECRET": _long_secret,
    "FEELINGS_SESSION_SECRET": _long_secret,
    "FEELINGS_ENCRYPTION_KEYS": _encryption_keys,
    "FEELINGS_MEMORY_FINGERPRINT_KEY": _fingerprint_key,
}
MANUAL = (
    "FEELINGS_DOMAIN",
    "FEELINGS_ACME_EMAIL",
    "FEELINGS_TELEGRAM_BOT_TOKEN",
    "FEELINGS_BOT_USERNAME",
    "OPENROUTER_API_KEY",
)


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def main() -> int:
    if not ENV.exists():
        _write_private(ENV, EXAMPLE.read_text(encoding="utf-8"))
        print(f"создан {ENV.name} из {EXAMPLE.name}")
    if ENV.stat().st_mode & 0o077:
        os.chmod(ENV, 0o600)
        print(f"права {ENV.name} исправлены на 600")
    lines = ENV.read_text(encoding="utf-8").splitlines()
    filled: list[str] = []
    values: dict[str, str] = {}
    for i, line in enumerate(lines):
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if value.strip():
            values[name] = value.strip()
            continue
        if name in GENERATORS:
            value = GENERATORS[name]()
        elif name == "FEELINGS_ADMIN_TOKEN_SHA256":
            if ADMIN_TOKEN_FILE.exists():
                print(
                    f"{ADMIN_TOKEN_FILE.name} уже есть — хеш не создан (удалите файл или задайте хеш)"
                )
                continue
            token = secrets.token_urlsafe(32)
            _write_private(ADMIN_TOKEN_FILE, token + "\n")
            value = hashlib.sha256(token.encode()).hexdigest()
        else:
            continue
        lines[i] = f"{name}={value}"
        values[name] = value
        filled.append(name)
    _write_private(ENV, "\n".join(lines) + "\n")
    for name in filled:
        print(f"заполнено: {name}")
    if "FEELINGS_ADMIN_TOKEN_SHA256" in filled:
        print(
            f"токен администратора — в {ADMIN_TOKEN_FILE} (600): передать и удалить файл"
        )
    missing = [n for n in MANUAL if not values.get(n)]
    for name in missing:
        print(f"задать вручную: {name}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
