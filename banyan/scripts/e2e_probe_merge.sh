#!/bin/bash
# E2E probes for the merged gateway (Banyan hosting built-in).
B="http://127.0.0.1:${GATEWAY_PORT:-8080}"
IDEM="e2e-merge-$(date +%s)"

echo "== P1: anonymous registerUser (expect 200, GraphQL ok) =="
curl -sS -o /tmp/p1.json -w "HTTP %{http_code}\n" -X POST "$B/beta/core/banyan/user_engine_graphql" \
  -H 'content-type: application/json' -H 'part_id: nestaging' \
  -d '{"query":"mutation($k: ID!, $input: UserRegisterInput!){ registerUser(idempotencyKey: $k, input: $input){ user { id username email status } } }","variables":{"k":"'"$IDEM"'","input":{"email":"probe.e2e.merge@banyanos.dev","username":"probe_e2e_merge","password":"Pr0be!Pass9","displayName":"Probe E2E Merge"}}}'
head -c 400 /tmp/p1.json; echo

echo "== P1b: createUser anonymous (expect 401 fail-closed - not whitelisted) =="
curl -sS -o /tmp/p1b.json -w "HTTP %{http_code}\n" -X POST "$B/beta/core/banyan/user_engine_graphql" \
  -H 'content-type: application/json' -H 'part_id: nestaging' \
  -d '{"query":"mutation($k: ID!, $input: UserAdminCreateInput!){ createUser(idempotencyKey: $k, input: $input){ id } }","variables":{"k":"'"$IDEM"'-x","input":{"username":"x_probe","email":"x_probe@banyanos.dev"}}}'
head -c 200 /tmp/p1b.json; echo

echo "== P2: wrong credentials login (expect 200, GraphQL application error) =="
curl -sS -o /tmp/p2.json -w "HTTP %{http_code}\n" -X POST "$B/beta/core/banyan/user_engine_graphql" \
  -H 'content-type: application/json' -H 'part_id: nestaging' \
  -d '{"query":"mutation($k: ID!, $input: LoginInput!){ login(idempotencyKey: $k, input: $input){ user { id username email } } }","variables":{"k":"'"$IDEM"'-login","input":{"email":"admin@banyanos.dev","password":"definitely-wrong-password"}}}'
head -c 300 /tmp/p2.json; echo

echo "== P3: native path without token (expect 401) =="
curl -sS -o /tmp/p3.json -w "HTTP %{http_code}\n" -X POST "$B/user_engine_graphql" \
  -H 'content-type: application/json' -H 'part_id: nestaging' \
  -d '{"query":"{ me { user_id } }"}'
head -c 200 /tmp/p3.json; echo

echo "== P4: bad-signature Banyan JWT (expect 401) =="
curl -sS -o /tmp/p4.json -w "HTTP %{http_code}\n" -X POST "$B/beta/core/banyan/user_engine_graphql" \
  -H 'content-type: application/json' -H 'part_id: nestaging' \
  -H 'authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1LTEiLCJpc3MiOiJiYW55YW4iLCJhdWQiOiJiYW55YW4tZ3JhcGhxbCJ9.bad-signature-000' \
  -d '{"query":"{ me { user_id } }"}'
head -c 200 /tmp/p4.json; echo

echo "== P5: missing part_id (expect 400) =="
curl -sS -o /tmp/p5.json -w "HTTP %{http_code}\n" -X POST "$B/beta/core/banyan/user_engine_graphql" \
  -H 'content-type: application/json' \
  -d '{"query":"{ me { user_id } }"}'
head -c 200 /tmp/p5.json; echo