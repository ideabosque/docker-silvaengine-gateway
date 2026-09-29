#!/usr/bin/env bash
# =============================================================================
# SilvaEngine Gateway — Banyan 日常更新脚本（clone/pull 模块 + 自动重启相关容器）
# =============================================================================
# 定位：既有部署（bash deploy.sh 已完成）的代码迭代更新，单命令完成
#   「模块源码 clone/pull → 按需重建镜像 → 自动重启相关容器 → 幂等收敛」。
# 首次部署请运行 bash deploy.sh（本脚本检测无 .env 时会拒绝执行）。
#
# 三步编排（零逻辑重复——重活全部委托 deploy.sh 权威流水线）：
#   U1 前置检查    deploy.sh 就位 / .env 存在（首次部署完成标志）
#   U2 交付仓更新  docker-silvaengine-gateway 自身 fetch + ff（deploy.sh
#                  阶段 2 的 16 仓清单不含交付仓自身，README 明示「自身更新
#                  手工 git pull」——本脚本补上这一环）；deploy.sh 自身有
#                  变更时提示（后续委托自动使用新版执行）
#   U3 委托流水线  bash deploy.sh up：阶段 2 clone/pull 16 仓模块 → 阶段 5/6
#                  暂存 + 源码 digest（未变自动跳过构建）→ 阶段 9 up -d
#                  （镜像有变自动重建 gateway 容器）→ 阶段 10 健康验证 →
#                  阶段 11/12 超管与资源授权幂等差量收敛
#
# 「自动重启相关容器」语义（生产镜像模式：源码打进镜像，非宿主挂载）：
#   - 网关/引擎/框架代码变更 → 镜像重建 → gateway 容器由 compose up -d 自动
#     重建（相关容器）；数据面容器（postgres/neo4j/redis/ddb-local）与代码
#     无关，不受扰动，持续运行。
#   - 源码未变 → 镜像与容器均保持原样（避免无意义重启）；--restart 透传
#     deploy.sh 强制重启 gateway（改种子 JSON 后使用）。
#   - 严禁「只 restart 容器不重建镜像」——容器内源码不会因 restart 变新，
#     本脚本不走该捷径（防旧镜像 + 新代码假象）。
#
# 幂等与安全：
#   - 可重复执行；脏工作区 fail-closed（交付仓与 16 仓同一纪律，防覆盖手工
#     修改）；委托失败打印 deploy.sh 退出码与排查建议后安全退出（deploy.sh
#     各阶段自身幂等，不遗留损坏中间状态）。
#   - --dry-run 透传：拉代码 + 暂存 + digest，不构建不起容器（安全演练）。
#
# 依赖：bash 3.2+ 与 git（U2 需要；宿主无 git 时可用 --skip-self-pull 跳过
#   交付仓更新，模块仓由 deploy.sh 阶段 2 处理）。无需 python/jq/rsync。
# =============================================================================
set -Eeuo pipefail
umask 077
export LC_ALL=C

DEPLOY_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DEPLOY_DIR"
REPO_ROOT=$(cd "$DEPLOY_DIR/.." && pwd)
ENV_FILE="$DEPLOY_DIR/.env"
DEPLOY_SH="$DEPLOY_DIR/deploy.sh"
SELF_SH="$DEPLOY_DIR/update.sh"

if command -v sha256sum >/dev/null 2>&1; then
  HASH_BIN="sha256sum"
else
  HASH_BIN="shasum -a 256"
fi

# 与 deploy.sh 阶段 2 同一非交互化纪律：禁交互凭据提示 + SSH TOFU/BatchMode
export GIT_TERMINAL_PROMPT=0
if [ -z "${GIT_SSH_COMMAND:-}" ]; then
  export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
fi

PASSTHROUGH=()
SKIP_SELF_PULL=0
MODE="update"

CURRENT_STAGE="U0"
STAGE_DESC="初始化"

log() { printf '[update.sh] %s\n' "$*"; }

set_stage() {
  CURRENT_STAGE="$1"
  STAGE_DESC="$2"
  log "── 阶段 $1/3：$2"
}

die() {
  printf '\n[update.sh] X 阶段 %s（%s）失败：%s\n' \
    "$CURRENT_STAGE" "$STAGE_DESC" "$*" >&2
  printf '[update.sh] 本脚本幂等——修复后可直接重跑；首次部署请运行 bash deploy.sh\n' >&2
  exit 1
}

on_error() {
  local code=$?
  printf '\n[update.sh] X 阶段 %s（%s）内部异常（退出码 %s，行 %s）。\n' \
    "$CURRENT_STAGE" "$STAGE_DESC" "$code" "${BASH_LINENO[0]:-?}" >&2
  printf '[update.sh] 本脚本幂等——修复后可直接重跑\n' >&2
  exit "$code"
}
trap on_error ERR

usage() {
  cat <<'USAGE'
SilvaEngine Gateway — Banyan 日常更新（clone/pull 16 仓模块 + 自动重启相关容器）

用法: bash update.sh [选项]

定位：
  仅用于既有部署的代码迭代更新（首次部署请运行 bash deploy.sh）。
  U1 前置检查 → U2 交付仓自身 git pull → U3 委托 deploy.sh 全流水线
  （模块 clone/pull → digest 未变跳过构建 → 镜像有变自动重建 gateway
  容器 → 健康验证 → 超管/资源幂等收敛）。

选项:
  --restart          透传 deploy.sh：更新后强制重启 gateway（改种子 JSON 后使用）
  --force-build      透传 deploy.sh：强制重建镜像（默认源码未变自动跳过）
  --dry-run          透传 deploy.sh：拉代码 + 暂存 + digest，不构建不起容器
  --skip-self-pull   跳过 U2 交付仓自身更新（本地开发工作区有未提交修改时）
  --self-test        内置纯逻辑自检（临时目录夹具，不碰 docker/podman）
  -h, --help         显示本帮助

环境变量:
  DELIVERY_BRANCH    U2 交付仓目标分支（默认：交付仓当前所在分支）
  其余环境变量（GATEWAY_BRANCH / ENGINE_BRANCH / *_BRANCH / *_DIR 路径
  覆盖 / GATEWAY_PORT 等）由 deploy.sh 透传生效，详见 bash deploy.sh -h
USAGE
}

parse_args() {
  local arg
  for arg in "$@"; do
    case "$arg" in
      --restart|--force-build|--dry-run)
        PASSTHROUGH+=("$arg") ;;
      --skip-self-pull) SKIP_SELF_PULL=1 ;;
      --self-test) MODE="selftest" ;;
      -h|--help) usage; exit 0 ;;
      *)
        printf '未知参数：%s\n' "$arg" >&2
        usage >&2
        exit 1 ;;
    esac
  done
}

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

repo_clean() {
  local out
  out=$(git -C "$1" status --porcelain 2>/dev/null) || return 1
  [ -z "$out" ]
}

current_branch() {
  git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null || true
}

# U2：交付仓自身 fetch + ff-only（脏仓 fail-closed；非 git 布局跳过）
delivery_pull() {
  local root="$1" branch tgt_branch
  if [ ! -e "$root/.git" ]; then
    log "  - 跳过交付仓自身更新（非 git 布局——按 rsync 手工布局处理）"
    return 0
  fi
  if ! command -v git >/dev/null 2>&1; then
    die "交付仓为 git 布局但宿主无 git——请安装 git（apt/yum install git）后重跑，或追加 --skip-self-pull 跳过自身更新"
  fi
  if ! repo_clean "$root"; then
    die "交付仓工作区不干净（存在未提交改动或未跟踪文件）——为避免覆盖手工修改，脚本拒绝自动更新。请先在 $root 提交或 stash，或追加 --skip-self-pull"
  fi
  branch=$(current_branch "$root")
  if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
    die "交付仓处于 detached HEAD 或无法识别当前分支——请手工检查 $root（或用 DELIVERY_BRANCH 指定分支）后重跑，或追加 --skip-self-pull"
  fi
  if [ -n "${DELIVERY_BRANCH:-}" ]; then
    tgt_branch="$DELIVERY_BRANCH"
  else
    tgt_branch="$branch"
  fi
  log "  + update 交付仓 docker-silvaengine-gateway（分支 $tgt_branch）"
  git -C "$root" fetch origin "$tgt_branch" \
    || die "git fetch 失败：交付仓分支 $tgt_branch——检查网络与远端可达性（远端名非 origin 时 git remote -v 核对）"
  git -C "$root" checkout "$tgt_branch" \
    || die "git checkout 失败：交付仓分支 $tgt_branch——远端无此分支时用 DELIVERY_BRANCH 指定"
  git -C "$root" merge --ff-only "origin/$tgt_branch" \
    || die "git merge --ff-only 失败：交付仓（本地与远端分叉）——请手工处理 $root 后重跑"
  if [ "$branch" != "$tgt_branch" ]; then
    log "  （已切换交付仓分支：$branch → $tgt_branch）"
  fi
}

cmd_update() {
  local before after rc gw_port

  set_stage U1 "前置检查（deploy.sh 就位 / .env 存在）"
  [ -f "$DEPLOY_SH" ] \
    || die "未找到 deploy.sh（$DEPLOY_SH）——update.sh 须与 deploy.sh 同目录"
  [ -f "$ENV_FILE" ] \
    || die "未找到 .env——update.sh 仅用于既有部署的日常更新；首次部署请先运行 bash deploy.sh"

  set_stage U2 "更新交付仓自身（docker-silvaengine-gateway）"
  if [ "$SKIP_SELF_PULL" = "1" ]; then
    log "--skip-self-pull：跳过交付仓自身更新"
  else
    before=$($HASH_BIN "$DEPLOY_SH" | awk '{print $1}')
    delivery_pull "$REPO_ROOT"
    after=$($HASH_BIN "$DEPLOY_SH" | awk '{print $1}')
    if [ "$before" != "$after" ]; then
      log "deploy.sh 自身已更新（U2 拉取到新版本），后续委托将使用新版执行"
    fi
  fi

  set_stage U3 "模块更新与容器收敛（委托 deploy.sh）"
  log "委托执行：bash deploy.sh up ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"
  rc=0
  bash "$DEPLOY_SH" up ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} || rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '\n[update.sh] X 阶段 U3（模块更新与容器收敛）失败：deploy.sh 退出码 %s\n' "$rc" >&2
    printf '[update.sh] 排查建议：按上方 deploy.sh 各阶段输出与提示处理（阶段语义见 bash deploy.sh -h）；本脚本幂等——修复后可直接重跑\n' >&2
    exit "$rc"
  fi

  gw_port=$(env_get GATEWAY_PORT)
  printf '\n'
  log "============================================================"
  log " Banyan 日常更新完成"
  log "============================================================"
  log "  交付仓       : docker-silvaengine-gateway（U2 fetch + ff；--skip-self-pull 时跳过）"
  log "  模块仓       : 16 仓 clone/pull（引擎 12 仓 SSH + ideabosque 4 仓 https）"
  log "  网关镜像     : 源码 digest 变化时已重建（未变自动跳过构建）"
  log "  gateway 容器 : 镜像有变 → compose up -d 已自动重建；未变 → 保持运行"
  log "  数据面容器   : postgres / neo4j / redis / ddb-local 不受代码更新扰动"
  log "  幂等收敛     : 超管（admin-init）与平台资源授权（resource-init）已差量补齐"
  printf '\n'
  log "验证：curl -sS http://127.0.0.1:${gw_port:-8080}/health；bash deploy.sh status"
  log "强制重启 gateway（如改种子 JSON）：bash update.sh --restart"
}

# ---------------------------------------------------------------------------
# 自检（纯逻辑，不碰 docker/podman；夹具在临时目录，git 用本地 bare 仓）
# ---------------------------------------------------------------------------
cmd_self_test() {
  local tmp pass=0 fail=0 out rc
  tmp=$(mktemp -d) || return 1

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

  printf '── 单元：usage / parse_args / git 工具函数\n'

  # --- usage 渲染 ---
  local u
  u=$(usage)
  expect_eq "usage 含 --restart 透传说明" \
    "$(printf '%s' "$u" | grep -c -- '--restart' || true)" "1"
  expect_eq "usage 含 --skip-self-pull" \
    "$(printf '%s' "$u" | grep -c -- '--skip-self-pull' || true)" "1"
  expect_eq "usage 含 DELIVERY_BRANCH 环境变量" \
    "$(printf '%s' "$u" | grep -c 'DELIVERY_BRANCH' || true)" "1"

  # --- parse_args（子 shell 内执行，不污染全局） ---
  expect_eq "parse_args 透传三 flag" \
    "$(PASSTHROUGH=(); parse_args --restart --force-build --dry-run; \
       printf '%s' "${PASSTHROUGH[*]+"${PASSTHROUGH[*]}"}")" \
    "--restart --force-build --dry-run"
  expect_eq "parse_args 无参不透传" \
    "$(PASSTHROUGH=(); parse_args; printf '%s' "${PASSTHROUGH[*]+"${PASSTHROUGH[*]}"}")" \
    ""
  expect_eq "parse_args --skip-self-pull 置位" \
    "$(PASSTHROUGH=(); SKIP_SELF_PULL=0; parse_args --skip-self-pull; \
       printf '%s' "$SKIP_SELF_PULL")" "1"
  expect_eq "parse_args 未知参数退出码 1" \
    "$(PASSTHROUGH=(); ( parse_args --bogus 2>/dev/null && echo 0 ) || echo 1)" "1"

  # --- git 工具函数（真实 git 夹具） ---
  if command -v git >/dev/null 2>&1; then
    local gt="$tmp/gitrepo"
    git init -q "$gt" 2>/dev/null
    git -C "$gt" config user.email t@t.local
    git -C "$gt" config user.name t
    touch "$gt/f"
    git -C "$gt" add f
    git -C "$gt" commit -qm init
    expect_eq "repo_clean 干净仓通过" "$(repo_clean "$gt" && echo 1 || true)" "1"
    touch "$gt/untracked"
    expect_eq "repo_clean 脏仓（未跟踪文件）非零" \
      "$(repo_clean "$gt" >/dev/null 2>&1 && echo 1 || true)" ""
    rm -f "$gt/untracked"
    expect_eq "current_branch 返回分支名（master/main 皆可）" \
      "$(case $(current_branch "$gt") in master|main) echo ok ;; *) echo bad ;; esac)" \
      "ok"
    git -C "$gt" checkout -q --detach
    expect_eq "current_branch detached HEAD 返回 HEAD" "$(current_branch "$gt")" "HEAD"
  else
    printf '  （跳过 git 夹具测试：宿主无 git）\n'
  fi

  # --- 集成：夹具子进程（临时 bare origin + clone 交付仓 + stub deploy.sh） ---
  if command -v git >/dev/null 2>&1; then
    printf '\n── 集成：夹具子进程（bare origin + 交付仓 clone + stub deploy.sh）\n'

    local fx="$tmp/fix" stub_args="$tmp/stub_args"

    mk_delivery() {
      rm -rf "$fx"
      git init -q --bare "$fx/origin.git"
      git clone -q "$fx/origin.git" "$fx/delivery" 2>/dev/null
      git -C "$fx/delivery" config user.email t@t.local
      git -C "$fx/delivery" config user.name t
      mkdir -p "$fx/delivery/banyan"
      cp "$SELF_SH" "$fx/delivery/banyan/update.sh"
      cat > "$fx/delivery/banyan/deploy.sh" <<'STUB'
#!/usr/bin/env bash
if [ -n "${STUB_ARGS_FILE:-}" ]; then
  printf '%s\n' "$@" > "$STUB_ARGS_FILE"
fi
echo "[stub-deploy] invoked with: $*"
exit 0
STUB
      touch "$fx/delivery/banyan/.env"
      git -C "$fx/delivery" checkout -q -b main 2>/dev/null || true
      git -C "$fx/delivery" add -A
      git -C "$fx/delivery" commit -qm init
      git -C "$fx/delivery" push -q -u origin main 2>/dev/null
      rm -f "$stub_args"
    }

    run_fixture() {
      STUB_ARGS_FILE="$stub_args" bash "$fx/delivery/banyan/update.sh" "$@" 2>&1
    }

    # T1 无 .env → 拒绝执行且不委托
    mk_delivery
    rm -f "$fx/delivery/banyan/.env"
    rc=0; out=$(run_fixture --skip-self-pull) || rc=$?
    expect_eq "T1 无 .env 拒绝（退出码 1）" "$rc" "1"
    expect_eq "T1 提示首次部署走 deploy.sh" \
      "$(printf '%s' "$out" | grep -c '首次部署请先运行 bash deploy.sh' || true)" "1"
    expect_eq "T1 stub 未被调用" "$([ -f "$stub_args" ] && echo bad || echo ok)" "ok"
    git -C "$fx/delivery" checkout -q -- banyan/.env

    # T2 缺 deploy.sh → 拒绝
    rm -f "$fx/delivery/banyan/deploy.sh"
    rc=0; out=$(run_fixture) || rc=$?
    expect_eq "T2 缺 deploy.sh 拒绝（退出码 1）" "$rc" "1"
    expect_eq "T2 提示须与 deploy.sh 同目录" \
      "$(printf '%s' "$out" | grep -c 'update.sh 须与 deploy.sh 同目录' || true)" "1"
    expect_eq "T2 stub 未被调用" "$([ -f "$stub_args" ] && echo bad || echo ok)" "ok"
    git -C "$fx/delivery" checkout -q -- banyan/deploy.sh

    # T3 脏交付仓 → fail-closed 且不委托
    echo dirty > "$fx/delivery/banyan/local.txt"
    rc=0; out=$(run_fixture --dry-run) || rc=$?
    expect_eq "T3 脏仓 fail-closed（退出码 1）" "$rc" "1"
    expect_eq "T3 提示工作区不干净" \
      "$(printf '%s' "$out" | grep -c '工作区不干净' || true)" "1"
    expect_eq "T3 stub 未被调用" "$([ -f "$stub_args" ] && echo bad || echo ok)" "ok"
    rm -f "$fx/delivery/banyan/local.txt"

    # T4 干净仓 + origin 新提交 → ff + 委托（up 首参）+ deploy.sh 更新提示
    git clone -q "$fx/origin.git" "$tmp/originwork" 2>/dev/null
    git -C "$tmp/originwork" config user.email t@t.local
    git -C "$tmp/originwork" config user.name t
    printf '# stub v2\n' >> "$tmp/originwork/banyan/deploy.sh"
    git -C "$tmp/originwork" add -A
    git -C "$tmp/originwork" commit -qm 'update stub'
    git -C "$tmp/originwork" push -q origin main 2>/dev/null
    rc=0; out=$(run_fixture) || rc=$?
    expect_eq "T4 干净仓更新成功（退出码 0）" "$rc" "0"
    expect_eq "T4 委托 deploy.sh up" \
      "$(printf '%s' "$out" | grep -c '\[stub-deploy\] invoked with: up' || true)" "1"
    expect_eq "T4 deploy.sh 变更提示" \
      "$(printf '%s' "$out" | grep -c 'deploy.sh 自身已更新' || true)" "1"
    expect_eq "T4 ff 后 HEAD 与 origin/main 一致" \
      "$(git -C "$fx/delivery" rev-parse HEAD)" \
      "$(git -C "$fx/delivery" rev-parse origin/main)"
    expect_eq "T4 stub 首参 up" "$(head -1 "$stub_args")" "up"

    # T5 flag 透传顺序（up 前置 + 三 flag 依序）
    rc=0; out=$(run_fixture --restart --force-build --dry-run) || rc=$?
    expect_eq "T5 透传成功（退出码 0）" "$rc" "0"
    expect_eq "T5 stub 参数逐行一致" "$(cat "$stub_args")" "up
--restart
--force-build
--dry-run"

    # T6 幂等重跑（无新提交）
    rc=0; out=$(run_fixture) || rc=$?
    expect_eq "T6 幂等重跑（退出码 0）" "$rc" "0"
    expect_eq "T6 重跑无 deploy.sh 变更提示" \
      "$(printf '%s' "$out" | grep -c '自身已更新' || true)" "0"
    expect_eq "T6 stub 再次被调用（幂等）" "$([ -f "$stub_args" ] && echo 1 || true)" "1"

    # T7 --skip-self-pull + 脏仓 → 跳过自身更新仍可委托
    echo dirty > "$fx/delivery/banyan/local.txt"
    rc=0; out=$(run_fixture --skip-self-pull) || rc=$?
    expect_eq "T7 脏仓 + --skip-self-pull 成功（退出码 0）" "$rc" "0"
    expect_eq "T7 输出跳过提示" \
      "$(printf '%s' "$out" | grep -c -- '--skip-self-pull：跳过交付仓自身更新' || true)" "1"
    expect_eq "T7 stub 被调用" "$([ -f "$stub_args" ] && echo 1 || true)" "1"
    rm -f "$fx/delivery/banyan/local.txt"

    # T8 非 git REPO_ROOT（rsync 布局）→ 提示跳过 + 委托；.env GATEWAY_PORT 渲染
    local fx2="$tmp/fix2"
    mkdir -p "$fx2/delivery/banyan"
    cp "$SELF_SH" "$fx2/delivery/banyan/update.sh"
    cp "$fx/delivery/banyan/deploy.sh" "$fx2/delivery/banyan/deploy.sh"
    printf 'GATEWAY_PORT=9090\n' > "$fx2/delivery/banyan/.env"
    rm -f "$stub_args"
    rc=0; out=$(STUB_ARGS_FILE="$stub_args" \
      bash "$fx2/delivery/banyan/update.sh") || rc=$?
    expect_eq "T8 非 git 布局成功（退出码 0）" "$rc" "0"
    expect_eq "T8 提示非 git 布局跳过" \
      "$(printf '%s' "$out" | grep -c '非 git 布局' || true)" "1"
    expect_eq "T8 coda 渲染 .env GATEWAY_PORT" \
      "$(printf '%s' "$out" | grep -c 'http://127.0.0.1:9090/health' || true)" "1"

    # T9 DELIVERY_BRANCH 覆盖（origin 建 release 分支 → 切换 + ff）
    git -C "$tmp/originwork" checkout -q -b release
    printf '# release\n' >> "$tmp/originwork/banyan/deploy.sh"
    git -C "$tmp/originwork" add -A
    git -C "$tmp/originwork" commit -qm release
    git -C "$tmp/originwork" push -q -u origin release 2>/dev/null
    rc=0; out=$(DELIVERY_BRANCH=release run_fixture) || rc=$?
    expect_eq "T9 DELIVERY_BRANCH 覆盖成功（退出码 0）" "$rc" "0"
    expect_eq "T9 切换分支提示（main → release）" \
      "$(printf '%s' "$out" | grep -c '已切换交付仓分支：main → release' || true)" "1"
    expect_eq "T9 交付仓落位 release 分支" "$(current_branch "$fx/delivery")" "release"
  else
    printf '  （跳过集成夹具测试：宿主无 git）\n'
  fi

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
parse_args "$@"

log "SilvaEngine Gateway — Banyan 日常更新（clone/pull 模块 + 自动重启相关容器）"

case "$MODE" in
  selftest)
    if cmd_self_test; then exit 0; else exit 1; fi
    ;;
  update) cmd_update ;;
esac