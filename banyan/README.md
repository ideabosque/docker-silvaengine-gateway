# SilvaEngine Gateway — Banyan 生产数据面（banyan/ 一键部署）

本目录是 Banyan 系统的**生产镜像形态**一键部署交付物：目标服务器只需预装
Docker（含 Compose v2 插件），运行 `bash deploy.sh` 即可完成全部部署，
无需 Python / jq / awscli / rsync / SSH key。本机开发/测试支持
Podman + compose provider（docker-compose v2 二进制或 podman-compose）。

与老仓根 Dockerfile（KGE/RFQ/MCP 网关）和网关仓 `deploy/`（bind-mount
变体）的关系：本目录自包含（Dockerfile / docker-compose.yml /
requirements.txt / env 模板 / ddb_init.py），gateway 源码**打进镜像**而非
git+ssh 安装或宿主机挂载；数据面（postgres / neo4j / redis / DynamoDB
Local）与网关仓 bind-mount 变体的 compose 服务块逐字一致，两个形态共用
同一验收清单。

## 架构

```text
浏览器/前端 → gateway(FastAPI 单容器，12 引擎进程内分发，uvicorn PID 1)
              ├─ postgres   关系型/日志型数据（12 引擎共用）
              ├─ neo4j      图数据与向量数据唯一存储（知识图谱/记忆/embedding）
              ├─ redis      短时令牌/验证码/MFA 会话（AOF）
              └─ ddb-local  DynamoDB Local（se-configdata 配置叠加，全离线模式 A）
                             ddb-init 幂等建表灌种子（复用网关镜像，免 pip）
```

- 跨引擎互调走 `BANYAN_LOOPBACK_BASE_URL` 容器内回环（服务令牌经鉴权桥
  验证，与前端同一路径）。
- `SETTING_SOURCE=se-configdata` + `ENDPOINT_ID=banyan`：启动时叠加 DDB
  配置记录 + Banyan 路径规范化 + PermAuthorizer 鉴权桥。

## 前置条件

1. **Docker + Compose v2**（服务器）或 **Podman + compose provider**（本机）：
   `brew install docker-compose`（macOS）/ `apt install docker-compose-plugin`
   （Linux）后 `podman compose version` 应能解析；macOS 需 `podman machine
   start`，内存建议 ≥4GiB（`podman machine set --memory 4096`）。
2. **源码树**（构建期使用，不打进镜像的路径不落 .env）按默认发现链布局：

```text
<任意根>/
├── docker-silvaengine-gateway/     # 本仓（banyan/ 在其下）
├── silvaengine_gateway/
│   └── silvaengine_gateway/        # 网关包（app.py / auth/ / middleware/ 等）
├── banyan/modules/                 # 12 引擎仓根（agent_engine/…/user_engine，
│                                   #   双层 modules/<repo>/<repo>/ 布局）
├── silvaengine_base/               # 三框架仓（repo 根即包 或 内含同名包目录，
├── silvaengine_utility/            #   两种布局 deploy.sh 自动归一化）
├── silvaengine_connections/
└── ../docker/api-runtime/vendor/   # vendor 快照（constants/definitions/
                                    #   dynamodb_base 三包）
```

   不符合默认布局时，用环境变量覆盖（见下表）。rsync 示例：

```bash
rsync -a --exclude .git --exclude .venv --exclude __pycache__ \
  /path/ideabosque/ server:/srv/ideabosque/
rsync -a --exclude .git /path/docker/api-runtime/vendor/ server:/srv/docker/api-runtime/vendor/
```

## 执行方式

```bash
cd docker-silvaengine-gateway/banyan
bash deploy.sh                # 部署/更新（幂等，可重复执行）
bash deploy.sh status         # 状态 + 健康检查 + 关键日志核查
bash deploy.sh down [-v]      # 停止（-v 连数据卷一起删除）
bash deploy.sh --dry-run      # 环境检测/配置生成/端口预检/暂存/digest，不构建不起容器
bash deploy.sh --restart     # 部署后重启 gateway（改种子 JSON 后使用）
bash deploy.sh --force-build  # 强制重建镜像（默认源码未变自动跳过）
bash deploy.sh --force-env    # 重新生成 .env 与种子 JSON（密码会变更）
bash deploy.sh --self-test    # 内置纯逻辑自检（59 项，不碰 docker/podman）
```

### 十一个阶段

| 阶段 | 内容 | 失败时排查建议（脚本会打印） |
|---|---|---|
| 1 | 环境检测（Docker/Compose v2 或 Podman/provider） | 安装指引 / usermod / podman machine start |
| 2 | 配置四态状态机 + 源码树校验 | 缺失项一次列全；路径可用环境变量覆盖 |
| 3 | 端口预检 8000/8001/5432/6379/7474/7687 | 区分本栈容器/他项目容器/宿主机进程 |
| 4 | 暂存 .build-context/ + 计算源码 digest | tar 排除模式不兼容会显式失败 |
| 5 | 构建镜像（digest 与镜像 label 相同则跳过） | 换源指引（PYTHON_IMAGE / PIP_INDEX_URL） |
| 6 | 启动 DynamoDB Local | DDB_LOCAL_IMAGE 可换源 |
| 7 | ddb-init：建表 → 灌种子 → 核验（幂等覆盖写） | compose logs ddb-init；重跑自愈 |
| 8 | 启动五服务（gateway + 数据面） | GATEWAY_WAIT_TIMEOUT 延长等待 |
| 9 | 健康验证（容器 healthy + /health + 关键日志）| 容器日志指引 + 内存建议 |
| 10 | 超管初始化（admin-init 幂等收敛，密码永不覆盖）| compose logs admin-init；重跑幂等 |
| 11 | 资源注册与根角色授权收敛（resource-init：login → registerResources → 终态核验）| compose logs resource-init / 网关日志；重跑幂等 |

### 环境变量覆盖

| 变量 | 时机 | 默认 | 说明 |
|---|---|---|---|
| `TENANT_PART_ID` | 首次生成 .env | `nestaging` | 租户 part_id |
| `GATEWAY_PACKAGE_DIR` 等路径类 | 每次运行 | 见前置条件布局 | 六个源码路径覆盖（不写入 .env） |
| `PYTHON_IMAGE` | 每次构建 | `docker.m.daocloud.io/library/python:3.12-slim` | 基础镜像（可换官方源） |
| `PIP_INDEX_URL` | 每次构建 | 阿里云 pypi | pip 源（可换官方源） |
| `POSTGRES_IMAGE` 等 4 个数据面镜像 | 首次生成 .env | daocloud 加速源 | 生成后直接改 .env 生效 |
| `GATEWAY_WAIT_TIMEOUT` | 每次运行 | `600` | 健康等待上限秒数 |

## 部署后验证

```bash
curl -sS http://127.0.0.1:8000/health            # 应返回 200
bash deploy.sh status                             # 五容器 healthy + 两条关键日志
# GraphQL 负向探针（无令牌应 401/403，证明路径规范化+鉴权桥生效）：
curl -sS -X POST http://127.0.0.1:8000/beta/core/banyan/user_engine_graphql \
  -H 'content-type: application/json' -d '{"query": "{ __typename }"}'
# 超级管理员登录闭环（阶段 10 已建超管 + platform:super_admin 绑定）：
curl -sS -X POST http://127.0.0.1:8000/beta/core/banyan/user_engine_graphql \
  -H 'content-type: application/json' -H 'part_id: <TENANT_PART_ID>' \
  -d '{"query": "mutation { login(input: {account: \"<ADMIN_ACCOUNT>\", password: \"<ADMIN_PASSWORD>\"}) { authToken ... } }"}'
# 其余业务 mutation 骨架（携带 part_id 头 + Banyan JWT）：
curl -sS -X POST http://127.0.0.1:8000/beta/core/banyan/user_engine_graphql \
  -H 'content-type: application/json' -H 'part_id: <TENANT_PART_ID>' \
  -H 'Authorization: Bearer <登录返回的 authToken>' \
  -d '{"query": "mutation { ... }"}'
```

关键启动日志（`check_log` 必查两条 REQUIRED）：
- `se-configdata setting loaded: setting_id=` —— DDB 配置叠加生效
- `Pool bootstrap: framework pools created` —— 池引导生效（经 ConnectionPoolManager
  同步建池：postgres_main/audit/telemetry + httpx 回环池，引擎复用免自建）

### 超级管理员凭据（阶段 10）

- 首次部署后系统内**没有任何用户**，阶段 10 的 `admin-init` 一次性容器
  （`scripts/admin_init.py`，复用网关镜像）幂等收敛引导账户：
  - 等待 PostgreSQL 可连 → 等待预设角色 `platform:super_admin`（网关冷启动
    建表时已自动种子，阶段 9 healthy 即完成，重试窗口仅为兑底）；
  - 账号在 `tenant_user` 缺失 → INSERT（`status=ACTIVE`、`email_verified=true`、
    Argon2id 哈希，参数与 `user_engine/utils/security.py` 逐字一致）；
    已存在 → **仅收敛** status/email_verified，**密码永不覆盖**；
  - 角色绑定写入 `tenant_perm_user_role`（UNIQUE 约束 ON CONFLICT DO NOTHING
    天然幂等）；写后回读核验。
- 凭据来源：`.env` 的 `ADMIN_ACCOUNT`（默认 `admin@banyanos.dev`）与
  `ADMIN_PASSWORD`（默认 `B@nyan0s.d3v`）——**默认密码仅供首次登录，
  部署完成后请立即登录修改**；改密后重跑 deploy.sh 不会重置密码。
- 有意不写入 `password_history` / `audit` 表（引擎拥有其语义，引导只保证
  登录闭环）；不打印密码明文（摘要仅提示密码位于 .env）。
- 密码含 `@` / `.` 等非字母数字字符——仅走 .env 直写通道，严禁进入
  种子 JSON sed 渲染路径；强度门控：≥12 位 + ≥3 字符类，弱密码在阶段 10
  显式失败（exit 2）。
- 手工重跑：`docker compose run --rm admin-init`（幂等）。

### 平台资源与根角色授权（阶段 11）

- 全新部署后资源目录（`tenant_perm_resource`：前端可见性资源 MODULE/MENU/
  PAGE/BUTTON + 各引擎已接入 API 资源）与角色-资源授权（`tenant_perm_role_resource`）
  均为空——perm_engine 刻意不在冷启动注册资源（需导入全部 12 引擎 deploy()，
  冷启动超时风险，见 perm_engine `handlers/config.py` 注释），历史上需登录前端
  手动点「资源注册」。阶段 11 的 `resource-init` 一次性容器
  （`scripts/resource_init.py`，复用网关镜像）通过**与前端按钮同一正向通道**
  补上这一环：
  - 等网关 /health → 超管 login（匿名白名单 Mutation，顺带端到端验证阶段 10
    的登录闭环）→ `registerResources` Mutation（平台级，需 platform:super_admin
    角色，阶段 10 已绑定；含 REGISTER_RESOURCES 审计行）；
  - 一次调用同时完成：资源差量入库 → 剪枝/转换/补链/映射 → 预设角色
    （super_admin / tenant_admin / merchant_admin / 商户根角色）全量 PERMIT 授权；
  - 终态核验：GraphQL `resourceRegistrationStats`（信息性，不一致仅 WARN——
    code 去重与 (module, name) 去重可能合法不等）+ **DB 反连接零缺口**（每个
    存活资源均有超管授权行，权威判定）；
  - **幂等**：重跑 `inserted=0`、授权零缺口 → exit 0；Banyan 升级后重跑会自动
    注册新增引擎 action 并授权（收敛语义）。
- **改密后重部署**：首次登录后修改超管密码（runbook 建议）会使 `.env` 的
  `ADMIN_PASSWORD` 失效——此时登录失败但 DB 终态已收敛 → WARN 放行（不阻塞
  幂等重跑）；若资源未就绪且登录失败则 fail-closed 退出，按提示排查。
- 部分引擎导入失败（`errors>0`）会 fail-closed 退出并列出失败明细，重跑
  自动补齐（幂等差量写入）。
- 首次注册需在网关进程内导入 12 引擎（10-60s 属正常），HTTP 超时默认 300s，
  可用 `RESOURCE_INIT_HTTP_TIMEOUT` 覆盖。
- 手工重跑：`docker compose run --rm resource-init`（幂等）。

Banyan 系统 API 接口契约文档（端点、认证、12 引擎 644 个操作清单、示例
与已知边界）见 [API.md](./API.md)。

## 幂等与错误处理设计

- **四态状态机**：`.env` + 种子 JSON 双在→复用；双无→生成；仅 `.env`→补渲染
  种子；仅 JSON（孤儿态）→报错退出（防密码错位）。
- **digest 跳过构建**：暂存树（文件清单排序 + 内容双重哈希）写入镜像 label
  `org.silvaengine.banyan.source-digest`；重复执行时 label 匹配则跳过构建，
  `--force-build` 强制重建。requirements.txt 变更同样触发重建。
- **失败安全退出**：任一阶段失败打印原因 + 排查建议后退出，不遗留损坏的
  中间状态；修复后直接重跑（幂等），整体回滚 `bash deploy.sh down`。
- **种子自愈**：ddb-local 为 `-inMemory`，每次部署重灌种子；种子 JSON 由
  `.env` 渲染，占位符残留/`_comment` 残留会显式失败。
- **权限**：`.env` 与种子 JSON 以 600 权限生成；容器内以 uid 1000 非 root
  运行。

## 清理

```bash
bash deploy.sh down        # 停止容器（保留数据卷与镜像）
bash deploy.sh down -v     # 连数据卷一起删除（postgres/neo4j/redis 数据清空）
rm -rf .build-context      # 可选：删除暂存目录（下次部署自动重建）
```

## 与网关仓 `deploy/` 的同步关系

- `env/se-configdata.example.json`、`scripts/ddb_init.py`、`requirements.txt`
  为网关仓 `deploy/` 对应文件的副本，权威源在网关仓；网关仓变更后同步
  到本目录（本目录不自动跟踪）。
- bind-mount 变体（网关仓 `deploy/deploy.sh`）与本形态的容器名/卷名/端口
  相同，**请勿在同一台机器同时运行两形态**（脚本端口预检会拦截并提示）。
- 回归基线：本形态与 bind-mount 形态共用 `Wireframes_思维导图_V3.md`
  V5.2 基准与网关仓 `test_banyan_compat.py`（42 用例）验收清单。

## 已知边界

- 模式 A（DDB Local 全离线）专用；读真实云端 DynamoDB 的模式 B 走网关仓
  `deploy/README.md` 手动路径。
- 种子 JSON 渲染要求配置值纯字母数字（sed 注入面收敛）；复杂密码请改用
  模式 B 手动灌表。
- 镜像构建后源码改动不会自动进容器——重跑 `bash deploy.sh`（digest 变化
  自动重建）；仅改种子 JSON 用 `--restart`。