#!/bin/bash
# Super-admin login loop against the merged gateway.
B="http://127.0.0.1:8000"
ENVF="banyan/.env"
# shellcheck disable=SC1090
eval "$(grep -E '^(ADMIN_ACCOUNT|ADMIN_PASSWORD)=' "$ENVF" | sed 's/^/export /')"
IDEM="e2e-admin-$(date +%s)"
echo "admin account: $ADMIN_ACCOUNT"

echo "== L1: super-admin login =="
curl -sS -o /tmp/l1.json -w "HTTP %{http_code}\n" -X POST "$B/beta/core/banyan/user_engine_graphql" \
  -H 'content-type: application/json' -H 'part_id: nestaging' \
  -d '{"query":"mutation($k: ID!, $input: LoginInput!){ login(idempotencyKey: $k, input: $input){ authToken user { id username email } } }","variables":{"k":"'"$IDEM"'","input":{"email":"'"$ADMIN_ACCOUNT"'","password":"'"$ADMIN_PASSWORD"'"}}}'
head -c 200 /tmp/l1.json; echo

TOKEN=$(python3 -c "import json;print(json.load(open('/tmp/l1.json'))['data']['login']['authToken'])" 2>/dev/null)
if [ -z "$TOKEN" ]; then echo "FAIL: no authToken"; exit 1; fi
echo "token: ${TOKEN:0:25}..."

echo "== L2: me query with super-admin token (expect platform:super_admin role) =="
curl -sS -o /tmp/l2.json -w "HTTP %{http_code}\n" -X POST "$B/beta/core/banyan/user_engine_graphql" \
  -H 'content-type: application/json' -H 'part_id: nestaging' \
  -H "authorization: Bearer $TOKEN" \
  -d '{"query":"{ me { id username email tenantId roles } }"}'
head -c 500 /tmp/l2.json; echo