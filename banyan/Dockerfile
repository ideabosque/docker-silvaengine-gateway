# =============================================================================
# SilvaEngine Gateway — Banyan 生产镜像（P1-A）
#
# 构建方式：deploy.sh 把各仓源码 staging 到 .build-context/（框架双布局
# 已归一化为包目录形态，排除 .git/.venv/__pycache__ 等）后执行
#   docker/podman build -f Dockerfile .build-context
#
# 与老仓根 Dockerfile（KGE/RFQ/MCP 网关）的刻意差异（取舍理由）：
#   - 引擎/框架/网关源码 COPY 进镜像而非 git+ssh pip 安装 —— 无需在服务器
#     配置 SSH deploy key，镜像自包含（与 bind-mount 变体同一 PYTHONPATH
#     布局，验收结论互相迁移）；
#   - uvicorn 直接作 PID 1（compose 级 healthcheck）而非 supervisord ——
#     单进程即全部职责，SIGTERM 优雅退出天然成立；
#   - pip + PIP_INDEX_URL 构建参数（默认阿里云源）而非 uv —— 少一个
#     astral.sh 网络依赖，国内构建链路可控。
#
# PYTHONPATH 布局与 api-runtime / bind-mount 变体逐字一致（已验证形态）：
#   /app/vendor                    → silvaengine_constants / silvaengine_definitions /
#                                   silvaengine_dynamodb_base 快照包
#   /app                           → silvaengine_gateway 包 + /app/silvaengine_<框架>
#                                   （staging 已归一化为包目录布局，直接命中）
#   /app/modules/<repo>            → 12 引擎仓根（双层 modules/<repo>/<repo>/，
#                                   Invoker.resolve_proxied_callable 普通 import）
# 缺失任一路径条目 → 对应引擎 import 失败 → 鉴权层 Permission denied →
# 全量 403（api-runtime 踩坑在案，务必全量列入）。
# =============================================================================
ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim

# --- 阶段 1：builder —— 只装依赖，不进运行时镜像 -----------------------------
FROM ${PYTHON_IMAGE} AS builder
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check \
        --index-url "${PIP_INDEX_URL}" -r requirements.txt

# --- 阶段 2：runtime —— venv + 源码，非 root ---------------------------------
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="silvaengine-gateway-banyan" \
      org.opencontainers.image.description="Banyan data plane: FastAPI gateway hosting 12 engines in-process"

COPY --from=builder /opt/venv /opt/venv

# staging 归一化后的源码树（见文件头注释；缺一即引擎 import 失败）
COPY vendor/ /app/vendor/
COPY silvaengine_gateway/ /app/silvaengine_gateway/
COPY silvaengine_base/ /app/silvaengine_base/
COPY silvaengine_utility/ /app/silvaengine_utility/
COPY silvaengine_connections/ /app/silvaengine_connections/
COPY modules/ /app/modules/

# 源码权限归一化：COPY 会原样保留 context 内文件模式，宿主 umask 077 会
# 造出 600 文件 → 容器内 uid 1000 读不了源码直接崩溃（实测踩坑）。
# 镜像内归一化优先于脚本级 chmod——任何来源/任何 umask 的 context 都被兜住。
RUN chmod -R a+rX /app /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app/vendor:/app:/app/modules/agent_engine:/app/modules/capability_engine:/app/modules/knowledge_engine:/app/modules/llm_engine:/app/modules/memory_engine:/app/modules/merchant_engine:/app/modules/monitor_engine:/app/modules/orchestration_engine:/app/modules/perm_engine:/app/modules/prompt_engine:/app/modules/setting_engine:/app/modules/user_engine"

# 非 root 运行（uid 1000，与老仓 gateway 用户一致）
RUN useradd -m -u 1000 gateway
USER gateway
WORKDIR /app
EXPOSE 8000

CMD ["uvicorn", "silvaengine_gateway.app:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]