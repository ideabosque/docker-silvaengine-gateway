# SilvaEngine Gateway — Banyan 生产数据面（banyan/ 一键部署）

本目录是 Banyan 系统的**生产镜像形态**一键部署交付物：目标服务器预装
Docker（含 Compose v2 插件）、git 与可访问 banyanos 私有仓的宿主 SSH key
（12 引擎仓为 GitHub 私有仓，阶段 2 走 SSH clone），运行 `bash deploy.sh`
即可完成全部部署（阶段 2 自动 clone/pull 全部源码仓），无需
Python / jq / awscli / rsync / 手工准备源码树。本机开发/测试支持
Podman + compose provider（docker-compose v2 二进制或 podman-compose）。

与老仓根 Dockerfile（KGE/RFQ/MCP 网关）和网关仓 `deploy/`（bind-mount
变体）的关系：本目录自包含（Dockerfile / docker-compose.yml /
requirements.txt / env 模板 / ddb_init.py / vendor 三包快照），gateway
源码**打进镜像**而非 git+ssh 安装或宿主机挂载；数据面（postgres / neo4j /
redis / DynamoDB Local）与网关仓 bind-mount 变体的 compose 服务块逐字
一致（网关宿主端口除外：本栈默认 8080 可配，变体恒 8000），两个形态共用
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
2. **git + SSH key（引擎仓必需）**：banyanos 12 引擎仓为 GitHub 私有仓，
   阶段 2 走 SSH `git@github.com:<repo>` clone——宿主需安装 git，并配置具有
   banyanos 组织读权限的 SSH key（`ssh -T git@github.com` 验证；passphrase
   key 请先 `ssh-add`；port 22 被墙的服务器可在 `~/.ssh/config` 配
   `Host github.com → HostName ssh.github.com / Port 443`）。宿主无 git
   时脚本可用容器镜像 `alpine/git` 兑底，但**仅适用于 https 公开仓**
   （ideabosque 4 仓）——SSH 私有仓遇容器兑底会 fail-closed 退出。
3. **源码树无需手工准备**：`deploy.sh` 阶段 2 自动 clone/pull 全部 16 仓
   （网关仓 / 12 引擎仓 / 3 框架仓）到工作区（= `banyan/` 的祖父目录；
   服务器上建议 `/var/www/banyan`）。服务器首次部署单命令引导：

```bash
git clone https://github.com/ideabosque/docker-silvaengine-gateway.git \
  /var/www/banyan/docker-silvaengine-gateway
cd /var/www/banyan/docker-silvaengine-gateway/banyan
bash deploy.sh          # 其余 16 仓由阶段 2 自动 clone（vendor 三包已内置）
```

阶段 2 产生的布局（亦即构建期源码路径发现链，可用 `*_DIR` 覆盖）：

```text
<工作区>/
├── docker-silvaengine-gateway/     # 本仓（banyan/ 在其下；自身更新手工 git pull）
│   └── banyan/vendor/              # vendor 三包内置于本仓（勿 clone 上游替代）
├── silvaengine_gateway/
│   └── silvaengine_gateway/        # 网关包（app.py / auth/ / middleware/ 等）
├── banyan/modules/                 # 12 引擎仓根（agent_engine/…/user_engine，
│                                   #   双层 modules/<repo>/<repo>/ 布局）
├── silvaengine_base/               # 三框架仓（repo 根即包 或 内含同名包目录，
├── silvaengine_utility/            #   两种布局 deploy.sh 自动归一化）
└── silvaengine_connections/
```

阶段 2 clone 分支映射与访问协议（`*_BRANCH` 环境变量可覆盖）：

| 仓库组 | 组织 | 分支默认 | 协议 | 工作区相对路径 |
|---|---|---|---|---|
| silvaengine_gateway（网关仓） | ideabosque | `feature/integrate-with-silvaengine-daemon`（Banyan 托管代码仅在此分支） | https（公开，`GITHUB_URL_BASE` 可镜像） | `silvaengine_gateway/` |
| 12 引擎仓（agent…user_engine） | banyanos | `main` | **ssh（GitHub 私有仓，需宿主 SSH key）** | `banyan/modules/<repo>/` |
| silvaengine_base / silvaengine_connections | ideabosque | `main` | https（公开） | 同名目录 |
| silvaengine_utility | ideabosque | `banyan`（含幂等 scope tenant_id 化提交） | https（公开） | `silvaengine_utility/` |

阶段 2 幂等语义：目录缺失 → clone（`--single-branch`；引擎仓 SSH，
ideabosque 仓 https）；已存在且为 git 仓 →
`fetch + ff` 更新（**脏工作区 fail-closed 拒绝更新**，防覆盖手工修改）；
已存在但非 git 目录 → 视为 rsync 手工布局跳过。vendor 三包内置于
`banyan/vendor/`（含 dynamodb_base TTL 缓存增强补丁，与上游有实质差异，
**勿以 clone 上游替代**，见 `vendor/README.md`）。SSH 首连经
`GIT_SSH_COMMAND` 默认参数（`StrictHostKeyChecking=accept-new` +
`BatchMode=yes`）非交互化：主机指纹 TOFU 自动记录、key 缺失时快速失败
而非挂起；已设 `GIT_SSH_COMMAND` 时尊重不覆盖，`~/.ssh/config`
（如 port 443 变体、IdentityFile）依然生效。

> **rsync 替代路径**：无法直连 GitHub 时，可在可达机器 clone 后 rsync 到服务
> 器保持相同相对布局（各仓为非 git 目录，阶段 2 自动按手工布局跳过）。
> **开发机注意**：在本地开发工作区上运行 deploy.sh 时，**必须设置五个
> `*_DIR` 路径覆盖**（`GATEWAY_PACKAGE_DIR` / `BANYAN_MODULES_DIR` /
> `SILVAENGINE_BASE_DIR` / `SILVAENGINE_UTILITY_DIR` /
> `SILVAENGINE_CONNECTIONS_DIR`）指向开发检出——设置即声明「该组源码自管」，
> 阶段 2 跳过该组 clone/pull，避免脚本 checkout/pull 开发工作区。

## 执行方式

```bash
cd docker-silvaengine-gateway/banyan
bash deploy.sh                # 部署/更新（幂等，可重复执行）
bash deploy.sh status         # 状态 + 健康检查 + 关键日志核查
bash deploy.sh down [-v]      # 停止（-v 连数据卷一起删除）
bash deploy.sh --dry-run      # 环境检测/源码获取/配置/端口预检/暂存/digest，不构建不起容器
bash deploy.sh --restart     # 部署后重启 gateway（改种子 JSON 后使用）
bash deploy.sh --force-build  # 强制重建镜像（默认源码未变自动跳过）
bash deploy.sh --force-env    # 重新生成 .env 与种子 JSON（密码会变更）
bash deploy.sh --self-test    # 内置纯逻辑自检（103 项，不碰 docker/podman）
```

### 十二个阶段

| 阶段 | 内容 | 失败时排查建议（脚本会打印） |
|---|---|---|
| 1 | 环境检测（Docker/Compose v2 或 Podman/provider；git 宿主优先，容器兑底仅限 https 仓） | 安装指引 / usermod / podman machine start |
| 2 | 源码获取：clone/pull 16 仓（ideabosque 4 仓 https，引擎 12 仓 SSH 私有仓；vendor 内置勿 clone；`*_DIR` 覆盖组自管跳过） | 引擎仓 SSH 失败：`ssh -T git@github.com` 验证 key 与 banyanos 访问权（passphrase 先 ssh-add；port 22 被墙经 `~/.ssh/config` 切 443）；https 仓可 `GITHUB_URL_BASE` 镜像基址；脏工作区 fail-closed |
| 3 | 配置四态状态机 + 源码树校验 | 缺失项一次列全；路径可用环境变量覆盖 |
| 4 | 端口预检 `${GATEWAY_PORT}`/8001/数据面四端口（PG/Redis/Neo4j http+bolt 宿主端口可配）+ 同名前缀异项目容器互斥 | 区分本栈容器/他项目容器/宿主机进程 |
| 5 | 暂存 .build-context/ + 计算源码 digest | tar 排除模式不兼容会显式失败 |
| 6 | 构建镜像（digest 与镜像 label 相同则跳过） | 换源指引（PYTHON_IMAGE / PIP_INDEX_URL） |
| 7 | 启动 DynamoDB Local | DDB_LOCAL_IMAGE 可换源 |
| 8 | ddb-init：建表 → 灌种子 → 核验（幂等覆盖写） | compose logs ddb-init；重跑自愈 |
| 9 | 启动五服务（gateway + 数据面） | GATEWAY_WAIT_TIMEOUT 延长等待 |
| 10 | 健康验证（容器 healthy + /health + 关键日志）| 容器日志指引 + 内存建议 |
| 11 | 超管初始化（admin-init 幂等收敛，密码永不覆盖）| compose logs admin-init；重跑幂等 |
| 12 | 资源注册与根角色授权收敛（resource-init：login → registerResources → 终态核验）| compose logs resource-init / 网关日志；重跑幂等 |

### 环境变量覆盖

| 变量 | 时机 | 默认 | 说明 |
|---|---|---|---|
| `TENANT_PART_ID` | 首次生成 .env | `nestaging` | 租户 part_id |
| `GATEWAY_PORT` | 首次生成 .env | `8080` | 网关宿主端口（容器内恒 8000；legacy .env 缺键自动补写） |
| `POSTGRES_PORT` / `REDIS_PORT` / `NEO4J_HTTP_PORT` / `NEO4J_BOLT_PORT` | 首次生成 .env | `5432` / `6379` / `7474` / `7687` | 数据面宿主端口（容器侧端口恒不变；宿主 5432/6379 等被保留服务占用时经这些键换道；legacy .env 缺键自动补写） |
| `NEO4J_AUTH` | 首次生成 .env | 随机 16 位 | neo4j 认证（格式 `neo4j/<纯字母数字>`；预置数据卷迁移场景导出固定旧凭据一次到位，如 `neo4j/12345abc`；缺省随机生成） |
| `GATEWAY_BRANCH` / `ENGINE_BRANCH` / `SILVAENGINE_*_BRANCH` | 阶段 2 clone | 网关仓 feature 分支；引擎 main；base/connections main；utility banyan | 各仓组 clone 分支覆盖 |
| `GITHUB_URL_BASE` | 阶段 2 | `https://github.com` | **https 仓** clone 基址（镜像加速）；引擎 SSH 仓不受影响 |
| `GIT_SSH_COMMAND` | 阶段 2 | `ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes` | 引擎 SSH 仓所用 ssh 命令；已设时尊重不覆盖（`~/.ssh/config` 依然生效） |
| `GIT_IMAGE` | 阶段 2 | daocloud `alpine/git` | 宿主无 git 时兑底容器镜像（仅限 https 仓；SSH 私有仓遇容器兑底 fail-closed） |
| `GATEWAY_PACKAGE_DIR` 等路径类 | 每次运行 | 见前置条件布局 | 六个源码路径覆盖（不写入 .env；**设置即声明该组自管，阶段 2 跳过该组 clone/pull**） |
| `PYTHON_IMAGE` | 每次构建 | `docker.m.daocloud.io/library/python:3.12-slim` | 基础镜像（可换官方源） |
| `PIP_INDEX_URL` | 每次构建 | 阿里云 pypi | pip 源（可换官方源） |
| `POSTGRES_IMAGE` 等 4 个数据面镜像 | 首次生成 .env | daocloud 加速源 | 生成后直接改 .env 生效 |
| `GATEWAY_WAIT_TIMEOUT` | 每次运行 | `600` | 健康等待上限秒数 |

## 部署后验证

```bash
curl -sS http://127.0.0.1:8080/health            # 应返回 200（GATEWAY_PORT 可配，默认 8080）
bash deploy.sh status                             # 五容器 healthy + 两条关键日志
# GraphQL 负向探针（无令牌应 401/403，证明路径规范化+鉴权桥生效）：
curl -sS -X POST http://127.0.0.1:8080/beta/core/banyan/user_engine_graphql \
  -H 'content-type: application/json' -d '{"query": "{ __typename }"}'
# 超级管理员登录闭环（阶段 11 已建超管 + platform:super_admin 绑定；LoginInput
# 以 email 字段承载账号 + 幂等键，与 resource-init 同一契约，io-deployment 实测）：
curl -sS -X POST http://127.0.0.1:8080/beta/core/banyan/user_engine_graphql \
  -H 'content-type: application/json' -H 'part_id: <TENANT_PART_ID>' \
  -d '{"query": "mutation($k: ID!, $input: LoginInput!){ login(idempotencyKey: $k, input: $input){ authToken user { id } } }", "variables": {"k": "<任意幂等键>", "input": {"email": "<ADMIN_ACCOUNT>", "password": "<ADMIN_PASSWORD>"}}}'
# 其余业务 mutation 骨架（携带 part_id 头 + Banyan JWT）：
curl -sS -X POST http://127.0.0.1:8080/beta/core/banyan/user_engine_graphql \
  -H 'content-type: application/json' -H 'part_id: <TENANT_PART_ID>' \
  -H 'Authorization: Bearer <登录返回的 authToken>' \
  -d '{"query": "mutation { ... }"}'
```

关键启动日志（`check_log` 必查两条 REQUIRED）：
- `se-configdata setting loaded: setting_id=` —— DDB 配置叠加生效
- `Pool bootstrap: framework pools created` —— 池引导生效（经 ConnectionPoolManager
  同步建池：postgres_main/audit/telemetry + httpx 回环池，引擎复用免自建）

### 超级管理员凭据（阶段 11）

- 首次部署后系统内**没有任何用户**，阶段 11 的 `admin-init` 一次性容器
  （`scripts/admin_init.py`，复用网关镜像）幂等收敛引导账户：
  - 等待 PostgreSQL 可连 → 等待预设角色 `platform:super_admin`（网关冷启动
    建表时已自动种子，阶段 10 healthy 即完成，重试窗口仅为兑底）；
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
  种子 JSON sed 渲染路径；强度门控：≥12 位 + ≥3 字符类，弱密码在阶段 11
  显式失败（exit 2）。
- 手工重跑：`docker compose run --rm admin-init`（幂等）。

### 平台资源与根角色授权（阶段 12）

- 全新部署后资源目录（`tenant_perm_resource`：前端可见性资源 MODULE/MENU/
  PAGE/BUTTON + 各引擎已接入 API 资源）与角色-资源授权（`tenant_perm_role_resource`）
均为空——perm_engine 刻意不在冷启动注册资源（需导入全部 12 引擎 deploy()，
  冷启动超时风险，见 perm_engine `handlers/config.py` 注释），历史上需登录前端
  手动点「资源注册」。阶段 12 的 `resource-init` 一次性容器
  （`scripts/resource_init.py`，复用网关镜像）通过**与前端按钮同一正向通道**
  补上这一环：
  - 等网关 /health → 超管 login（匿名白名单 Mutation，顺带端到端验证阶段 11
    的登录闭环）→ `registerResources` Mutation（平台级，需 platform:super_admin
    角色，阶段 11 已绑定；含 REGISTER_RESOURCES 审计行）；
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
- **阶段 2 幂等**：目录缺失→clone；已有 git 仓→`fetch + ff`（脏仓 fail-closed
  拒绝动，防覆盖手工修改）；已有非 git 目录→按手工/rsync 布局跳过；`*_DIR`
  覆盖组自管跳过；legacy .env 缺 `GATEWAY_PORT`/数据面端口键/超管键
  自动补写不覆盖。
  引擎仓走 SSH（私有，`GIT_SSH_COMMAND` 非交互化，容器兑底模式遇 SSH 仓
  fail-closed）；ideabosque 仓走 https（公开，可镜像）。
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
- `vendor/` 三包为 api-runtime vendor 快照的**嵌入副本**（其中
  silvaengine_dynamodb_base 含 se-* 配置表 TTL 缓存增强补丁，与上游仓库
  存在实质差异，**勿以 clone 上游替代**），来源与升级注意见 `vendor/README.md`。
- bind-mount 变体（网关仓 `deploy/deploy.sh`）与本形态**共用容器名前缀
  与卷名，请勿在同一台机器同时运行两形态**——端口已分道（本栈默认 8080
  可配、变体恒 8000），拦截改由 deploy.sh 阶段 4 的容器名归属互斥检查承担。
- 回归基线：本形态与 bind-mount 形态共用 `Wireframes_思维导图_V3.md`
  V5.2 基准与网关仓 `test_banyan_compat.py`（42 用例）验收清单。

## 已知边界

- **引擎仓需 SSH key**：banyanos 12 引擎仓为 GitHub 私有仓，阶段 2 走 SSH
  clone——宿主须有 banyanos 组织读权限的 SSH key（账户级 key 或各仓
  deploy key 均可）。SSH 不可达且不愿配 key 时，可走「rsync 替代路径」
  （见前置条件）或设 `BANYAN_MODULES_DIR` 指向已有源码目录。
- 网关宿主端口默认 8080（`.env` 的 `GATEWAY_PORT` 可配）；容器内监听恒 8000
  （healthcheck / 跨引擎回环 / resource-init 均走容器内地址，不受宿主端口影响）。
- 数据面宿主端口默认 5432/6379/7474/7687（`.env` 的 `POSTGRES_PORT` /
  `REDIS_PORT` / `NEO4J_HTTP_PORT` / `NEO4J_BOLT_PORT` 可配）；容器侧端口恒
  不变——引擎连接、admin-init、resource-init 均走容器网络服务名（`postgres` /
  `neo4j` / `redis`），不受宿主端口影响；宿主端口仅供本机调试。
- 模式 A（DDB Local 全离线）专用；读真实云端 DynamoDB 的模式 B 走网关仓
  `deploy/README.md` 手动路径。
- 种子 JSON 渲染要求配置值纯字母数字（sed 注入面收敛）；复杂密码请改用
  模式 B 手动灌表。
- 镜像构建后源码改动不会自动进容器——重跑 `bash deploy.sh`（阶段 2 自动
  pull 最新源码 + digest 变化自动重建）；仅改种子 JSON 用 `--restart`。