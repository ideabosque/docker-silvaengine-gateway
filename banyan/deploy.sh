#!/usr/bin/env bash
# =============================================================================
# SilvaEngine Gateway — Banyan 一键部署脚本（生产镜像模式 · P1-A）
# =============================================================================
# 目标服务器只需预装 Docker（含 Compose v2 插件）；本机开发/测试支持
# Podman + compose provider（docker-compose v2 二进制或 podman-compose）。
# 无需 Python / jq / awscli / rsync —— 脚本只依赖 bash + tar + 基本 coreutils。
#
# 本目录（banyan/）自包含：Dockerfile + docker-compose.yml + requirements.txt
# + env/ 模板 + scripts/ddb_init.py。与网关仓 deploy/（bind-mount 变体）的
# 手工路径等价，但 gateway 源码打进镜像而非宿主机挂载。
#
#   阶段 1  环境检测        Docker+Compose v2 或 Podman+compose provider
#   阶段 2  配置与源码校验  .env 与种子 JSON 生成/复用（四态状态机）+
#                          网关包 / 12 引擎 / vendor / 三框架源码树校验
#   阶段 3  端口预检        8000/8001/5432/6379/7474/7687（区分占用者与 compose 项目）
#   阶段 4  暂存构建上下文  tar 排除 .git/.venv/__pycache__ 等 → .build-context/
#                          （框架双布局归一化为包目录形态）→ 计算源码 digest
#   阶段 5  镜像构建        digest 与既有镜像 label 相同则跳过（--force-build 覆盖）
#   阶段 6  启动 ddb-local  DynamoDB Local（-inMemory -sharedDb）
#   阶段 7  ddb-init        建表 → 灌种子（覆盖写，幂等）→ 行数核验
#   阶段 8  启动五服务      ddb-local postgres neo4j redis gateway
#   阶段 9  健康与日志验证  容器 healthy + /health 探活 + 两条关键启动日志
#   阶段 10 超管初始化      admin-init 幂等收敛：超管用户 + platform:super_admin
#                          绑定（密码永不覆盖）+ 终态核验 + 摘要
#   阶段 11 资源注册与      resource-init：超管 login → registerResources（与
#           根角色授权收敛  前端「资源注册」同一正向通道，含审计行；资源差量入库 +
#                          预设角色全量 PERMIT 授权）→ DB 反连接终态核验；改密后
#                          重部署降级为 DB 终态核验（WARN 放行，幂等重跑不阻塞）
#
# 用法：
#   bash deploy.sh                # 部署/更新（幂等，可重复执行）
#   bash deploy.sh status         # 状态与健康检查
#   bash deploy.sh down [-v]      # 停止清理（-v 连数据卷一起删除）
#   bash deploy.sh --restart      # 部署后重启 gateway
#   bash deploy.sh --force-build  # 强制重建镜像（默认源码未变自动跳过）
#   bash deploy.sh --force-env    # 重新生成 .env 与种子 JSON（密码会变）
#   bash deploy.sh --dry-run      # 跑到阶段 4（含 staging 与 digest），不构建不起容器
#   bash deploy.sh --self-test    # 内置纯逻辑自检（不碰 docker/podman）
#
# 环境变量覆盖（路径类仅构建期使用，不写入 .env）：
#   TENANT_PART_ID（默认 nestaging）
#   GATEWAY_PACKAGE_DIR、BANYAN_MODULES_DIR、SILVAENGINE_BASE_DIR、
#   SILVAENGINE_UTILITY_DIR、SILVAENGINE_CONNECTIONS_DIR、VENDOR_DIR
# 构建期（不写入 .env，按运行时环境取值）：
#   PYTHON_IMAGE（默认 docker.m.daocloud.io/library/python:3.12-slim）
#   PIP_INDEX_URL（默认阿里云 pypi 源）
# 首次生成 .env 时写入的数据面镜像（生成后改 .env 生效）：
#   POSTGRES_IMAGE / NEO4J_IMAGE / REDIS_IMAGE / DDB_LOCAL_IMAGE（默认 daocloud 加速源）
# 随时可用：GATEWAY_WAIT_TIMEOUT（健康等待上限秒数，默认 600）
#
# 源码路径发现链（默认值，可用上述环境变量覆盖）：
#   banyan/.. = 本仓根；仓根/.. = 工作区目录（ideabosque，含 silvaengine_gateway/
#   banyan/modules/、三框架仓）；工作区/.. /docker/api-runtime/vendor（vendor 快照）。
#   服务器上请按 README.md §前置条件 保持同样相对布局 rsync。
#
# 幂等性：重复运行复用 .env 与种子 JSON；源码未变（digest 相同）跳过镜像构建；
# ddb-init 每次重灌种子（-inMemory 重建自愈）。任何阶段失败安全退出并给出
# 原因与排查建议；修复后直接重跑即可，整体回滚：bash deploy.sh down。
#
# 兼容性：bash 3.2+（macOS 自带版本可用），无关联数组 / readarray 依赖。
# =============================================================================
set -Eeuo pipefail
umask 077
export LC_ALL=C

DEPLOY_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DEPLOY_DIR"
REPO_ROOT=$(cd "$DEPLOY_DIR/.." && pwd)
WS_DIR=$(cd "$REPO_ROOT/.." && pwd)
UP_DIR=$(cd "$WS_DIR/.." && pwd)

ENV_FILE="$DEPLOY_DIR/.env"
SEED_DIR="$DEPLOY_DIR/env"
EXAMPLE_JSON="$SEED_DIR/se-configdata.example.json"
SEED_JSON="$SEED_DIR/se-configdata.local.json"
BUILD_CONTEXT="$DEPLOY_DIR/.build-context"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.yml"
DOCKERFILE="$DEPLOY_DIR/Dockerfile"
REQUIREMENTS="$DEPLOY_DIR/requirements.txt"

COMPOSE_PROJECT="banyan"
export COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT"

GATEWAY_IMAGE_DEFAULT="silvaengine-gateway-banyan:latest"
DIGEST_LABEL="org.silvaengine.banyan.source-digest"

UP_SERVICES=(ddb-local postgres neo4j redis gateway)
HOST_PORTS=(8000 8001 5432 6379 7474 7687)
ENGINE_REPOS=(agent_engine capability_engine knowledge_engine llm_engine \
  memory_engine merchant_engine monitor_engine orchestration_engine \
  perm_engine prompt_engine setting_engine user_engine)
VENDOR_PKGS=(silvaengine_constants silvaengine_definitions silvaengine_dynamodb_base)
FRAMEWORK_PKGS=(silvaengine_base silvaengine_utility silvaengine_connections)
REQUIRED_LOGS=("se-configdata setting loaded: setting_id=" \
  "Pool bootstrap: framework pools created")
OPTIONAL_LOGS=("repo-root layout detected" "Rewrote [0-9]+ httpx pool base_url")

REQUIRED_ENV_KEYS=(GATEWAY_IMAGE TENANT_PART_ID POSTGRES_USER POSTGRES_PASSWORD \
  POSTGRES_DB NEO4J_AUTH REDIS_PASSWORD SE_CONFIGDATA_ENDPOINT_URL)

WAIT_TIMEOUT="${GATEWAY_WAIT_TIMEOUT:-600}"
PYTHON_IMAGE="${PYTHON_IMAGE:-docker.m.daocloud.io/library/python:3.12-slim}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"

FORCE_ENV=0
FORCE_BUILD=0
DRY_RUN=0
RESTART=0
VOLUMES=0
MODE="up"

RUNTIME_BIN=""
COMPOSE=()

CURRENT_STAGE="0"
STAGE_DESC="初始化"
STAGE_HINT=""

log() { printf '[deploy.sh] %s\n' "$*"; }

set_stage() {
  CURRENT_STAGE="$1"
  STAGE_DESC="$2"
  STAGE_HINT="$3"
  log "── 阶段 $1/10：$2"
}

die() {
  printf '\n[deploy.sh] X 阶段 %s（%s）失败：%s\n' \
    "$CURRENT_STAGE" "$STAGE_DESC" "$*" >&2
  if [ -n "$STAGE_HINT" ]; then
    printf '[deploy.sh] 排查建议：%s\n' "$STAGE_HINT" >&2
  fi
  printf '[deploy.sh] 本脚本幂等——修复后可直接重跑；整体回滚：bash deploy.sh down（-v 连数据卷）\n' >&2
  exit 1
}

on_error() {
  local code=$?
  printf '\n[deploy.sh] X 阶段 %s（%s）内部异常（退出码 %s，行 %s）。\n' \
    "$CURRENT_STAGE" "$STAGE_DESC" "$code" "${BASH_LINENO[0]:-?}" >&2
  if [ -n "$STAGE_HINT" ]; then
    printf '[deploy.sh] 排查建议：%s\n' "$STAGE_HINT" >&2
  fi
  printf '[deploy.sh] 本脚本幂等——修复后可直接重跑；整体回滚：bash deploy.sh down（-v 连数据卷）\n' >&2
  exit "$code"
}
trap on_error ERR

usage() {
  cat <<'USAGE'
SilvaEngine Gateway — Banyan 一键部署（生产镜像模式：源码打进镜像）

用法: bash deploy.sh [命令] [选项]

命令:
  (默认)      部署/更新（幂等，可重复执行；源码未变自动跳过构建）
  status      容器状态 + 健康检查 + 关键日志核查
  down        停止并清理（数据卷保留）；down -v 连数据卷一起删除

选项:
  --restart    部署后重启 gateway
  --force-build 强制重建镜像（忽略 digest 跳过逻辑）
  --force-env  删除并重新生成 .env 与种子 JSON（密码会变更）
  --dry-run    跑到暂存与 digest 为止，不构建、不起容器
  --self-test  内置纯逻辑自检（不碰 docker/podman）
  -h, --help   显示本帮助

运行时（自动探测，docker 优先，podman 兜底）:
  服务器: Docker + Compose v2（docker-compose-plugin）
  本机:   Podman + docker-compose v2 二进制（brew install docker-compose）
          或 podman-compose；macOS 需先 podman machine start

环境变量（路径类仅构建期使用；镜像类在首次生成 .env 时写入）:
  TENANT_PART_ID              租户 part_id（默认 nestaging）
  GATEWAY_PACKAGE_DIR         网关包目录（默认 <工作区>/silvaengine_gateway/silvaengine_gateway）
  BANYAN_MODULES_DIR          Banyan 12 引擎目录（默认 <工作区>/banyan/modules）
  SILVAENGINE_BASE_DIR        框架仓 silvaengine_base（默认 <工作区>/silvaengine_base）
  SILVAENGINE_UTILITY_DIR     框架仓 silvaengine_utility
  SILVAENGINE_CONNECTIONS_DIR 框架仓 silvaengine_connections
  VENDOR_DIR                  vendor 快照（默认 <工作区>/../docker/api-runtime/vendor）
  PYTHON_IMAGE                构建用基础镜像（默认 daocloud 加速源）
  PIP_INDEX_URL               构建用 pip 源（默认阿里云）
  POSTGRES_IMAGE / NEO4J_IMAGE / REDIS_IMAGE / DDB_LOCAL_IMAGE
                              数据面镜像（首次生成 .env 时的默认值）
  GATEWAY_WAIT_TIMEOUT        健康等待上限秒数（默认 600）
USAGE
}

# ---------------------------------------------------------------------------
# 基础工具函数
# ---------------------------------------------------------------------------

# compose 命令（依赖 detect_runtime 已填充 COMPOSE 数组）。
# 项目名经 COMPOSE_PROJECT_NAME 注入（banyan），compose 文件取当前目录默认名，
# 规避 podman compose wrapper 对子命令前全局参数的兼容性差异。
dc() {
  if [ "${#COMPOSE[@]}" -eq 0 ]; then
    die "运行时未探测（内部错误：dc 先于 detect_runtime 调用）"
  fi
  "${COMPOSE[@]}" "$@"
}

# 随机字母数字串（urandom | tr | head；管道尾 || true 防 SIGPIPE × pipefail）
rand_alnum() {
  local n="$1" out
  out=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$n" || true)
  printf '%s' "$out"
}

# sed 替换串转义（\ / & ；本脚本用 | 作分隔符，仍按通用规则转义）
sed_escape() {
  printf '%s' "$1" | sed 's/[\\/&]/\\&/g' || true
}

# 依序返回第一个存在的候选路径；全不存在则返回非零
first_existing() {
  local c
  for c in "$@"; do
    if [ -e "$c" ]; then
      printf '%s' "$c"
      return 0
    fi
  done
  return 1
}

# 从 .env 读键值（剥离成对引号；缺失返回空串）
env_get() {
  local key="$1" line val
  line=$(grep -m1 "^${key}=" "$ENV_FILE" 2>/dev/null || true)
  val="${line#*=}"
  case "$val" in
    \"*\") val="${val#\"}"; val="${val%\"}" ;;
    \'*\') val="${val#\'}"; val="${val%\'}" ;;
  esac
  printf '%s' "$val"
}

# 端口被占用返回 0；空闲返回非零。优先 timeout 限时（无 timeout 的 macOS 直连，
# 127.0.0.1 上未监听端口会立即收到 RST，不会悬挂）
port_busy() {
  local port="$1"
  if command -v timeout >/dev/null 2>&1; then
    if timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/$port" >/dev/null 2>&1; then
      return 0
    fi
  else
    if bash -c "exec 3<>/dev/tcp/127.0.0.1/$port" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

# ---------------------------------------------------------------------------
# 阶段 1：运行时探测（docker 优先，podman 兜底）
# ---------------------------------------------------------------------------

detect_runtime() {
  local out cv docker_reason=""

  RUNTIME_BIN=""
  COMPOSE=()

  if command -v docker >/dev/null 2>&1; then
    if out=$(docker info 2>&1); then
      cv=$(docker compose version --short 2>&1 || true)
      case "$cv" in
        v2*|2*)
          RUNTIME_BIN=docker
          COMPOSE=(docker compose)
          log "运行时：Docker（Compose $cv）"
          return 0
          ;;
        *)
          docker_reason="Docker 可用但未检出 Compose v2（当前：${cv:-未检出}）"
          ;;
      esac
    elif printf '%s' "$out" | grep -qi 'permission denied'; then
      docker_reason="Docker 守护进程连接被拒（permission denied，用户可能不在 docker 组）"
    else
      docker_reason="Docker 守护进程未运行或连接失败"
    fi
  else
    docker_reason="未找到 docker 命令"
  fi

  if command -v podman >/dev/null 2>&1; then
    if out=$(podman info 2>&1); then
      if out=$(podman compose version 2>&1); then
        RUNTIME_BIN=podman
        COMPOSE=(podman compose)
        log "运行时：Podman（compose provider：$(printf '%s' "$out" | head -1)）"
        if [ -n "$docker_reason" ]; then
          log "提示：Docker 不可用（$docker_reason），已切换 Podman"
        fi
        return 0
      fi
      die "podman 可用但未找到 compose provider——安装 docker-compose v2 二进制（brew install docker-compose / apt install docker-compose-plugin）或 pip install podman-compose 后重跑。Docker 侧情况：$docker_reason"
    fi
    die "podman 已安装但守护进程/machine 未运行——macOS：podman machine start（无 machine 先 podman machine init）；Linux：检查 podman.socket（systemctl --user start podman.socket）。或修复 Docker：$docker_reason"
  fi

  die "未找到可用的 docker 或 podman——请安装 Docker（含 Compose v2 插件）：https://docs.docker.com/engine/install/；本机 Podman 方案另需 docker-compose v2 或 podman-compose"
}

# 容器健康状态（docker 用 .State.Health.Status；podman 用 .State.HealthStatus）
health_state() {
  local st
  st=$("$RUNTIME_BIN" inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null || true)
  if [ -z "$st" ] || [ "$st" = "<no value>" ]; then
    st=$("$RUNTIME_BIN" inspect -f '{{.State.HealthStatus}}' "$1" 2>/dev/null || true)
  fi
  printf '%s' "$st"
}

# 读取镜像 label（不存在返回空串）
image_label() {
  "$RUNTIME_BIN" image inspect --format "{{index .Config.Labels \"$2\"}}" \
    "$1" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 阶段 2：配置生成与校验
# ---------------------------------------------------------------------------

# 把 .env 相关键读入 shell 变量（构建/渲染/摘要使用）
load_env_values() {
  TENANT_PART_ID=$(env_get TENANT_PART_ID)
  PG_USER=$(env_get POSTGRES_USER)
  PG_PASSWORD=$(env_get POSTGRES_PASSWORD)
  PG_DB=$(env_get POSTGRES_DB)
  NEO4J_AUTH=$(env_get NEO4J_AUTH)
  NEO4J_PASSWORD="${NEO4J_AUTH#neo4j/}"
  REDIS_PASSWORD=$(env_get REDIS_PASSWORD)
  GATEWAY_IMAGE=$(env_get GATEWAY_IMAGE)
}

# 配置值必须纯字母数字：本脚本用 sed 渲染种子 JSON，放宽字符集会引入
# 注入/转义问题；含特殊字符的密码请自行改用模式 B 手动灌表
assert_alnum() {
  local name="$1" val="$2"
  if [ -z "$val" ]; then
    die "配置值校验失败：$name 为空（.env）"
  fi
  case "$val" in
    *[!A-Za-z0-9]*)
      die "配置值校验失败：$name 含非字母数字字符。种子 JSON 由 sed 渲染，为避免转义问题，请把 .env 中该值改为纯字母数字，删除 env/se-configdata.local.json 后重跑（或 --force-env 重新生成）"
      ;;
  esac
}

# 首次部署：生成随机密码 + 数据面镜像默认值 → .env（不含源码路径，路径仅构建期使用）
generate_env() {
  local part_id pg_pw neo4j_pw redis_pw tmp_f
  part_id="${TENANT_PART_ID:-nestaging}"
  pg_pw=$(rand_alnum 16)
  neo4j_pw=$(rand_alnum 16)
  redis_pw=$(rand_alnum 16)

  tmp_f="$ENV_FILE.tmp.$$"
  cat > "$tmp_f" <<EOF
# SilvaEngine Gateway banyan/.env —— 由 deploy.sh 自动生成，勿提交（含密码）。
# 修改后直接重跑 bash deploy.sh 即可生效；键说明见 banyan/README.md。

# --- 网关镜像（deploy.sh 构建；ddb-init 复用同一镜像）---
GATEWAY_IMAGE=${GATEWAY_IMAGE_DEFAULT}

# --- 租户 ---
TENANT_PART_ID=${part_id}

# --- 数据面镜像（China 加速默认；可换官方/其他源，改后重跑生效）---
POSTGRES_IMAGE=${POSTGRES_IMAGE:-docker.m.daocloud.io/library/postgres:16}
NEO4J_IMAGE=${NEO4J_IMAGE:-docker.m.daocloud.io/library/neo4j:5.26-ubi10}
REDIS_IMAGE=${REDIS_IMAGE:-docker.m.daocloud.io/library/redis:7}
DDB_LOCAL_IMAGE=${DDB_LOCAL_IMAGE:-docker.m.daocloud.io/amazon/dynamodb-local:latest}

# --- 数据面凭据（密码随机生成，纯字母数字）---
POSTGRES_USER=banyan
POSTGRES_PASSWORD=${pg_pw}
POSTGRES_DB=banyan
NEO4J_AUTH=neo4j/${neo4j_pw}
REDIS_PASSWORD=${redis_pw}

# --- 超级管理员（阶段 10 admin-init 使用；默认密码建议部署后立即修改）---
ADMIN_ACCOUNT=${ADMIN_ACCOUNT:-admin@banyanos.dev}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-B@nyan0s.d3v}

# --- se-configdata：模式 A 读 DDB Local（离线，容器内地址）---
SE_CONFIGDATA_ENDPOINT_URL=http://ddb-local:8000
# boto3 对 DDB Local 仍需签名凭据（假凭据即可，DDB Local 不校验）
region_name=us-west-2
aws_access_key_id=local
aws_secret_access_key=local

# --- 可选覆盖（取消注释生效）---
# SE_CONFIGDATA_TABLE=se-configdata
# SILVAENGINE_CACHE_TTL=300
# BANYAN_LOOPBACK_BASE_URL=http://127.0.0.1:8000/beta/core/banyan
# ENDPOINT_ID=banyan
# ADAPTER_STAGE=beta
# ADAPTER_AREA=core
EOF
  mv -f "$tmp_f" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  log "已生成 .env（随机密码 16 位；JWT/x-api-key 密钥仅在种子 JSON 中）"
}

# 超管键补全：存量 .env（本批次前生成）缺 ADMIN_ACCOUNT/ADMIN_PASSWORD 时追加默认值；
# 已有值（含运维自定义）不覆盖。密码含 @ 与 .——非纯字母数字，严禁走 assert_alnum/
# 种子 JSON sed 渲染路径（heredoc/env_file 直写是唯一安全通道）。
ensure_admin_env_keys() {
  local changed=0
  if [ -z "$(env_get ADMIN_ACCOUNT)" ]; then
    printf '\n# --- 超级管理员（阶段 10 admin-init 使用）---\nADMIN_ACCOUNT=%s\n' \
      "${ADMIN_ACCOUNT:-admin@banyanos.dev}" >> "$ENV_FILE"
    changed=1
  fi
  if [ -z "$(env_get ADMIN_PASSWORD)" ]; then
    printf 'ADMIN_PASSWORD=%s\n' \
      "${ADMIN_PASSWORD:-B@nyan0s.d3v}" >> "$ENV_FILE"
    changed=1
  fi
  if [ "$changed" = "1" ]; then
    chmod 600 "$ENV_FILE"
    log "已补全 .env 超管键（ADMIN_ACCOUNT/ADMIN_PASSWORD，默认值；已有值不覆盖）"
  fi
}

# 按 .env 渲染种子 JSON：替换 8 个占位符 + 删 _comment 行 + 渲染后核验
render_seed_json() {
  local jwt api_key sed_script tmp_f

  assert_alnum "TENANT_PART_ID" "$TENANT_PART_ID"
  assert_alnum "POSTGRES_USER" "$PG_USER"
  assert_alnum "POSTGRES_DB" "$PG_DB"
  assert_alnum "POSTGRES_PASSWORD" "$PG_PASSWORD"
  assert_alnum "NEO4J_PASSWORD" "$NEO4J_PASSWORD"
  assert_alnum "REDIS_PASSWORD" "$REDIS_PASSWORD"
  jwt=$(rand_alnum 32)
  api_key=$(rand_alnum 32)
  assert_alnum "JWT_SECRET" "$jwt"
  assert_alnum "ENV_API_KEY" "$api_key"

  sed_script=$(mktemp) || die "mktemp 失败"
  tmp_f=$(mktemp) || { rm -f "$sed_script"; die "mktemp 失败"; }
  {
    printf 's|<TENANT_PART_ID>|%s|g\n' "$(sed_escape "$TENANT_PART_ID")"
    printf 's|<REPLACE_WITH_BANYAN_JWT_SECRET>|%s|g\n' "$(sed_escape "$jwt")"
    printf 's|<NEO4J_PASSWORD>|%s|g\n' "$(sed_escape "$NEO4J_PASSWORD")"
    printf 's|<REDIS_PASSWORD>|%s|g\n' "$(sed_escape "$REDIS_PASSWORD")"
    printf 's|<POSTGRES_DB>|%s|g\n' "$(sed_escape "$PG_DB")"
    printf 's|<POSTGRES_USER>|%s|g\n' "$(sed_escape "$PG_USER")"
    printf 's|<POSTGRES_PASSWORD>|%s|g\n' "$(sed_escape "$PG_PASSWORD")"
    printf 's|<ENV_API_KEY>|%s|g\n' "$(sed_escape "$api_key")"
    printf '/^[[:space:]]*"_comment"[[:space:]]*:/d\n'
  } > "$sed_script"
  sed -f "$sed_script" "$EXAMPLE_JSON" > "$tmp_f" \
    || { rm -f "$sed_script" "$tmp_f"; die "种子 JSON 渲染失败（sed 异常）"; }
  rm -f "$sed_script"

  if grep -q '<[A-Z][A-Z0-9_]*>' "$tmp_f"; then
    rm -f "$tmp_f"
    die "种子 JSON 仍残留未替换占位符——env/se-configdata.example.json 可能被改动过"
  fi
  if grep -q '"_comment"' "$tmp_f"; then
    rm -f "$tmp_f"
    die "种子 JSON 仍含 _comment 键（会被当垃圾变量灌入 DDB）"
  fi
  mv -f "$tmp_f" "$SEED_JSON"
  chmod 600 "$SEED_JSON"
  log "已渲染 env/se-configdata.local.json（含随机 JWT_SECRET 与 x-api-key）"
}

# 必填键非空校验（收集全部缺失后一次报错）
validate_env_keys() {
  local missing=() k v
  for k in "${REQUIRED_ENV_KEYS[@]}"; do
    v=$(env_get "$k")
    if [ -z "$v" ]; then
      missing+=("$k")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    die ".env 缺少必填键：${missing[*]}。手工补全，或删除 .env 与 env/se-configdata.local.json 后重跑自动生成（或 --force-env）"
  fi
}

# 源码路径发现（环境变量覆盖优先；仅构建期使用，不写入 .env）
resolve_source_paths() {
  if [ -z "${GATEWAY_PACKAGE_DIR:-}" ]; then
    if ! GATEWAY_PACKAGE_DIR=$(first_existing "$WS_DIR/silvaengine_gateway/silvaengine_gateway"); then
      GATEWAY_PACKAGE_DIR=""
    fi
  fi
  if [ -z "${BANYAN_MODULES_DIR:-}" ]; then
    if ! BANYAN_MODULES_DIR=$(first_existing "$WS_DIR/banyan/modules"); then
      BANYAN_MODULES_DIR=""
    fi
  fi
  if [ -z "${SILVAENGINE_BASE_DIR:-}" ]; then
    if ! SILVAENGINE_BASE_DIR=$(first_existing "$WS_DIR/silvaengine_base"); then
      SILVAENGINE_BASE_DIR=""
    fi
  fi
  if [ -z "${SILVAENGINE_UTILITY_DIR:-}" ]; then
    if ! SILVAENGINE_UTILITY_DIR=$(first_existing "$WS_DIR/silvaengine_utility"); then
      SILVAENGINE_UTILITY_DIR=""
    fi
  fi
  if [ -z "${SILVAENGINE_CONNECTIONS_DIR:-}" ]; then
    if ! SILVAENGINE_CONNECTIONS_DIR=$(first_existing "$WS_DIR/silvaengine_connections"); then
      SILVAENGINE_CONNECTIONS_DIR=""
    fi
  fi
  if [ -z "${VENDOR_DIR:-}" ]; then
    if ! VENDOR_DIR=$(first_existing "$UP_DIR/docker/api-runtime/vendor"); then
      VENDOR_DIR=""
    fi
  fi
}

# 框架仓双布局解析：repo 根即包（根有 __init__.py）或内含 <pkg>/ 包目录。
# 输出包目录路径；无法识别返回非零。
fw_src() {
  if [ -f "$1/__init__.py" ]; then
    printf '%s' "$1"
    return 0
  fi
  if [ -f "$1/$2/__init__.py" ]; then
    printf '%s' "$1/$2"
    return 0
  fi
  return 1
}

# 源码树校验（收集全部问题后一次报错，便于一次性修复）
validate_source_paths() {
  local problems=() f d fw fwdir

  for f in __init__.py app.py auth/middleware.py; do
    if [ ! -f "$GATEWAY_PACKAGE_DIR/$f" ]; then
      problems+=("网关包缺文件：${GATEWAY_PACKAGE_DIR}/$f（环境变量：GATEWAY_PACKAGE_DIR）")
    fi
  done
  for d in "${ENGINE_REPOS[@]}"; do
    if [ ! -d "$BANYAN_MODULES_DIR/$d" ]; then
      problems+=("Banyan 引擎目录缺失：${BANYAN_MODULES_DIR}/$d（环境变量：BANYAN_MODULES_DIR）")
    fi
  done
  for d in "${VENDOR_PKGS[@]}"; do
    if [ ! -f "$VENDOR_DIR/$d/__init__.py" ]; then
      problems+=("vendor 包缺失：${VENDOR_DIR}/$d（环境变量：VENDOR_DIR）")
    fi
  done
  for fw in silvaengine_base silvaengine_utility silvaengine_connections; do
    case "$fw" in
      silvaengine_base) fwdir="$SILVAENGINE_BASE_DIR" ;;
      silvaengine_utility) fwdir="$SILVAENGINE_UTILITY_DIR" ;;
      silvaengine_connections) fwdir="$SILVAENGINE_CONNECTIONS_DIR" ;;
    esac
    if ! fw_src "$fwdir" "$fw" >/dev/null 2>&1; then
      problems+=("框架仓布局无法识别：$fwdir（环境变量：SILVAENGINE_${fw#silvaengine_}_DIR）")
    fi
  done
  if [ ! -f "$REQUIREMENTS" ]; then
    problems+=("缺少 banyan/requirements.txt")
  fi
  if [ ! -f "$EXAMPLE_JSON" ]; then
    problems+=("缺少模板 env/se-configdata.example.json")
  fi
  if [ ! -f "$DOCKERFILE" ]; then
    problems+=("缺少 banyan/Dockerfile")
  fi
  if [ ! -f "$COMPOSE_FILE" ]; then
    problems+=("缺少 banyan/docker-compose.yml")
  fi

  if [ "${#problems[@]}" -gt 0 ]; then
    printf '[deploy.sh] 源码树校验未通过（%s 项）：\n' "${#problems[@]}" >&2
    for d in "${problems[@]}"; do
      printf '  - %s\n' "$d" >&2
    done
    die "源码树不完整——按上方清单补齐后重跑；源路径可用环境变量覆盖（见 -h）"
  fi
  log "源码树校验通过：网关包 + 12 引擎 + vendor 3 包 + 3 框架 + 构建文件"
}

# 四态状态机：复用 / 生成 / 仅 .env 补渲染 / 孤儿报错
ensure_config() {
  if [ "$FORCE_ENV" = "1" ]; then
    log "--force-env：删除并重新生成 .env 与种子 JSON（密码将变更）"
    rm -f "$ENV_FILE" "$SEED_JSON"
  fi

  local have_env=0 have_json=0
  if [ -f "$ENV_FILE" ]; then have_env=1; fi
  if [ -f "$SEED_JSON" ]; then have_json=1; fi

  if [ "$have_env" = "1" ] && [ "$have_json" = "1" ]; then
    log "复用现有配置：.env + env/se-configdata.local.json"
    load_env_values
  elif [ "$have_env" = "0" ] && [ "$have_json" = "0" ]; then
    log "首次部署：自动生成 .env（随机密码）并渲染种子 JSON"
    generate_env
    load_env_values
    render_seed_json
  elif [ "$have_env" = "1" ]; then
    log "种子 JSON 缺失——按现有 .env 重新渲染（密码/JWT 会重生成）"
    load_env_values
    render_seed_json
  else
    die "孤儿状态：env/se-configdata.local.json 存在但 .env 缺失（两者密码会不一致）。请恢复 .env，或两者都删除后重跑（或 --force-env 重新生成全部密码）"
  fi

  validate_env_keys
  ensure_admin_env_keys
  resolve_source_paths
  validate_source_paths
}

# ---------------------------------------------------------------------------
# 阶段 3：端口预检
# ---------------------------------------------------------------------------

check_ports() {
  local p holder proj
  for p in "${HOST_PORTS[@]}"; do
    if port_busy "$p"; then
      holder=$("$RUNTIME_BIN" ps --format '{{.Names}} {{.Ports}}' 2>/dev/null \
        | grep ":$p->" | head -1 | cut -d' ' -f1 || true)
      if [ -n "$holder" ]; then
        proj=$("$RUNTIME_BIN" inspect \
          -f '{{index .Config.Labels "com.docker.compose.project"}}' \
          "$holder" 2>/dev/null || true)
        if [ "$proj" = "$COMPOSE_PROJECT" ]; then
          log "端口 $p 被本栈容器（$holder）占用——重复运行场景，继续"
        else
          die "端口 $p 被其他容器占用（name=$holder，compose 项目=${proj:-未知}）——请停止该容器或调整端口映射；若为网关仓 bind-mount 变体（deploy/）的容器，两形态请勿同时运行"
        fi
      else
        die "端口 $p 被宿主机进程占用——排查：lsof -i :$p（macOS）/ ss -ltnp | grep :$p（Linux）"
      fi
    fi
  done
  log "端口预检通过：${HOST_PORTS[*]}"
}

# ---------------------------------------------------------------------------
# 阶段 4：暂存构建上下文 + 源码 digest
# ---------------------------------------------------------------------------

# tar 排除清单（bsdtar/GNU tar 双兼容模式：'./x' 根级 + '*/x' 任意深 + '*.pyc' 后缀）
TAR_EXCLUDES=(--exclude './.git' --exclude '*/.git' \
  --exclude './.venv' --exclude '*/.venv' \
  --exclude './venv' --exclude '*/venv' \
  --exclude './__pycache__' --exclude '*/__pycache__' \
  --exclude './.pytest_cache' --exclude '*/.pytest_cache' \
  --exclude './.mypy_cache' --exclude '*/.mypy_cache' \
  --exclude './.ruff_cache' --exclude '*/.ruff_cache' \
  --exclude './.tox' --exclude '*/.tox' \
  --exclude './node_modules' --exclude '*/node_modules' \
  --exclude './.DS_Store' --exclude '*/.DS_Store' \
  --exclude './.idea' --exclude '*/.idea' \
  --exclude './.vscode' --exclude '*/.vscode' \
  --exclude '*.pyc' --exclude '*.pyo' --exclude '*.egg-info')

# 目录拷贝（tar 流式，排除垃圾；服务器可能无 rsync）
stage_copy() {
  local src="$1" dst="$2"
  mkdir -p "$dst" || return 1
  (cd "$src" && tar "${TAR_EXCLUDES[@]}" -cf - .) | (cd "$dst" && tar -xf -)
}

# 暂存全部源码到 .build-context/（每次全量重建，幂等且无陈旧残留）：
#   silvaengine_gateway/   ← 网关包目录内容
#   silvaengine_base/ 等   ← 框架仓（双布局归一化为包目录形态）
#   modules/               ← 12 引擎仓根（保持 modules/<repo>/ 双层布局）
#   vendor/                ← vendor 快照 3 包
#   requirements.txt       ← 本目录副本
stage_context() {
  local fw fwdir src junk
  log "暂存构建上下文 → $BUILD_CONTEXT（排除 .git/.venv/__pycache__ 等）"
  rm -rf "$BUILD_CONTEXT"
  mkdir -p "$BUILD_CONTEXT" || die "创建 .build-context/ 失败"

  if ! stage_copy "$GATEWAY_PACKAGE_DIR" "$BUILD_CONTEXT/silvaengine_gateway"; then
    die "暂存网关包失败（源：$GATEWAY_PACKAGE_DIR）"
  fi
  for fw in "${FRAMEWORK_PKGS[@]}"; do
    case "$fw" in
      silvaengine_base) fwdir="$SILVAENGINE_BASE_DIR" ;;
      silvaengine_utility) fwdir="$SILVAENGINE_UTILITY_DIR" ;;
      silvaengine_connections) fwdir="$SILVAENGINE_CONNECTIONS_DIR" ;;
    esac
    if ! src=$(fw_src "$fwdir" "$fw"); then
      die "框架仓布局无法识别：$fwdir（期望 repo 根即包，或内含 $fw/ 包目录）"
    fi
    if ! stage_copy "$src" "$BUILD_CONTEXT/$fw"; then
      die "暂存框架 $fw 失败（源：$src）"
    fi
  done
  if ! stage_copy "$BANYAN_MODULES_DIR" "$BUILD_CONTEXT/modules"; then
    die "暂存 12 引擎目录失败（源：$BANYAN_MODULES_DIR）"
  fi
  if ! stage_copy "$VENDOR_DIR" "$BUILD_CONTEXT/vendor"; then
    die "暂存 vendor 快照失败（源：$VENDOR_DIR）"
  fi
  cp "$REQUIREMENTS" "$BUILD_CONTEXT/requirements.txt" || die "复制 requirements.txt 失败"

  # 暂存后核验：任一 tar 实现若未按预期排除垃圾，立即失败而非污染镜像层
  junk=$(find "$BUILD_CONTEXT" \( -name .git -o -name .venv -o -name venv \
    -o -name __pycache__ -o -name node_modules -o -name '*.pyc' \) \
    -print -quit 2>/dev/null || true)
  if [ -n "$junk" ]; then
    die "构建上下文含应排除内容：$junk —— 当前 tar 的排除模式不兼容，请报告环境（tar --version）"
  fi

  # 暂存权限归一化：容器以 uid 1000 运行，源码必须全员可读。
  # 宿主 umask 077（本脚本默认）会使 tar 解包产物为 600，COPY 会原样
  # 带进镜像 → 容器内 Permission denied（镜像内 chmod 兑底见 Dockerfile）。
  chmod -R a+rX "$BUILD_CONTEXT" || die "暂存目录权限归一化失败"
  log "暂存完成：网关包 + 3 框架（布局归一化）+ 12 引擎 + vendor 3 包 + requirements.txt"
}

# 上下文源码 digest：文件清单（排序）+ 内容 双重哈希。
# 构建幂等跳过的依据（写入镜像 label，重复执行比对）。
if command -v sha256sum >/dev/null 2>&1; then
  HASH_BIN="sha256sum"
else
  HASH_BIN="shasum -a 256"
fi

context_digest() {
  local n digest
  n=$(find "$BUILD_CONTEXT" -type f | wc -l | tr -d ' ')
  if [ -z "$n" ] || [ "$n" -eq 0 ]; then
    return 1
  fi
  digest=$(cd "$BUILD_CONTEXT" \
    && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 $HASH_BIN | $HASH_BIN) \
    || return 1
  printf '%s' "$digest" | awk '{print $1}'
}

# 完整 digest = 上下文内容 + Dockerfile 内容。只哈希上下文会把 Dockerfile
# 自身的变更吞进“digest 未变 → 跳过构建”，镜像永不重建（实测踩坑）。
combined_digest() {
  local ctx_d df_d
  ctx_d=$(context_digest) || return 1
  df_d=$($HASH_BIN "$DOCKERFILE" | awk '{print $1}') || return 1
  printf '%s %s\n' "$ctx_d" "$df_d" | $HASH_BIN | awk '{print $1}'
}

# ---------------------------------------------------------------------------
# 阶段 5：镜像构建（digest 相同则跳过）
# ---------------------------------------------------------------------------

build_image() {
  local digest="$1" existing
  if "$RUNTIME_BIN" image inspect "$GATEWAY_IMAGE" >/dev/null 2>&1; then
    existing=$(image_label "$GATEWAY_IMAGE" "$DIGEST_LABEL")
    if [ "$existing" = "$digest" ] && [ "$FORCE_BUILD" != "1" ]; then
      log "源码 digest 未变化（镜像 label 匹配），跳过构建：$GATEWAY_IMAGE"
      return 0
    fi
  fi
  if [ "$FORCE_BUILD" = "1" ]; then
    log "--force-build：强制重建镜像"
  fi
  log "构建镜像：$GATEWAY_IMAGE"
  log "  基础镜像：$PYTHON_IMAGE    pip 源：$PIP_INDEX_URL"
  if ! "$RUNTIME_BIN" build \
      -f "$DOCKERFILE" \
      --build-arg "PYTHON_IMAGE=$PYTHON_IMAGE" \
      --build-arg "PIP_INDEX_URL=$PIP_INDEX_URL" \
      --label "$DIGEST_LABEL=$digest" \
      -t "$GATEWAY_IMAGE" \
      "$BUILD_CONTEXT"; then
    die "镜像构建失败——常见原因：基础镜像拉取失败（可 export PYTHON_IMAGE=python:3.12-slim 换官方源）、pip 源不可达（可 export PIP_INDEX_URL=https://pypi.org/simple/）、内存不足（podman machine set --memory 4096 后重启 machine）"
  fi
  log "镜像构建完成：$GATEWAY_IMAGE"
}

# ---------------------------------------------------------------------------
# 阶段 9：健康与日志
# ---------------------------------------------------------------------------

wait_healthy() {
  local deadline now all_ok st c line
  deadline=$(( $(date +%s) + WAIT_TIMEOUT ))
  log "等待容器健康（最长 ${WAIT_TIMEOUT}s；首次运行含数据面镜像拉取）"
  while :; do
    now=$(date +%s)
    if [ "$now" -ge "$deadline" ]; then
      printf '%s\n' "── gateway 最近日志（$RUNTIME_BIN logs --tail 60）──" >&2
      "$RUNTIME_BIN" logs --tail 60 silvaengine-gateway 2>&1 || true
      die "健康检查超时（${WAIT_TIMEOUT}s）。可用 GATEWAY_WAIT_TIMEOUT 环境变量延长等待；机器内存不足时 podman 需 ≥4GiB（podman machine set --memory 4096 后 stop/start）"
    fi
    all_ok=1
    line=""
    for c in silvaengine-gateway-postgres silvaengine-gateway-neo4j \
      silvaengine-gateway-redis silvaengine-gateway; do
      st=$(health_state "$c")
      line="$line ${c#silvaengine-gateway-}=${st:-none}"
      if [ "$st" != "healthy" ]; then
        all_ok=0
      fi
    done
    st=$("$RUNTIME_BIN" inspect -f '{{.State.Running}}' silvaengine-gateway-ddb-local 2>/dev/null || true)
    line="$line ddb-local=${st:-none}"
    if [ "$st" != "true" ]; then
      all_ok=0
    fi
    if [ "$all_ok" = "1" ]; then
      log "全部容器健康：$line"
      return 0
    fi
    printf '  等待中（剩余 %ss）：%s\n' "$(( deadline - now ))" "$line"
    sleep 5
  done
}

verify_gateway() {
  local out
  if ! out=$(dc exec -T gateway python -c \
    "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=10); print('health=' + str(r.status))" 2>&1); then
    die "网关 /health 探活失败（容器内 urllib）"
  fi
  log "网关探活：$out"
}

# $1 = required（部署时缺失即失败）| info（status 时仅告警）
# 扫全量容器日志而非尾部窗口：启动日志可达数百行（含引擎迁移回溯），
# 固定 tail 窗口会把头部的两条启动标记挤出视野（实测踩坑）；
# "容器当前健康" 已由 wait_healthy 在前保证，全量扫描不会误放行崩溃循环。
check_log() {
  local logs miss=() pat
  logs=$("$RUNTIME_BIN" logs silvaengine-gateway 2>&1 || true)
  # herestring 而非管道：pipefail 下 grep -q 匹配即退会让上游 printf 吃 SIGPIPE
  # （141 → if! 判真 → 误报缺失）；启动日志 > 64KB 管道缓冲时必触发（实测踩坑）
  for pat in "${REQUIRED_LOGS[@]}"; do
    if ! grep -qF "$pat" <<< "$logs"; then
      miss+=("$pat")
    fi
  done
  if [ "${#miss[@]}" -gt 0 ]; then
    for pat in "${miss[@]}"; do
      printf '[deploy.sh] 缺失关键日志：%s\n' "$pat" >&2
    done
    if [ "$1" = "required" ]; then
      die "网关启动关键日志缺失——se-configdata 叠加或池引导未生效；完整日志：$RUNTIME_BIN logs silvaengine-gateway"
    fi
  fi
  for pat in "${OPTIONAL_LOGS[@]}"; do
    if ! grep -qE "$pat" <<< "$logs"; then
      printf '[deploy.sh] 提示：可选日志缺失（%s）——repo-root 布局未检出或池回环改写未发生，非预期时请检查源码路径与 BANYAN_LOOPBACK_BASE_URL\n' "$pat" >&2
    fi
  done
}

# ---------------------------------------------------------------------------
# 阶段 9：摘要
# ---------------------------------------------------------------------------

print_summary() {
  cat <<EOF

============================================================
 SilvaEngine Gateway（Banyan 数据面 · 生产镜像模式）部署完成
============================================================
  网关地址      : http://<服务器IP>:8000   （本机探活 curl http://127.0.0.1:8000/health）
  GraphQL 入口  : POST http://<服务器IP>:8000/beta/core/banyan/<engine>_engine_graphql
  租户 part_id  : ${TENANT_PART_ID}
  网关镜像      : ${GATEWAY_IMAGE}
                  （源码 digest 已写入镜像 label；未变更时重跑自动跳过构建）
  生成文件      : .env（数据面密码 + 超管凭据，600 权限，勿提交）
                  env/se-configdata.local.json（JWT/x-api-key，600 权限，勿提交）
  数据面        : postgres / neo4j / redis + DynamoDB Local（127.0.0.1:8001 仅本机）
  超级管理员    : $(env_get ADMIN_ACCOUNT)
                  （密码在 .env 的 ADMIN_PASSWORD——建议首次登录后立即修改；
                   阶段 10 已绑定 platform:super_admin，重跑只收敛不覆盖密码）
  平台资源      : 阶段 11 已注册并全量授权根角色（tenant_perm_resource 全集
                  PERMIT → platform:super_admin，含菜单/按钮可见性与 API 资源；
                  升级重跑自动收敛新资源）

验证步骤：
  curl -sS http://127.0.0.1:8000/health
  bash deploy.sh status
  # 登录 mutation 骨架（携带 part_id 头 + Banyan JWT）：
  # curl -sS -X POST http://127.0.0.1:8000/beta/core/banyan/user_engine_graphql \\
  #   -H 'content-type: application/json' -H 'part_id: ${TENANT_PART_ID}' \\
  #   -d '{"query": "mutation { ... }"}'

常用操作：
  bash deploy.sh status          # 状态/健康/关键日志
  bash deploy.sh down            # 停止（数据卷保留）
  bash deploy.sh down -v         # 停止并删除数据卷
  bash deploy.sh --force-build   # 源码变更后强制重建镜像
  bash deploy.sh --force-env     # 重新生成全部密码

安全提醒：公网防火墙只放行 8000；8001/5432/6379/7474/7687 仅供本机调试。
============================================================
EOF
}

# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

cmd_up() {
  set_stage 1 "环境检测（Docker / Compose v2 或 Podman / compose provider）" \
    "服务器：安装 Docker 与 docker-compose-plugin；本机 Podman：brew install docker-compose 且 podman machine start；权限：sudo usermod -aG docker \$USER 后重新登录"
  detect_runtime

  set_stage 2 "配置生成与源码树校验" \
    "源路径可用环境变量覆盖（bash deploy.sh -h 查看清单）；.env 改动后重跑即生效；--force-env 重新生成"
  ensure_config

  set_stage 3 "端口预检（${HOST_PORTS[*]}）" \
    "占用排查：lsof -i :<port>（macOS）/ ss -ltnp | grep :<port>（Linux）"
  check_ports

  set_stage 4 "暂存构建上下文（staging + 源码 digest）" \
    "暂存为全量重建，无陈旧残留；排除 .git/.venv 等失败时检查 tar 版本"
  stage_context
  DIGEST=$(combined_digest) \
    || die "源码 digest 计算失败（构建上下文为空或哈希工具异常）"

  if [ "$DRY_RUN" = "1" ]; then
    local existing
    existing=""
    if "$RUNTIME_BIN" image inspect "$GATEWAY_IMAGE" >/dev/null 2>&1; then
      existing=$(image_label "$GATEWAY_IMAGE" "$DIGEST_LABEL")
    fi
    log "源码 digest：$DIGEST"
    if [ "$existing" = "$DIGEST" ] && [ "$FORCE_BUILD" != "1" ]; then
      log "--dry-run 结论：将跳过镜像构建（digest 与既有镜像一致）"
    else
      log "--dry-run 结论：将构建镜像 $GATEWAY_IMAGE（digest 未命中既有镜像）"
    fi
    log "--dry-run 完成：环境/配置/端口/暂存全部通过，未构建镜像、未启动容器"
    return 0
  fi

  set_stage 5 "构建网关镜像（源码未变自动跳过）" \
    "基础镜像/pip 源可经 PYTHON_IMAGE / PIP_INDEX_URL 覆盖；构建慢属正常（pip 全量安装）"
  build_image "$DIGEST"

  set_stage 6 "启动 DynamoDB Local" \
    "镜像拉取失败检查网络；DDB_LOCAL_IMAGE 可换源（.env）"
  dc up -d ddb-local

  set_stage 7 "初始化 se-configdata（建表/灌种子/核验）" \
    "失败多为种子 JSON 渲染或容器网络问题；详情：docker compose logs ddb-init；重跑幂等"
  dc run --rm ddb-init

  set_stage 8 "启动网关与数据面（五服务）" \
    "镜像拉取失败检查网络/数据面镜像源（.env 中 *_IMAGE）；健康等待可用 GATEWAY_WAIT_TIMEOUT 延长"
  dc up -d "${UP_SERVICES[@]}"
  if [ "$RESTART" = "1" ]; then
    log "--restart：重启 gateway"
    dc restart gateway
  fi

  set_stage 9 "健康检查与启动验证" \
    "看报错：$RUNTIME_BIN logs silvaengine-gateway；首启较慢可用 GATEWAY_WAIT_TIMEOUT=900 重跑"
  wait_healthy
  verify_gateway
  check_log required

  set_stage 10 "初始化超级管理员（幂等收敛，密码永不覆盖）" \
    "失败多为 PG 连接或预设角色未就绪；详情：$RUNTIME_BIN compose logs admin-init；重跑幂等"
  dc run --rm admin-init

  set_stage 11 "资源注册与根角色授权收敛（幂等，重跑只差量补齐）" \
    "失败看 failed_items 与网关日志：$RUNTIME_BIN logs silvaengine-gateway；部分引擎导入失败重跑自动补齐；改密后重部署走 DB 终态核验降级"
  dc run --rm resource-init

  print_summary
}

cmd_status() {
  if [ ! -f "$ENV_FILE" ]; then
    die "未找到 .env——请先运行 bash deploy.sh 完成部署"
  fi
  set_stage S "状态检查" "docker compose ps -a 查看全部容器"
  detect_runtime
  dc ps
  local bad=0 c st
  for c in silvaengine-gateway-postgres silvaengine-gateway-neo4j \
    silvaengine-gateway-redis silvaengine-gateway; do
    st=$(health_state "$c")
    printf '  %-34s %s\n' "$c" "${st:-未运行}"
    if [ "$st" != "healthy" ]; then
      bad=1
    fi
  done
  st=$("$RUNTIME_BIN" inspect -f '{{.State.Running}}' silvaengine-gateway-ddb-local 2>/dev/null || true)
  printf '  %-34s %s\n' "silvaengine-gateway-ddb-local" "${st:-未运行}"
  if [ "$st" != "true" ]; then
    bad=1
  fi
  check_log info
  if [ "$bad" = "1" ]; then
    log "存在未健康/未运行的容器（上方状态）"
    exit 1
  fi
  log "全部容器健康"
}

cmd_down() {
  if [ ! -f "$ENV_FILE" ]; then
    die "未找到 .env——如需清理残留容器/卷，请在本目录手工执行 docker compose down（-v 连卷）"
  fi
  set_stage D "停止并清理" "compose down 失败时可先 docker ps 查看占用"
  detect_runtime
  if [ "$VOLUMES" = "1" ]; then
    dc down -v
    log "已停止全部容器并删除数据卷（postgres/neo4j/redis 数据已清空）"
  else
    dc down
    log "已停止全部容器（数据卷保留；彻底清理：bash deploy.sh down -v）"
  fi
}

# ---------------------------------------------------------------------------
# 自检（纯逻辑，不碰 docker/podman；生成/渲染在 mktemp 目录中进行）
# ---------------------------------------------------------------------------

cmd_self_test() {
  local tmp
  tmp=$(mktemp -d) || return 1
  local saved_env="$ENV_FILE" saved_seed="$SEED_JSON" saved_ctx="$BUILD_CONTEXT"
  local pass=0 fail=0

  expect_eq() {
    local desc="${1:-}" actual="${2:-}" expected="${3:-}"
    if [ "$actual" = "$expected" ]; then
      printf '  PASS: %s\n' "$desc"
      pass=$((pass + 1))
    else
      printf '  FAIL: %s —— 期望 [%s] 实际 [%s]\n' "$desc" "$expected" "$actual"
      fail=$((fail + 1))
    fi
  }

  ENV_FILE="$tmp/.env"
  SEED_JSON="$tmp/se-configdata.local.json"

  # --- 基础工具 ---
  local r1 r2
  r1=$(rand_alnum 16)
  r2=$(rand_alnum 16)
  expect_eq "rand_alnum 长度 16" "${#r1}" "16"
  expect_eq "rand_alnum 纯字母数字" \
    "$(printf '%s' "$r1" | grep -c '^[A-Za-z0-9]*$' || true)" "1"
  expect_eq "rand_alnum 两次不同" "$([ "$r1" != "$r2" ] && echo 1 || true)" "1"

  expect_eq "sed_escape 转义 \\ / &" "$(sed_escape 'a/b\c&d')" 'a\/b\\c\&d'

  local saved_env2="$ENV_FILE"
  ENV_FILE="$tmp/envget"
  printf 'K1="va lue"\nK2=plain\n' > "$ENV_FILE"
  expect_eq "env_get 剥双引号" "$(env_get K1)" "va lue"
  expect_eq "env_get 普通值" "$(env_get K2)" "plain"
  expect_eq "env_get 缺失为空" "$(env_get K3)" ""
  ENV_FILE="$saved_env2"

  # --- generate_env → .env 断言 ---
  generate_env
  local pw_check
  pw_check=$(env_get POSTGRES_PASSWORD)
  expect_eq "GATEWAY_IMAGE 默认值" "$(env_get GATEWAY_IMAGE)" "$GATEWAY_IMAGE_DEFAULT"
  expect_eq "SE_CONFIGDATA_ENDPOINT_URL 指向 ddb-local" \
    "$(env_get SE_CONFIGDATA_ENDPOINT_URL)" "http://ddb-local:8000"
  expect_eq "POSTGRES_PASSWORD 16 位" "${#pw_check}" "16"
  expect_eq "POSTGRES_PASSWORD 纯字母数字" \
    "$(printf '%s' "$pw_check" | grep -c '^[A-Za-z0-9]*$' || true)" "1"
  expect_eq "NEO4J_AUTH 形态 neo4j/<16>" \
    "$(env_get NEO4J_AUTH | grep -c '^neo4j/[A-Za-z0-9]\{16\}$' || true)" "1"
  expect_eq "AWS 假凭据已写入" "$(env_get aws_access_key_id)" "local"
  expect_eq "POSTGRES_IMAGE 默认 China 源" \
    "$(env_get POSTGRES_IMAGE)" "${POSTGRES_IMAGE:-docker.m.daocloud.io/library/postgres:16}"
  expect_eq "NEO4J_IMAGE 默认 China 源" \
    "$(env_get NEO4J_IMAGE)" "${NEO4J_IMAGE:-docker.m.daocloud.io/library/neo4j:5.26-ubi10}"
  expect_eq "REDIS_IMAGE 默认 China 源" \
    "$(env_get REDIS_IMAGE)" "${REDIS_IMAGE:-docker.m.daocloud.io/library/redis:7}"
  expect_eq "DDB_LOCAL_IMAGE 默认 China 源" \
    "$(env_get DDB_LOCAL_IMAGE)" "${DDB_LOCAL_IMAGE:-docker.m.daocloud.io/amazon/dynamodb-local:latest}"
  if [ -z "${TENANT_PART_ID:-}" ]; then
    expect_eq "TENANT_PART_ID 默认 nestaging" "$(env_get TENANT_PART_ID)" "nestaging"
  fi
  expect_eq ".env 权限 600" \
    "$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE" 2>/dev/null || true)" "600"

  # --- 超管键（阶段 10 admin-init）---
  expect_eq "ADMIN_ACCOUNT 默认值" "$(env_get ADMIN_ACCOUNT)" "admin@banyanos.dev"
  expect_eq "ADMIN_PASSWORD 默认值" "$(env_get ADMIN_PASSWORD)" "B@nyan0s.d3v"
  expect_eq "ADMIN_PASSWORD 含特殊字符（严禁 assert_alnum/sed 路径）" \
    "$(printf '%s' "$(env_get ADMIN_PASSWORD)" | grep -c '^[A-Za-z0-9]*$' || true)" "0"

  # --- load_env_values 回读 ---
  load_env_values
  expect_eq "load_env_values 回读 PG 密码一致" "$PG_PASSWORD" "$(env_get POSTGRES_PASSWORD)"
  expect_eq "load_env_values 回读 GATEWAY_IMAGE 一致" "$GATEWAY_IMAGE" "$GATEWAY_IMAGE_DEFAULT"
  expect_eq "NEO4J_PASSWORD 解析 16 位" "${#NEO4J_PASSWORD}" "16"

  # --- ensure_admin_env_keys 补全（存量 .env 场景）---
  local saved_admin_env="$ENV_FILE"
  ENV_FILE="$tmp/env_admin_missing"
  printf 'GATEWAY_IMAGE=x\nTENANT_PART_ID=y\n' > "$ENV_FILE"
  ensure_admin_env_keys
  expect_eq "补全：缺失时追加 ADMIN_ACCOUNT" "$(env_get ADMIN_ACCOUNT)" "admin@banyanos.dev"
  expect_eq "补全：缺失时追加 ADMIN_PASSWORD" "$(env_get ADMIN_PASSWORD)" "B@nyan0s.d3v"
  expect_eq "补全：追加后仍 600 权限" \
    "$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE" 2>/dev/null || true)" "600"
  ENV_FILE="$tmp/env_admin_custom"
  printf 'ADMIN_ACCOUNT=ops@example.io\n' > "$ENV_FILE"
  ensure_admin_env_keys
  expect_eq "补全：已有自定义 ADMIN_ACCOUNT 不追加重复行" \
    "$(grep -c '^ADMIN_ACCOUNT=' "$ENV_FILE" || true)" "1"
  expect_eq "补全：自定义 ADMIN_ACCOUNT 值保留" "$(env_get ADMIN_ACCOUNT)" "ops@example.io"
  expect_eq "补全：缺失 ADMIN_PASSWORD 单独追加" "$(env_get ADMIN_PASSWORD)" "B@nyan0s.d3v"
  ENV_FILE="$saved_admin_env"

  # --- render_seed_json（用仓内真实模板）---
  render_seed_json
  expect_eq "种子 JSON 无残留占位符" "$(grep -c '<[A-Z][A-Z0-9_]*>' "$SEED_JSON" || true)" "0"
  expect_eq "种子 JSON 无 _comment" "$(grep -c '_comment' "$SEED_JSON" || true)" "0"
  expect_eq "种子 JSON 注入 PG 库名（3 处池）" \
    "$(grep -c "\"database\": \"$(env_get POSTGRES_DB)\"" "$SEED_JSON" || true)" "3"
  expect_eq "种子 JSON 注入 PG 密码（3 处池）" \
    "$(grep -c "\"password\": \"$(env_get POSTGRES_PASSWORD)\"" "$SEED_JSON" || true)" "3"
  expect_eq "种子 JSON initialize_tables 保留" \
    "$(grep -c 'initialize_tables": true' "$SEED_JSON" || true)" "1"
  expect_eq "种子 JSON part_id 已注入" \
    "$(grep -c "\"part_id\": \"$(env_get TENANT_PART_ID)\"" "$SEED_JSON" || true)" "1"

  # --- validate_env_keys 通过 ---
  validate_env_keys
  expect_eq "validate_env_keys 生成 .env 全通过" "ok" "ok"

  # --- port_busy 空闲端口判定 ---
  local free_p="" p
  for p in 39201 39211 39221 39231 39241 39251 39261 39271 39281 39291; do
    if ! port_busy "$p"; then
      free_p="$p"
      break
    fi
  done
  if [ -n "$free_p" ]; then
    if port_busy "$free_p"; then
      expect_eq "port_busy 空闲端口($free_p)判定非忙" "busy" "free"
    else
      expect_eq "port_busy 空闲端口($free_p)判定非忙" "free" "free"
    fi
  else
    printf '  （跳过 port_busy 测试：未找到空闲端口）\n'
  fi

  # --- 框架双布局解析 ---
  local mk="$tmp/fake"
  mkdir -p "$mk/fw_root" "$mk/fw_inner/silvaengine_utility"
  touch "$mk/fw_root/__init__.py" "$mk/fw_inner/silvaengine_utility/__init__.py"
  expect_eq "fw_src repo 根即包布局" "$(fw_src "$mk/fw_root" silvaengine_base)" "$mk/fw_root"
  expect_eq "fw_src 内层包目录布局" \
    "$(fw_src "$mk/fw_inner" silvaengine_utility)" "$mk/fw_inner/silvaengine_utility"
  expect_eq "fw_src 无法识别返回非零" "$(fw_src "$mk" nosuch >/dev/null 2>&1 || echo bad)" "bad"

  # --- stage_copy 排除 + digest 确定性 ---
  local src1="$tmp/src1" dst1="$tmp/dst1" dst2="$tmp/dst2"
  mkdir -p "$src1/pkg/__pycache__" "$src1/.git" "$src1/pkg/.venv"
  touch "$src1/pkg/__init__.py" "$src1/pkg/__pycache__/j.pyc" "$src1/pkg/junk.pyc" \
    "$src1/.git/config" "$src1/pkg/.venv/x" "$src1/x.gitignore"
  if stage_copy "$src1" "$dst1"; then
    expect_eq "stage_copy 拷贝包文件" "$([ -f "$dst1/pkg/__init__.py" ] && echo 1 || true)" "1"
    expect_eq "stage_copy 保留 .gitignore" "$([ -f "$dst1/x.gitignore" ] && echo 1 || true)" "1"
    expect_eq "stage_copy 排除 .git" "$([ -e "$dst1/.git" ] && echo bad || echo ok)" "ok"
    expect_eq "stage_copy 排除 .venv" "$([ -e "$dst1/pkg/.venv" ] && echo bad || echo ok)" "ok"
    expect_eq "stage_copy 排除 __pycache__" "$([ -e "$dst1/pkg/__pycache__" ] && echo bad || echo ok)" "ok"
    expect_eq "stage_copy 排除 *.pyc" "$([ -e "$dst1/pkg/junk.pyc" ] && echo bad || echo ok)" "ok"
  else
    expect_eq "stage_copy 执行成功" "failed" "ok"
  fi
  if stage_copy "$src1" "$dst2"; then
    local d1 d2
    BUILD_CONTEXT="$dst1"
    d1=$(context_digest)
    BUILD_CONTEXT="$dst2"
    d2=$(context_digest)
    expect_eq "context_digest 确定性（两次暂存一致）" "$d1" "$d2"
    BUILD_CONTEXT="$dst1"
    expect_eq "context_digest 内容敏感（变更后不同）" \
      "$([ "$(printf x >> "$dst1/pkg/__init__.py"; context_digest)" != "$d1" ] && echo 1 || true)" "1"
    expect_eq "context_digest 形态 64 位十六进制" \
      "$(printf '%s' "$d1" | grep -c '^[0-9a-f]\{64\}$' || true)" "1"
    mkdir -p "$tmp/empty_ctx"
    BUILD_CONTEXT="$tmp/empty_ctx"
    expect_eq "context_digest 空目录返回非零" "$(context_digest >/dev/null 2>&1 || echo bad)" "bad"

    # combined_digest 对 Dockerfile 内容敏感
    local saved_dockerfile="$DOCKERFILE" cd1 cd2
    DOCKERFILE="$tmp/fake.Dockerfile"
    printf 'FROM scratch\n' > "$DOCKERFILE"
    BUILD_CONTEXT="$dst1"
    cd1=$(combined_digest)
    printf 'FROM scratch\nRUN chmod -R a+rX /app\n' > "$DOCKERFILE"
    cd2=$(combined_digest)
    expect_eq "combined_digest 对 Dockerfile 内容敏感（变更后不同）" \
      "$([ "${cd1:-x}" != "${cd2:-y}" ] && echo 1 || true)" "1"
    DOCKERFILE="$saved_dockerfile"
  else
    expect_eq "stage_copy 第二次执行成功" "failed" "ok"
  fi
  BUILD_CONTEXT="$saved_ctx"

  # --- 真实源码树全量暂存（源码树存在时执行；等价于 dry-run 阶段 4）---
  resolve_source_paths
  if [ -n "$GATEWAY_PACKAGE_DIR" ] && [ -f "$GATEWAY_PACKAGE_DIR/app.py" ] \
    && [ -n "$BANYAN_MODULES_DIR" ] && [ -d "$BANYAN_MODULES_DIR/agent_engine" ] \
    && [ -n "$VENDOR_DIR" ] && [ -f "$VENDOR_DIR/silvaengine_constants/__init__.py" ]; then
    BUILD_CONTEXT="$tmp/realctx"
    stage_context
    expect_eq "真实树暂存：网关包 app.py" \
      "$([ -f "$BUILD_CONTEXT/silvaengine_gateway/app.py" ] && echo 1 || true)" "1"
    expect_eq "真实树暂存：三框架包根 __init__.py（归一化）" \
      "$([ -f "$BUILD_CONTEXT/silvaengine_base/__init__.py" ] && echo 1 || true)" "1"
    expect_eq "真实树暂存：引擎双层包 __init__.py" \
      "$([ -f "$BUILD_CONTEXT/modules/agent_engine/agent_engine/__init__.py" ] && echo 1 || true)" "1"
    expect_eq "真实树暂存：vendor 快照包 __init__.py" \
      "$([ -f "$BUILD_CONTEXT/vendor/silvaengine_constants/__init__.py" ] && echo 1 || true)" "1"
    expect_eq "真实树暂存：requirements.txt" \
      "$([ -f "$BUILD_CONTEXT/requirements.txt" ] && echo 1 || true)" "1"
    local rd1 rd2
    rd1=$(context_digest)
    stage_context
    rd2=$(context_digest)
    expect_eq "真实树暂存：digest 确定性" "$rd1" "$rd2"
    BUILD_CONTEXT="$saved_ctx"
  else
    printf '  （跳过真实树暂存测试：源码树未按默认布局检出，可用环境变量指定源路径）\n'
  fi

  ENV_FILE="$saved_env"
  SEED_JSON="$saved_seed"
  rm -rf "$tmp"

  if [ "$fail" -gt 0 ]; then
    printf 'self-test FAILED（%s/%s 通过）\n' "$pass" "$((pass + fail))"
    return 1
  fi
  printf 'self-test OK（%s/%s 全部通过）\n' "$pass" "$pass"
  return 0
}

# ---------------------------------------------------------------------------
# 参数解析与分发
# ---------------------------------------------------------------------------

for arg in "$@"; do
  case "$arg" in
    up|deploy) MODE="up" ;;
    status) MODE="status" ;;
    down) MODE="down" ;;
    --volumes|-v) VOLUMES=1 ;;
    --restart) RESTART=1 ;;
    --force-build) FORCE_BUILD=1 ;;
    --force-env) FORCE_ENV=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --self-test) MODE="selftest" ;;
    -h|--help) usage; exit 0 ;;
    *)
      printf '未知参数：%s\n' "$arg" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [ "$MODE" != "down" ] && [ "$VOLUMES" = "1" ]; then
  printf '错误：-v/--volumes 仅可与 down 组合\n' >&2
  exit 1
fi

log "SilvaEngine Gateway 一键部署（生产镜像模式 · Banyan 数据面）"

case "$MODE" in
  selftest)
    if cmd_self_test; then exit 0; else exit 1; fi
    ;;
  status) cmd_status ;;
  down) cmd_down ;;
  up) cmd_up ;;
  *)
    printf '内部错误：未知模式 %s\n' "$MODE" >&2
    exit 1
    ;;
esac