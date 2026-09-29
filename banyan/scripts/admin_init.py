#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Idempotent platform super-admin bootstrap for the Banyan-hosting gateway.

Runs INSIDE the gateway image as the one-shot ``admin-init`` compose service
(banyan/docker-compose.yml), driven by deploy.sh stage 10 via
``docker compose run --rm admin-init``. Credentials arrive through the
compose ``env_file: ./.env`` (ADMIN_ACCOUNT / ADMIN_PASSWORD + POSTGRES_*).

Why this exists: the preset roles (platform:super_admin / tenant_admin /
merchant_admin) are seeded by perm_engine's Config.initialize during the
gateway cold start (P6), but a fresh deployment has ZERO users — nobody can
log in to grant the first role. This script closes that bootstrap gap.

Semantics (every step idempotent — safe to re-run on each deploy):

1. wait for PostgreSQL to accept connections;
2. wait for the ``platform:super_admin`` preset role row (the gateway cold
   start seeds it; a healthy gateway implies the rows exist — the retry
   window is belt-and-braces, timeout dies safely and re-runnable);
3. ensure the ``tenant_user`` row for ADMIN_ACCOUNT:
   - missing  → INSERT (status=ACTIVE, email_verified=true, Argon2id hash,
     tenant_id NULL = platform level, created_by = zero UUID, matching the
     anonymous-bootstrapping convention of registerUser);
   - existing→ CONVERGE ONLY: flip status→ACTIVE and email_verified→true;
     the password is NEVER overwritten — a re-run after an operator changes
     the password must not reset it;
4. ensure the role binding in ``tenant_perm_user_role`` (ON CONFLICT DO
   NOTHING — the UNIQUE(user_id, role_id) makes this naturally idempotent);
5. verify the end state (user row + binding) and print a summary.

Deliberately NOT done (user-approved): no password_history rows, no audit
rows — the engines own those tables' semantics; this bootstrap only
guarantees the login closure.

Password hashing mirrors user_engine/utils/security.py exactly (OWASP
Argon2id baseline: time_cost=3, memory_cost=65536, parallelism=4,
hash_len=32; the salt is embedded in the self-describing hash string and
mirrored into password_salt via hash.split("$")[4]). user_engine itself is
NOT imported — its package __init__ drags the whole engine cold start in.

Configuration (environment):
  ADMIN_ACCOUNT    login email; stored as tenant_user.email
                  (default admin@banyanos.dev)
  ADMIN_PASSWORD   initial password (default B@nyan0s.d3v — CHANGE AFTER
                  FIRST LOGIN). Strength gate: >=12 chars + >=3 character
                  classes. Value gate: no whitespace/quotes/backslash/$
                  (must survive .env round-trips).
  POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB   database coordinates
  ADMIN_PG_HOST / ADMIN_PG_PORT   override the compose service defaults

Exit codes: 0 ok; 1 runtime failure (connection/role/SQL); 2 usage/config.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2

ZERO_UUID = "00000000-0000-0000-0000-000000000000"
SUPER_ADMIN_ROLE_CODE = "platform:super_admin"
DEFAULT_ADMIN_ACCOUNT = "admin@banyanos.dev"
DEFAULT_ADMIN_PASSWORD = "B@nyan0s.d3v"
DISPLAY_NAME = "Platform Super Admin"

# Argon2id parameters — MUST stay identical to user_engine/utils/security.py
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # 64 MiB
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32

# Value safety: the .env round-trip (heredoc + compose env_file) cannot
# survive these characters unambiguously — reject them up front.
_FORBIDDEN_RE = re.compile(r"[\s'\"\\$]")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ConfigError(Exception):
    """Invalid ADMIN_* values / usage problem (exit code 2)."""


class RuntimeFailure(Exception):
    """Connection/role/SQL failure (exit code 1)."""


# ---------------------------------------------------------------------------
# Validation + derivation (pure logic)
# ---------------------------------------------------------------------------


def validate_account(account: str) -> str:
    """Normalize + gate ADMIN_ACCOUNT (must be a sane email address)."""
    normalized = (account or "").strip().lower()
    if not normalized:
        raise ConfigError("ADMIN_ACCOUNT is empty — set it in .env")
    if _FORBIDDEN_RE.search(normalized):
        raise ConfigError(
            "ADMIN_ACCOUNT contains forbidden characters "
            "(whitespace/quotes/backslash/$)"
        )
    if not _EMAIL_RE.match(normalized):
        raise ConfigError(
            f"ADMIN_ACCOUNT must be an email address — got: {normalized!r}"
        )
    if len(normalized) > 128:
        raise ConfigError("ADMIN_ACCOUNT longer than 128 chars (tenant_user.email)")
    if len(normalized.split("@", 1)[0]) > 64:
        raise ConfigError(
            "ADMIN_ACCOUNT local part longer than 64 chars (tenant_user.username)"
        )
    return normalized


def _char_classes(password: str) -> int:
    classes = 0
    if re.search(r"[a-z]", password):
        classes += 1
    if re.search(r"[A-Z]", password):
        classes += 1
    if re.search(r"[0-9]", password):
        classes += 1
    if re.search(r"[^A-Za-z0-9]", password):
        classes += 1
    return classes


def validate_password(password: str) -> str:
    """Gate ADMIN_PASSWORD: non-empty, value-safe, >=12 chars, >=3 classes."""
    if not password:
        raise ConfigError("ADMIN_PASSWORD is empty — set it in .env")
    if _FORBIDDEN_RE.search(password):
        raise ConfigError(
            "ADMIN_PASSWORD contains forbidden characters "
            "(whitespace/quotes/backslash/$)"
        )
    if len(password) < 12:
        raise ConfigError(
            f"ADMIN_PASSWORD too weak: need >=12 chars, got {len(password)}"
        )
    if _char_classes(password) < 3:
        raise ConfigError(
            "ADMIN_PASSWORD too weak: need >=3 character classes "
            "(lower/upper/digit/symbol)"
        )
    return password


def derive_username(account: str) -> str:
    """tenant_user.username from the email local part (admin@x.io → admin)."""
    return account.split("@", 1)[0]


def extract_salt(password_hash: str) -> str:
    """password_salt from the self-describing argon2 hash (engine convention)."""
    parts = password_hash.split("$")
    if len(parts) < 6:
        raise RuntimeFailure(
            f"unexpected argon2 hash format (cannot extract salt): "
            f"{password_hash!r}"
        )
    return parts[4]


def hash_password(password: str) -> Dict[str, str]:
    """Argon2id hash with the engine's exact parameters.

    Returns (password_hash, password_salt): the self-describing hash string
    plus the embedded base64 salt, both stored as tenant_user columns.
    """
    from argon2 import PasswordHasher

    hasher = PasswordHasher(
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
    )
    digest = hasher.hash(password)
    return {"password_hash": digest, "password_salt": extract_salt(digest)}


# ---------------------------------------------------------------------------
# Database access (SQLAlchemy text() + .mappings() per the project iron rule)
# ---------------------------------------------------------------------------

SQL_ROLE = """
SELECT id FROM tenant_perm_role
WHERE code = :code AND tenant_id IS NULL AND deleted_at IS NULL
"""

SQL_FIND_USER = """
SELECT id, status, email_verified
FROM tenant_user
WHERE email = :email AND deleted_at IS NULL
"""

SQL_INSERT_USER = """
INSERT INTO tenant_user (
    username, display_name, password_hash, password_salt, password_algo,
    email, status, email_verified, created_by
) VALUES (
    :username, :display_name, :password_hash, :password_salt, 'ARGON2ID',
    :email, 'ACTIVE', true, :created_by
)
RETURNING id
"""

SQL_CONVERGE_USER = """
UPDATE tenant_user
SET status = 'ACTIVE', email_verified = true, updated_at = now()
WHERE id = :user_id
"""

SQL_BIND_ROLE = """
INSERT INTO tenant_perm_user_role (user_id, role_id, assigned_by)
VALUES (:user_id, :role_id, :assigned_by)
ON CONFLICT (user_id, role_id) DO NOTHING
RETURNING id
"""

SQL_VERIFY = """
SELECT u.id AS user_id, u.username, u.status, u.email_verified,
       r.code AS role_code
FROM tenant_user u
JOIN tenant_perm_user_role ur ON ur.user_id = u.id
JOIN tenant_perm_role r ON r.id = ur.role_id
WHERE u.email = :email AND u.deleted_at IS NULL
  AND r.code = :code AND r.deleted_at IS NULL
"""


def build_url(user: str, password: str, host: str, port: int, db: str) -> Any:
    """sqlalchemy URL — URL.create escapes the password safely."""
    from sqlalchemy.engine import URL

    return URL.create(
        drivername="postgresql+psycopg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=db,
    )


def connect(url: Any, max_wait: int) -> Any:
    """Engine that answered SELECT 1, or RuntimeFailure within max_wait."""
    from sqlalchemy import create_engine, text

    deadline = time.monotonic() + max_wait
    last_error: Optional[Exception] = None
    while True:
        try:
            engine = create_engine(url, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except Exception as exc:  # booting / credentials / network
            last_error = exc
            if time.monotonic() > deadline:
                raise RuntimeFailure(
                    f"PostgreSQL not reachable within {max_wait}s "
                    f"(last error: {last_error})"
                ) from exc
            time.sleep(2)


def wait_for_role(engine: Any, code: str, max_wait: int) -> str:
    """Bounded wait for the preset role row (gateway cold start seeds it)."""
    from sqlalchemy import text

    deadline = time.monotonic() + max_wait
    while True:
        with engine.connect() as conn:
            row = (
                conn.execute(text(SQL_ROLE), {"code": code}).mappings().first()
            )
        if row:
            return str(row["id"])
        if time.monotonic() > deadline:
            raise RuntimeFailure(
                f"preset role '{code}' not found within {max_wait}s — the "
                "gateway cold start seeds it; check the gateway logs "
                "(perm_engine Config.initialize) and re-run deploy.sh"
            )
        time.sleep(2)


def ensure_user(
    engine: Any,
    account: str,
    username: str,
    credentials: Dict[str, str],
) -> Tuple[str, str]:
    """Insert the admin user or converge an existing row (never the password).

    Returns (user_id, action) where action is one of
    "created" / "converged" / "already-active".
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        user = (
            conn.execute(text(SQL_FIND_USER), {"email": account})
            .mappings()
            .first()
        )
        if user is None:
            row = (
                conn.execute(
                    text(SQL_INSERT_USER),
                    {
                        "username": username,
                        "display_name": DISPLAY_NAME,
                        "password_hash": credentials["password_hash"],
                        "password_salt": credentials["password_salt"],
                        "email": account,
                        "created_by": ZERO_UUID,
                    },
                )
                .mappings()
                .first()
            )
            user_id = str(row["id"])
            action = "created"
        else:
            user_id = str(user["id"])
            if user["status"] != "ACTIVE" or not user["email_verified"]:
                conn.execute(text(SQL_CONVERGE_USER), {"user_id": user_id})
                action = "converged"
            else:
                action = "already-active"
    return user_id, action


def ensure_binding(engine: Any, user_id: str, role_id: str) -> bool:
    """Bind platform:super_admin; True when newly bound, False when present."""
    from sqlalchemy import text

    with engine.begin() as conn:
        row = (
            conn.execute(
                text(SQL_BIND_ROLE),
                {
                    "user_id": user_id,
                    "role_id": role_id,
                    "assigned_by": ZERO_UUID,
                },
            )
            .mappings()
            .first()
        )
    return row is not None


def verify(engine: Any, account: str, code: str) -> Dict[str, Any]:
    """Read the end state back; RuntimeFailure when the closure is broken."""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = (
            conn.execute(text(SQL_VERIFY), {"email": account, "code": code})
            .mappings()
            .first()
        )
    if row is None:
        raise RuntimeFailure(
            f"verification failed: no active '{code}' binding for {account}"
        )
    return dict(row)


# ---------------------------------------------------------------------------
# Self-test (pure logic; argon2 round-trip only when importable)
# ---------------------------------------------------------------------------


def self_test() -> int:
    checks: List[Tuple[str, bool]] = []

    # validate_account
    try:
        validate_account(DEFAULT_ADMIN_ACCOUNT)
        checks.append(("default account accepted", True))
        checks.append(
            (
                "account upper-case normalized",
                validate_account("  Admin@Banyanos.DEV ") == "admin@banyanos.dev",
            )
        )
    except ConfigError:
        checks.append(("default account accepted", False))
    for bad in ("", "not-an-email", "a@b", "has space@x.io", "q'uo@x.io", "a$b@x.io"):
        try:
            validate_account(bad)
            checks.append((f"account rejected: {bad!r}", False))
        except ConfigError:
            checks.append((f"account rejected: {bad!r}", True))

    # validate_password
    try:
        validate_password(DEFAULT_ADMIN_PASSWORD)
        checks.append(("default password accepted", True))
    except ConfigError:
        checks.append(("default password accepted", False))
    for bad in ("", "short", "Ab1!" * 2 + "x", "abcdefghijkl", "aB1$aaaa"):
        try:
            validate_password(bad)
            checks.append((f"password rejected: {bad!r}", False))
        except ConfigError:
            checks.append((f"password rejected: {bad!r}", True))

    # derive_username / extract_salt
    checks.append(
        ("username derived from local part", derive_username("admin@x.io") == "admin")
    )
    synthetic = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$hashhashhash"
    checks.append(
        ("salt extracted at parts[4]", extract_salt(synthetic) == "c2FsdHNhbHQ")
    )
    try:
        extract_salt("not-an-argon2-hash")
        checks.append(("malformed hash rejected", False))
    except RuntimeFailure:
        checks.append(("malformed hash rejected", True))

    # argon2 round-trip (importable inside the gateway image)
    try:
        creds = hash_password("B@nyan0s.d3v")
        checks.append(
            ("argon2id hash format", creds["password_hash"].startswith("$argon2id$"))
        )
        checks.append(
            (
                "salt mirrors hash parts[4]",
                creds["password_salt"] == creds["password_hash"].split("$")[4],
            )
        )
        from argon2 import PasswordHasher
        from argon2.exceptions import VerifyMismatchError

        verifier = PasswordHasher(
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_COST,
            parallelism=ARGON2_PARALLELISM,
            hash_len=ARGON2_HASH_LEN,
        )
        try:
            verifier.verify(creds["password_hash"], "wrong-password")
            checks.append(("argon2 verify rejects wrong password", False))
        except VerifyMismatchError:
            checks.append(("argon2 verify rejects wrong password", True))
    except ImportError:
        print("  (skip argon2 round-trip: argon2-cffi not installed on this host)")
        print("      — runs inside the gateway image via admin-init --self-test")

    failed = [desc for desc, ok_flag in checks if not ok_flag]
    for desc, ok_flag in checks:
        print(f"  {'PASS' if ok_flag else 'FAIL'}: {desc}")
    if failed:
        print(f"self-test FAILED ({len(failed)}/{len(checks)})")
        return EXIT_RUNTIME
    print(f"self-test OK ({len(checks)}/{len(checks)})")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotent super-admin bootstrap: wait PG -> wait preset role "
            "-> ensure tenant_user -> ensure role binding -> verify."
        )
    )
    parser.add_argument(
        "--account",
        default=os.environ.get("ADMIN_ACCOUNT", DEFAULT_ADMIN_ACCOUNT),
        help="admin login email (default: %(default)s)",
    )
    parser.add_argument(
        "--role-code",
        default=SUPER_ADMIN_ROLE_CODE,
        help="preset role code to bind (default: %(default)s)",
    )
    parser.add_argument(
        "--max-wait",
        type=int,
        default=int(os.environ.get("ADMIN_INIT_MAX_WAIT", "120")),
        help="per-step wait budget in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--self-test", action="store_true", help="run built-in logic tests and exit"
    )
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    try:
        account = validate_account(args.account)
        # The password is intentionally read from the environment only —
        # never a CLI flag (process list would leak it).
        password = validate_password(os.environ.get("ADMIN_PASSWORD", ""))
        pg_user = os.environ.get("POSTGRES_USER", "")
        pg_password = os.environ.get("POSTGRES_PASSWORD", "")
        pg_db = os.environ.get("POSTGRES_DB", "")
        pg_host = os.environ.get("ADMIN_PG_HOST", "postgres")
        pg_port = int(os.environ.get("ADMIN_PG_PORT", "5432"))
        if not (pg_user and pg_password and pg_db):
            raise ConfigError(
                "POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB must be set "
                "(compose env_file: ./.env)"
            )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    username = derive_username(account)
    print(f"admin account : {account}")
    print(f"role          : {args.role_code} (tenant_id NULL = platform level)")

    try:
        credentials = hash_password(password)
        print("password      : hashed (Argon2id, engine-parity parameters)")
        url = build_url(pg_user, pg_password, pg_host, pg_port, pg_db)
        engine = connect(url, args.max_wait)
        print("postgres      : connected")

        role_id = wait_for_role(engine, args.role_code, args.max_wait)
        print(f"preset role   : found (role_id={role_id})")

        user_id, action = ensure_user(engine, account, username, credentials)
        print(f"tenant_user   : {action} (user_id={user_id})")

        bound = ensure_binding(engine, user_id, role_id)
        print(
            "role binding  : "
            + ("created (user_id, role_id)" if bound else "already present")
        )

        state = verify(engine, account, args.role_code)
        print(
            f"verify        : status={state['status']} "
            f"email_verified={state['email_verified']} "
            f"username={state['username']}"
        )
    except RuntimeFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except Exception as exc:  # sqlalchemy/psycopg/network errors land here
        print(f"error: super-admin bootstrap failed: {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    print(
        f"OK: super admin '{account}' ready with role '{args.role_code}' "
        "— password is in .env (ADMIN_PASSWORD); change it after first login"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())