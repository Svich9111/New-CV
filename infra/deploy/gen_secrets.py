"""Заполняет пустые секреты в infra/deploy/.env стенда и ничего секретного не печатает.

Запуск (из корня репозитория, python3 ≥ 3.10, без зависимостей):

    python3 -I infra/deploy/gen_secrets.py [--dir КАТАЛОГ]

``--dir`` — куда писать .env, admin-token.txt и secrets/ (по умолчанию — рядом со скриптом;
для локальной проверки комплекта — временный каталог, compose запускается с ``--env-file``).

- нет .env — создаётся из .env.prod.example с правами 600;
- генерируются только ПУСТЫЕ значения; заданные не меняются (повторный запуск безопасен,
  ротация — вручную по docs/ADMIN.md);
- пароль суперпользователя БД — в secrets/postgres_superuser_password (600), не в .env;
- токен администратора пишется в infra/deploy/admin-token.txt (600), в .env — только его SHA-256;
- в вывод попадают только имена заполненных переменных.

Вручную остаются: FEELINGS_DOMAIN, FEELINGS_TLS, FEELINGS_TELEGRAM_BOT_TOKEN,
FEELINGS_BOT_USERNAME, OPENROUTER_API_KEY."""

import argparse
import base64
import hashlib
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE / ".env.prod.example"


def _password() -> str:
    return secrets.token_urlsafe(32)


def _long_secret() -> str:
    return secrets.token_urlsafe(48)


def _encryption_keys() -> str:
    return "k1:" + base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def _fingerprint_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


GENERATORS: dict[str, Callable[[], str]] = {
    "FEELINGS_OWNER_DB_PASSWORD": _password,
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
    "FEELINGS_TLS",
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
    parser = argparse.ArgumentParser(description="Заполняет пустые секреты стенда")
    parser.add_argument("--dir", type=Path, default=HERE)
    base = parser.parse_args().dir.resolve()
    env = base / ".env"
    admin_token_file = base / "admin-token.txt"
    secrets_dir = base / "secrets"
    superuser_file = secrets_dir / "postgres_superuser_password"
    secrets_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(secrets_dir, 0o700)
    if not superuser_file.exists():
        _write_private(superuser_file, _password() + "\n")
        print(
            f"создан {superuser_file.relative_to(base)} (пароль суперпользователя БД)"
        )
    if not env.exists():
        _write_private(env, EXAMPLE.read_text(encoding="utf-8"))
        print(f"создан {env.name} из {EXAMPLE.name}")
    if env.stat().st_mode & 0o077:
        os.chmod(env, 0o600)
        print(f"права {env.name} исправлены на 600")
    lines = env.read_text(encoding="utf-8").splitlines()
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
            if admin_token_file.exists():
                print(
                    f"{admin_token_file.name} уже есть — хеш не создан (удалите файл или задайте хеш)"
                )
                continue
            token = secrets.token_urlsafe(32)
            _write_private(admin_token_file, token + "\n")
            value = hashlib.sha256(token.encode()).hexdigest()
        else:
            continue
        lines[i] = f"{name}={value}"
        values[name] = value
        filled.append(name)
    _write_private(env, "\n".join(lines) + "\n")
    for name in filled:
        print(f"заполнено: {name}")
    if "FEELINGS_ADMIN_TOKEN_SHA256" in filled:
        print(
            f"токен администратора — в {admin_token_file} (600): передать и удалить файл"
        )
    missing = [n for n in MANUAL if not values.get(n)]
    for name in missing:
        print(f"задать вручную: {name}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
