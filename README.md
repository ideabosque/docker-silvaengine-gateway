# SilvaEngine Gateway (Docker)

A container image and `docker compose` stack for the
[**SilvaEngine Gateway**](https://github.com/ideabosque/silvaengine_gateway) — a
single FastAPI/uvicorn application that exposes installed SilvaEngine modules
over **MCP (JSON-RPC), REST, GraphQL, and SSE** on one port.

Modeled on `docker-mcp-core-daemon`: a slim Python 3.12 base, `uv` for fast
dependency resolution, and a `supervisor`-managed process lifecycle. Unlike the
core daemon (which split stdio/SSE transports into two processes), the gateway
serves all four protocols from a single uvicorn process, so supervisor runs one
program.

---

## ✨ What you get

| Protocol | Route (per registered module) | Notes |
|---|---|---|
| GraphQL | `POST /{ep}/knowledge_graph_graphql`, `/{ep}/ai_rfq_graphql`, `/{ep}/mcp_daemon_graphql` | One per module |
| REST / JSON-RPC | `POST /{ep}/mcp`, `POST /{ep}/extract`, cache admin, … | MCP JSON-RPC over HTTP |
| SSE | `GET/POST /{ep}/sse` | Server-Sent Events stream + message |
| MCP | JSON-RPC + SSE above | `mcp_daemon_engine` module |
| Health | `GET /health` | Public, used by the healthcheck |
| Auth | `POST /auth/token`, `GET /me` | Local JWT or AWS Cognito |

Routes are declared by the gateway's YAML manifest — see the upstream
[gateway README](https://github.com/ideabosque/silvaengine_gateway).

---

## 📂 Repository layout

```text
.
├── Dockerfile              # Python 3.12-slim + uv + supervisor build
├── docker-compose.yml      # gateway service (Neo4j runs separately)
├── requirements.txt        # git-sourced SilvaEngine deps (engines + gateway)
├── supervisord.conf        # single gateway process under supervisor
├── .env                    # environment (edit before use — secrets!)
├── Makefile                # convenience targets
├── .ssh/                   # deploy key for private ideabosque repos (build-time)
├── data/                   # persisted state ➜ /app/data
└── logs/                   # supervisor & gateway logs ➜ /var/log/supervisor
```

---

## 🔑 Prerequisite: SSH deploy key

The gateway and several of its engine dependencies live in **private** GitHub
repos under `ideabosque`, installed over `git+ssh`. Place a deploy/private key
with read access in `./.ssh/` **before building**:

```bash
cp ~/.ssh/id_ed25519 ./.ssh/id_ed25519     # a key authorized on the repos
chmod 600 ./.ssh/id_ed25519
```

The Dockerfile copies `./.ssh` into the build and trusts `github.com`. The key
files are gitignored (only `.ssh/.gitkeep` is tracked) so they are never
committed.

> For CI/CD, prefer BuildKit SSH forwarding (`docker build --ssh default …`)
> over baking a key into the image. The current Dockerfile uses the copy
> approach to mirror `docker-mcp-core-daemon`.

---

## 🚀 Quick start

```bash
# 1. Configure
cp .env .env.local 2>/dev/null || true   # .env is provided; edit it in place
vi .env                                   # set JWT secret, AWS keys, neo4j_password, openai_api_key

# 2. Add the SSH deploy key (see above)
cp ~/.ssh/id_ed25519 ./.ssh/

# 3. Build & launch
make build
make up           # or: docker compose up -d

# 4. Verify
make health       # curl http://localhost:8000/health
make logs
```

Get a token and call a protected route:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/token \
  -d "username=admin&password=admin123" | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/me
```

---

## ⚙️ Configuration

All configuration is environment-driven via `.env`. Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `CONTAINER_PORT` | `8000` | Published host port |
| `GATEWAY_PORT` | `8000` | In-container uvicorn bind port |
| `GATEWAY_WORKERS` | `1` | Worker processes (>1 needs shared backends — see below) |
| `GATEWAY_AUTH_PROVIDER` | `local` | `local` or `cognito` |
| `JWT_SECRET_KEY` | — | Local JWT signing secret (**change it**) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `admin123` | Bootstrap local admin |
| `region_name` / `aws_access_key_id` / `aws_secret_access_key` | — | AWS (DynamoDB, Cognito) |
| `neo4j_uri` / `neo4j_username` / `neo4j_password` | `bolt://host.docker.internal:7687` | Knowledge Graph Engine backend (external container) |
| `openai_api_key` / `llm_name` | — | LLM for KGE / AI RFQ |
| `MCP_TRANSPORT` | `sse` | Forwarded to `mcp_daemon_engine` |

Infrastructure settings are forwarded from the gateway to each module's
`Config.initialize()` via the route manifest's `config_class` mechanism — no
gateway code changes needed to add a module.

### Backing services

- **Neo4j** (Knowledge Graph Engine) runs in its **own container**. Point the
  gateway at it with `neo4j_uri` in `.env`. To reach a Neo4j on another compose
  network, attach this stack to that network (see the note in
  `docker-compose.yml`) and use the Neo4j service/container name; otherwise use a
  reachable `host:port` (e.g. `bolt://host.docker.internal:7687` on Docker
  Desktop).
- **DynamoDB** (AI RFQ + MCP modules) uses **AWS** via `region_name` and AWS
  credentials in `.env`. Set `initialize_tables=1` to auto-create tables.

### Developing engine modules from host source

By default the engines are pip-installed from git (pinned branches in
`requirements.txt`). For active development you can mount your local source over
the installed copies — edit on the host, **restart** the container (no rebuild):

- `SILVAENGINE_SRC` in `.env` points at your silvaengine repos
  (default `../silvaengine`).
- `docker-compose.yml` bind-mounts each engine package into `/app/src/<pkg>` and
  sets `PYTHONPATH=/app/src`, which Python searches **before** site-packages — so
  the mounted source shadows the git-installed version.
- The git installs stay in place to provide all transitive dependencies; only the
  engine *source* is overridden. This also bypasses the branch-pin problem — the
  container runs your exact working copy, including uncommitted changes.

```bash
# edit ../silvaengine/<engine>/... then:
docker compose restart silvaengine-gateway
```

Uncomment the optional mounts in `docker-compose.yml` to also run the gateway or
shared libs (`silvaengine_utility`, etc.) from source. Verify which copy is live:

```bash
docker exec silvaengine-gateway /opt/venv/bin/python -c \
  "import knowledge_graph_engine as k; print(k.__file__)"
# -> /app/src/knowledge_graph_engine/__init__.py  (host mount)
```

### Scaling (`GATEWAY_WORKERS > 1`)

In-memory task state, rate-limit counters, and the SSE client registry are
**per-process**. With more than one worker, switch to shared backends
(`GATEWAY_TASK_BACKEND=dynamodb`, `GATEWAY_RATE_LIMIT_BACKEND=dynamodb`) and use
sticky sessions for SSE. See the upstream gateway docs for details.

---

## 🛠️ Make targets

| Target | Action |
|---|---|
| `make build` | Build the image |
| `make up` | Start the gateway (detached) |
| `make dev` | Build + run in foreground with logs |
| `make down` | Stop & remove containers |
| `make logs` | Tail combined logs |
| `make gateway-logs` | Tail the gateway process log (supervisor) |
| `make status` | Supervisor process status |
| `make restart` | Restart the gateway process (no rebuild) |
| `make shell` | Shell into the gateway container |
| `make health` | Curl `/health` |
| `make clean` | Down + drop volumes & dangling images |
| `make rebuild` | clean → build → up |

---

## 💡 Troubleshooting

| Symptom | Resolution |
|---|---|
| Build fails cloning git repos | Ensure a valid key is in `./.ssh/` and authorized on the `ideabosque` repos. |
| `Permission denied (publickey)` during build | Key not copied / wrong permissions; `chmod 600 ./.ssh/id_*`. |
| Gateway unhealthy / restarts | `make gateway-logs`; check AWS/Neo4j/LLM credentials in `.env`. |
| `401 Unauthorized` | Get a token via `POST /auth/token`; check `JWT_SECRET_KEY` / Cognito settings. |
| KGE GraphQL errors | Confirm the external Neo4j is reachable from the container (`neo4j_uri`) and `neo4j_password` matches. |
| Port already in use | Change `CONTAINER_PORT` in `.env`. |

---

## 📝 License

MIT.
