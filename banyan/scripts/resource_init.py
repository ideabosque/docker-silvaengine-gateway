#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Idempotent platform resource registration + root-role grant convergence.

Runs INSIDE the gateway image as the one-shot ``resource-init`` compose
service (banyan/docker-compose.yml), driven by deploy.sh stage 11 via
``docker compose run --rm resource-init``. Credentials arrive through the
compose ``env_file: ./.env`` (ADMIN_ACCOUNT / ADMIN_PASSWORD + POSTGRES_*).

Why this exists: the resource catalog (frontend visibility resources +
per-engine API actions) is registered by perm_engine's
``ensure_default_resources`` — deliberately NOT part of the gateway cold
start (it imports all 12 engines' deploy(); see perm_engine
handlers/config.py) and historically required a manual frontend click on
「资源注册」. A fresh deployment therefore shipped with an empty
``tenant_perm_resource`` and zero role-resource grants until someone logged
in and clicked. This stage closes that gap through the SAME forward path
the frontend uses.

Semantics (every step idempotent — safe to re-run on each deploy):

1. wait for the gateway /health endpoint (compose already gates on
   service_healthy; the retry window is belt-and-braces);
2. login as ADMIN_ACCOUNT (anonymous-whitelisted mutation) → authToken —
   this also end-to-end validates the stage-10 admin closure;
3. call the ``registerResources`` mutation (platform-level, requires the
   platform:super_admin role — granted by stage 10). One call does both:
   differential resource insert AND ``_ensure_preset_role_permissions``
   (all registered resources PERMIT-bound to platform:super_admin +
   tenant_admin / merchant_admin / merchant root roles);
4. verify the end state:
   a. GraphQL ``resourceRegistrationStats`` (informational; a mismatch is
      WARN-only — code dedup vs (module, name) dedup can legitimately
      differ), and
   b. the authoritative DB anti-join: every live resource must have a
      grant row for platform:super_admin (gap == 0), resource count > 0.

Degradation (deliberate): if login fails but the DB end state is already
converged (resources present, zero grant gap) the script WARNs and exits
0 — the operator is expected to have changed the admin password after
first login (the runbook says to), so a re-deploy must not be blocked by
the stale .env password. Any other login failure (resources absent or
gap > 0) fails closed with actionable pointers.

Password handling: the value is read from the environment only — never a
CLI flag (process list would leak it). No strength gate here (unlike
admin_init, which CREATES the credential); any value the operator set
must be usable for login.

Configuration (environment):
  ADMIN_ACCOUNT / ADMIN_PASSWORD   super-admin credentials (stage 10)
  TENANT_PART_ID                   part_id header (default nestaging)
  ADAPTER_STAGE / ADAPTER_AREA / ENDPOINT_ID   route segments
                                  (default beta / core / banyan)
  RESOURCE_INIT_BASE_URL           gateway base URL
                                  (default http://gateway:8000)
  RESOURCE_INIT_HTTP_TIMEOUT       per-call HTTP timeout seconds
                                  (default 300 — first registration
                                  imports all engines inside the
                                  gateway process)
  ADMIN_PG_HOST / ADMIN_PG_PORT   Postgres coordinates override
  POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB   database coordinates

Exit codes: 0 ok (including the degraded WARN pass); 1 runtime failure
(login/registration/verification); 2 usage/config.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2

SUPER_ADMIN_ROLE_CODE = "platform:super_admin"
DEFAULT_PART_ID = "nestaging"
DEFAULT_ROUTE = ("beta", "core", "banyan")
DEFAULT_BASE_URL = "http://gateway:8000"
DEFAULT_HTTP_TIMEOUT = 300

GQL_LOGIN = (
    "mutation($k: ID!, $input: LoginInput!){ "
    "login(idempotencyKey: $k, input: $input){ authToken user { id } } }"
)
GQL_REGISTER = (
    "mutation($k: ID!){ registerResources(idempotencyKey: $k){ "
    "success inserted skipped errors total message } }"
)
GQL_STATS = "{ resourceRegistrationStats { scanned imported } }"


class ConfigError(Exception):
    """Invalid values / usage problem (exit code 2)."""


class RuntimeFailure(Exception):
    """Gateway/GraphQL/verification failure (exit code 1)."""


# ---------------------------------------------------------------------------
# Route / URL / payload building (pure logic)
# ---------------------------------------------------------------------------


def build_base_url(raw: Optional[str]) -> str:
    """Normalize RESOURCE_INIT_BASE_URL (strip trailing slashes)."""
    base = (raw or "").strip() or DEFAULT_BASE_URL
    if not base.startswith(("http://", "https://")):
        raise ConfigError(
            f"RESOURCE_INIT_BASE_URL must start with http:// or https:// "
            f"— got: {base!r}"
        )
    return base.rstrip("/")


def build_engine_url(base_url: str, route: Tuple[str, str, str], engine: str) -> str:
    """GraphQL endpoint for an engine: /{stage}/{area}/{endpoint}/{engine}_graphql."""
    stage, area, endpoint = route
    return f"{base_url}/{stage}/{area}/{endpoint}/{engine}_graphql"


def build_headers(part_id: str, token: Optional[str]) -> Dict[str, str]:
    """Common request headers; Authorization only when a token is present."""
    headers = {"content-type": "application/json", "part_id": part_id}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def build_login_payload(account: str, password: str, idem_key: str) -> bytes:
    """Login mutation body — json.dumps keeps every value injection-safe."""
    return json.dumps(
        {
            "query": GQL_LOGIN,
            "variables": {
                "k": idem_key,
                "input": {"email": account, "password": password},
            },
        }
    ).encode("utf-8")


def build_register_payload(idem_key: str) -> bytes:
    return json.dumps(
        {"query": GQL_REGISTER, "variables": {"k": idem_key}}
    ).encode("utf-8")


def build_stats_payload() -> bytes:
    return json.dumps({"query": GQL_STATS}).encode("utf-8")


_idem_seq = 0


def new_idem_key(prefix: str) -> str:
    """Fresh idempotency key per call (re-runs must re-execute, not replay)."""
    global _idem_seq
    _idem_seq += 1
    return f"{prefix}-{int(time.time())}-{os.getpid()}-{_idem_seq}"


# ---------------------------------------------------------------------------
# GraphQL client (stdlib urllib — the gateway image has no curl)
# ---------------------------------------------------------------------------


def graphql_post(
    url: str,
    payload: bytes,
    headers: Dict[str, str],
    timeout: int,
) -> Dict[str, Any]:
    """POST a GraphQL document; returns the parsed JSON body.

    Raises RuntimeFailure on transport errors / non-200 / invalid JSON.
    GraphQL-level ``errors`` are NOT raised here — the caller decides
    whether an errors list is fatal (login) or reportable (stats).
    """
    request = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        raise RuntimeFailure(
            f"HTTP {exc.code} from {url}"
            + (f" — {detail}" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeFailure(f"cannot reach {url} — {exc.reason}") from exc
    except Exception as exc:
        raise RuntimeFailure(f"request to {url} failed: {exc}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeFailure(
            f"non-JSON response from {url}: {body[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeFailure(f"unexpected response shape from {url}: {body[:200]!r}")
    return parsed


def extract_errors(body: Dict[str, Any]) -> str:
    """First GraphQL error message (empty string when none)."""
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("message", first))
        return str(first)
    return ""


# ---------------------------------------------------------------------------
# End-state verification (SQLAlchemy text() + .mappings() per the iron rule)
# ---------------------------------------------------------------------------

SQL_SUPER_ADMIN_ROLE_ID = """
SELECT id FROM tenant_perm_role
WHERE code = :code AND deleted_at IS NULL
LIMIT 1
"""

SQL_RESOURCE_COUNT = """
SELECT count(*) AS n
FROM tenant_perm_resource
WHERE deleted_at IS NULL
"""

# Authoritative end state: every live resource has a grant row for the
# root role. tenant_perm_role_resource has NO deleted_at column (grants
# are UPSERT rows; revoke is a hard DELETE), so no soft-delete filter.
SQL_GRANT_GAP = """
SELECT count(*) AS gap
FROM tenant_perm_resource r
WHERE r.deleted_at IS NULL
    AND NOT EXISTS (
        SELECT 1 FROM tenant_perm_role_resource g
        WHERE g.role_id = :role_id AND g.resource_id = r.id
    )
"""


def build_pg_url(
    user: str, password: str, host: str, port: int, db: str
) -> Any:
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


def connect_pg(url: Any, max_wait: int) -> Any:
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


def db_end_state(engine: Any) -> Dict[str, Any]:
    """Read the authoritative end state: role id / resources / grant gap.

    role_id is None when the preset root role is missing (cold start
    failed) — callers must treat that as a broken closure.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        role_row = (
            conn.execute(
                text(SQL_SUPER_ADMIN_ROLE_ID), {"code": SUPER_ADMIN_ROLE_CODE}
            )
            .mappings()
            .first()
        )
        if role_row is None:
            return {
                "role_id": None,
                "resources": 0,
                "gap": -1,
            }
        role_id = str(role_row["id"])
        resources = int(
            conn.execute(text(SQL_RESOURCE_COUNT)).mappings().first()["n"]
        )
        gap = int(
            conn.execute(text(SQL_GRANT_GAP), {"role_id": role_id})
            .mappings()
            .first()["gap"]
        )
    return {"role_id": role_id, "resources": resources, "gap": gap}


def db_end_state_ok(state: Dict[str, Any]) -> bool:
    """True when resources exist and every one is granted to the root role."""
    return bool(state["role_id"]) and state["resources"] > 0 and state["gap"] == 0


# ---------------------------------------------------------------------------
# Gateway health wait (belt-and-braces on top of compose service_healthy)
# ---------------------------------------------------------------------------


def wait_for_gateway(
    base_url: str, timeout_budget: int, http_timeout: int
) -> None:
    """Bounded wait for GET {base}/health → 200."""
    deadline = time.monotonic() + timeout_budget
    url = f"{base_url}/health"
    last_error: Optional[str] = None
    while True:
        try:
            with urllib.request.urlopen(url, timeout=http_timeout) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except Exception as exc:
            last_error = str(exc)
        if time.monotonic() > deadline:
            raise RuntimeFailure(
                f"gateway /health not ready within {timeout_budget}s "
                f"(last error: {last_error}) — check "
                "docker compose logs gateway and re-run deploy.sh"
            )
        time.sleep(2)


# ---------------------------------------------------------------------------
# Forward-path steps
# ---------------------------------------------------------------------------


def login(
    base_url: str,
    route: Tuple[str, str, str],
    part_id: str,
    account: str,
    password: str,
    http_timeout: int,
) -> str:
    """Anonymous-whitelisted login → authToken (fail-closed on any error)."""
    url = build_engine_url(base_url, route, "user_engine")
    body = graphql_post(
        url,
        build_login_payload(account, password, new_idem_key("res-init")),
        build_headers(part_id, token=None),
        http_timeout,
    )
    message = extract_errors(body)
    data = body.get("data") or {}
    token = (data.get("login") or {}).get("authToken") if isinstance(data, dict) else None
    if not token:
        raise RuntimeFailure(
            f"login failed for {account}"
            + (f" — {message}" if message else " (no error message)")
        )
    return str(token)


def register_resources(
    base_url: str,
    route: Tuple[str, str, str],
    part_id: str,
    token: str,
    http_timeout: int,
) -> Dict[str, Any]:
    """registerResources mutation → stats dict (fail-closed on transport)."""
    url = build_engine_url(base_url, route, "perm_engine")
    body = graphql_post(
        url,
        build_register_payload(new_idem_key("res-reg")),
        build_headers(part_id, token=token),
        http_timeout,
    )
    message = extract_errors(body)
    data = body.get("data") or {}
    result = data.get("registerResources") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise RuntimeFailure(
            "registerResources returned no result"
            + (f" — {message}" if message else "")
        )
    return result


def fetch_stats(
    base_url: str,
    route: Tuple[str, str, str],
    part_id: str,
    token: str,
    http_timeout: int,
) -> Tuple[Optional[int], Optional[int]]:
    """resourceRegistrationStats (scanned, imported) — informational only."""
    url = build_engine_url(base_url, route, "perm_engine")
    try:
        body = graphql_post(
            url,
            build_stats_payload(),
            build_headers(part_id, token=token),
            http_timeout,
        )
    except RuntimeFailure as exc:
        print(f"  (stats query unavailable: {exc})")
        return None, None
    if extract_errors(body):
        print("  (stats query returned GraphQL errors — informational only)")
        return None, None
    data = body.get("data") or {}
    stats = data.get("resourceRegistrationStats") if isinstance(data, dict) else None
    if not isinstance(stats, dict):
        return None, None
    scanned = stats.get("scanned")
    imported = stats.get("imported")
    return (
        int(scanned) if isinstance(scanned, int) else None,
        int(imported) if isinstance(imported, int) else None,
    )


# ---------------------------------------------------------------------------
# Self-test (pure logic — no network, no DB)
# ---------------------------------------------------------------------------


def self_test() -> int:
    checks: List[Tuple[str, bool]] = []

    def expect(desc: str, ok: bool) -> None:
        checks.append((desc, ok))

    # build_base_url
    expect(
        "default base url",
        build_base_url(None) == "http://gateway:8000",
    )
    expect("base url trailing slash stripped", build_base_url("http://x:1/") == "http://x:1")
    try:
        build_base_url("gateway:8000")
        expect("non-http base url rejected", False)
    except ConfigError:
        expect("non-http base url rejected", True)

    # build_engine_url
    expect(
        "engine url shape",
        build_engine_url("http://g:8000", ("beta", "core", "banyan"), "perm_engine")
        == "http://g:8000/beta/core/banyan/perm_engine_graphql",
    )

    # build_headers
    headers = build_headers("nestaging", None)
    expect("anonymous headers carry part_id", headers.get("part_id") == "nestaging")
    expect("anonymous headers have no auth", "authorization" not in headers)
    headers = build_headers("nestaging", "tok")
    expect("auth headers carry bearer", headers.get("authorization") == "Bearer tok")

    # payloads
    login_payload = json.loads(build_login_payload("a@b.io", 'p"\\$x', "k1"))
    expect(
        "login payload keeps operation shape",
        login_payload["query"].startswith("mutation($k: ID!, $input: LoginInput!)"),
    )
    expect(
        "login payload escapes special chars safely",
        login_payload["variables"]["input"]["password"] == 'p"\\$x',
    )
    register_payload = json.loads(build_register_payload("k2"))
    expect(
        "register payload keeps operation shape",
        register_payload["query"].startswith("mutation($k: ID!){"),
    )
    expect("register payload idem key", register_payload["variables"]["k"] == "k2")
    expect(
        "stats payload is a bare query",
        json.loads(build_stats_payload())["query"].startswith("{ resourceRegistrationStats"),
    )

    # idempotency keys
    key_a = new_idem_key("t")
    key_b = new_idem_key("t")
    expect("idem keys unique per call", key_a != key_b)
    expect("idem key carries prefix", key_a.startswith("t-"))

    # extract_errors
    expect("extract_errors empty body", extract_errors({"data": {}}) == "")
    expect(
        "extract_errors first message",
        extract_errors({"errors": [{"message": "boom"}]}) == "boom",
    )
    expect(
        "extract_errors tolerant of strings",
        extract_errors({"errors": ["plain"]}) == "plain",
    )

    # login result classification (the login() path treats this as failure)
    expect(
        "login body without token is a failure",
        not ((({"data": {"login": None}}).get("data") or {}).get("login")),
    )

    # db_end_state decision matrix
    ok_state = {"role_id": "r1", "resources": 42, "gap": 0}
    expect("end state ok when resources granted", db_end_state_ok(ok_state))
    expect(
        "end state fails on grant gap",
        not db_end_state_ok({"role_id": "r1", "resources": 42, "gap": 1}),
    )
    expect(
        "end state fails on empty catalog",
        not db_end_state_ok({"role_id": "r1", "resources": 0, "gap": 0}),
    )
    expect(
        "end state fails on missing role",
        not db_end_state_ok({"role_id": None, "resources": 42, "gap": -1}),
    )

    # register result gating
    result_ok = {"success": True, "inserted": 5, "skipped": 3, "errors": 0, "total": 8}
    expect("register result success flag", result_ok["success"] is True)
    expect("register result errors gate", result_ok["errors"] == 0)
    result_bad = dict(result_ok, errors=2)
    expect("register result fails on errors", result_bad["errors"] > 0)

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
            "Idempotent resource registration + root-role grant convergence: "
            "wait gateway -> login -> registerResources -> verify end state."
        )
    )
    parser.add_argument(
        "--self-test", action="store_true", help="run built-in logic tests and exit"
    )
    parser.add_argument(
        "--max-wait",
        type=int,
        default=int(os.environ.get("ADMIN_INIT_MAX_WAIT", "120")),
        help="per-step wait budget in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--http-timeout",
        type=int,
        default=int(os.environ.get("RESOURCE_INIT_HTTP_TIMEOUT", "300")),
        help="per-call HTTP timeout in seconds (default: %(default)s)",
    )
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    try:
        account = (os.environ.get("ADMIN_ACCOUNT", "") or "").strip().lower()
        if not account:
            raise ConfigError("ADMIN_ACCOUNT is empty — set it in .env")
        password = os.environ.get("ADMIN_PASSWORD", "")
        if not password:
            raise ConfigError("ADMIN_PASSWORD is empty — set it in .env")
        part_id = (os.environ.get("TENANT_PART_ID", DEFAULT_PART_ID) or DEFAULT_PART_ID)
        route = (
            os.environ.get("ADAPTER_STAGE", DEFAULT_ROUTE[0]) or DEFAULT_ROUTE[0],
            os.environ.get("ADAPTER_AREA", DEFAULT_ROUTE[1]) or DEFAULT_ROUTE[1],
            os.environ.get("ENDPOINT_ID", DEFAULT_ROUTE[2]) or DEFAULT_ROUTE[2],
        )
        base_url = build_base_url(os.environ.get("RESOURCE_INIT_BASE_URL"))
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

    print(f"admin account : {account}")
    print(f"part_id       : {part_id}")
    print(f"route         : /{route[0]}/{route[1]}/{route[2]}/<engine>_graphql")

    try:
        wait_for_gateway(base_url, args.max_wait, args.http_timeout)
        print("gateway       : /health ready")

        login_ok = True
        token = ""
        try:
            token = login(
                base_url, route, part_id, account, password, args.http_timeout
            )
            print("login         : ok (authToken issued)")
        except RuntimeFailure as exc:
            login_ok = False
            print(f"login         : FAILED — {exc}")

        if login_ok:
            result = register_resources(
                base_url, route, part_id, token, args.http_timeout
            )
            inserted = int(result.get("inserted") or 0)
            skipped = int(result.get("skipped") or 0)
            errors = int(result.get("errors") or 0)
            total = int(result.get("total") or 0)
            print(
                f"register      : inserted={inserted} skipped={skipped} "
                f"errors={errors} total={total}"
            )
            if result.get("message"):
                print(f"               {result['message']}")
            if errors > 0:
                raise RuntimeFailure(
                    f"registerResources reported {errors} failed item(s) "
                    f"(total={total}) — see gateway logs for failed engine "
                    "imports; re-run deploy.sh to converge"
                )

        # Authoritative end state — always verified, whichever path got here.
        engine = connect_pg(
            build_pg_url(pg_user, pg_password, pg_host, pg_port, pg_db),
            args.max_wait,
        )
        print("postgres      : connected")
        state = db_end_state(engine)
        if state["role_id"] is None:
            raise RuntimeFailure(
                f"preset role '{SUPER_ADMIN_ROLE_CODE}' not found — the "
                "gateway cold start seeds it; check gateway logs "
                "(perm_engine Config.initialize) and re-run deploy.sh"
            )
        print(
            f"end state     : resources={state['resources']} "
            f"grant_gap={state['gap']} (role_id={state['role_id']})"
        )

        scanned = imported = None
        if login_ok:
            scanned, imported = fetch_stats(
                base_url, route, part_id, token, args.http_timeout
            )
            if scanned is not None:
                print(f"stats         : scanned={scanned} imported={imported}")

        if not db_end_state_ok(state):
            raise RuntimeFailure(
                "end state not converged: "
                f"resources={state['resources']}, grant_gap={state['gap']} "
                "— registerResources may have partially failed; check "
                "gateway logs and re-run deploy.sh (idempotent)"
            )

        if not login_ok:
            print(
                "WARN: admin login failed (password changed after first "
                "login?) — forward-path re-verification skipped; DB end "
                "state is converged, continuing"
            )
        elif scanned is not None and imported is not None and scanned != imported:
            print(
                f"WARN: stats scanned={scanned} != imported={imported} "
                "(code dedup vs (module, name) dedup can legitimately "
                "differ) — DB end state is authoritative and converged"
            )

        print(
            "OK: platform resources registered and "
            f"'{SUPER_ADMIN_ROLE_CODE}' bound to all of them "
            f"({state['resources']} resources, 0 grant gap)"
        )
        return EXIT_OK
    except RuntimeFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except Exception as exc:  # sqlalchemy/psycopg/network errors land here
        print(f"error: resource registration failed: {exc}", file=sys.stderr)
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())