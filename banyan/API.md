# Banyan 系统 API 接口文档（docker-silvaengine-gateway 环境）

> **适用范围**：本文档描述 `docker-silvaengine-gateway/banyan/` 一键部署形态
> （网关单容器进程内分发 12 引擎 + postgres/neo4j/redis/DynamoDB Local 数据面）
> 暴露的 Banyan 系统 HTTP API 契约。
> **数据来源**：网关路由 manifest（`silvaengine_gateway/module_routes/*.yaml`）、
> 12 引擎 `deploy()` 静态配置、`perm_engine` 鉴权器（`ANONYMOUS_OPS`），
> 并经本地 podman 五容器栈实测验证（2026-09-28）。
> **操作明细权威源**：各引擎 `main.py` 的 `deploy()` 配置；**精确参数签名**以
> 线上 introspection 为准（见 §6）。

---

## 1. 概述

### 1.1 架构与请求链路

```text
客户端 → gateway(FastAPI, :8000)
          ├─ BanyanPathNormalizer   剥 /{stage}/{area} 前缀（纯 ASGI）
          ├─ BanyanAuthorizerBridge 真实 PermAuthorizer 鉴权（fail-closed）
          ├─ FlexJWTMiddleware      见桥标记让位（网关自有路由用）
          ├─ RateLimitMiddleware    全局限流（默认 100 次/60s）
          └─ 动态路由 /{endpoint_id}/{function} → 引擎 dispatch_graphql（线程池）
                                          ├─ postgres  关系型/日志型数据
                                          ├─ neo4j     图数据/向量数据
                                          ├─ redis     验证码/MFA 会话
                                          └─ ddb-local se-configdata 配置
```

- 引擎 schema 启动时静态构建（`build_graphql_schema()` 类级缓存），进程内分发，无跨进程 RPC。
- 跨引擎互调走容器内回环 `BANYAN_LOOPBACK_BASE_URL`（服务令牌与前端同路径、同鉴权）。

### 1.2 基址与路径契约

基址：`http://<host>:8000`（容器端口映射 `8000:8000`）。

两种等价路径形态（实测均可访问）：

| 形态 | 形状 | 说明 |
|---|---|---|
| Banyan 前端契约 | `/beta/core/banyan/{function}` | 与 AWS API Gateway 资源布局一致；`stage`/`area` 可经 `ADAPTER_STAGE`/`ADAPTER_AREA` 配置，默认 `beta`/`core` |
| 网关原生契约 | `/banyan/{function}` | 路径只携带 `{endpoint_id}`（默认 `banyan`，`ENDPOINT_ID` 配置），租户经请求头传递 |

> 路径规范化仅剥**精确匹配**的 `/{stage}/{area}` 前缀；错误 stage（如 `/prod/core/...`）
> 不被改写，自然 404。少于 3 段的路径（如 `/health`）不受影响。

### 1.3 端点总览（12 引擎，全部 POST）

| # | 引擎 | 端点路径（Banyan 形态） | Query | Mutation | 合计 |
|---|---|---|---|---|---|
| 1 | agent_engine | `/beta/core/banyan/agent_engine_graphql` | 23 | 37 | 60 |
| 2 | capability_engine | `/beta/core/banyan/capability_engine_graphql` | 37 | 52 | 89 |
| 3 | knowledge_engine | `/beta/core/banyan/knowledge_engine_graphql` | 31 | 42 | 73 |
| 4 | llm_engine | `/beta/core/banyan/llm_engine_graphql` | 12 | 16 | 28 |
| 5 | memory_engine | `/beta/core/banyan/memory_engine_graphql` | 28 | 44 | 72 |
| 6 | merchant_engine | `/beta/core/banyan/merchant_engine_graphql` | 14 | 23 | 37 |
| 7 | monitor_engine | `/beta/core/banyan/monitor_engine_graphql` | 28 | 22 | 50 |
| 8 | orchestration_engine | `/beta/core/banyan/orchestration_engine_graphql` | 16 | 32 | 48 |
| 9 | perm_engine | `/beta/core/banyan/perm_engine_graphql` | 38 | 60 | 98 |
| 10 | prompt_engine | `/beta/core/banyan/prompt_engine_graphql` | 5 | 3 | 8 |
| 11 | setting_engine | `/beta/core/banyan/setting_engine_graphql` | 9 | 19 | 28 |
| 12 | user_engine | `/beta/core/banyan/user_engine_graphql` | 12 | 41 | 53 |
| | | **合计** | **253** | **391** | **644** |

另有网关自身路由（非 Banyan 域）：`GET /health`（无鉴权，见 §7）。

---

## 2. 通用约定

### 2.1 请求头

| Header | 必填 | 说明 |
|---|---|---|
| `Content-Type: application/json` | 是 | 请求体为 JSON |
| `part_id` | 是 | 租户分区标识（部署配置 `TENANT_PART_ID`，默认种子 `nestaging`）。兼容别名 `Part-Id` / `Part-ID`。**缺失即 400** |
| `Authorization: Bearer <token>` | 条件 | Banyan JWT；仅匿名白名单操作（§3.2）可省略 |
| `x-api-key` | 否 | 仅透传给引擎，**容器形态不校验**（见 §8 已知边界） |

网关以 `{endpoint_id}#{part_id}` 构造 `partition_key`（如 `banyan#nestaging`）注入引擎上下文。

### 2.2 请求体

```json
{
  "query": "mutation Login($input: LoginInput!, $idempotencyKey: ID!) { ... }",
  "variables": { "input": { "...": "..." }, "idempotencyKey": "..." },
  "operation_name": "Login"
}
```

| 键 | 必填 | 说明 |
|---|---|---|
| `query` | 是 | GraphQL 文档。**为空时引擎返回 200 + `{"errors": "GraphQL query 不能为空。"}`**（实测） |
| `variables` | 否 | 变量对象 |
| `operation_name` | 否 | 多操作文档时指定（注意键为 **snake_case**，非 GraphQL 惯例的 `operationName`） |

- 顶层 `context` / `metadata` 由网关注入，调用方传入值会被覆盖关键键（见 §3.5），**勿依赖**。
- GraphQL 字段与参数名为 **camelCase**（`registerUser`、`idempotencyKey`、`emailSent`）；仅 WebAuthn 系列为保留官方拼写显式命名（`beginWebAuthnRegistration` 等）。

### 2.3 响应格式

统一 JSON。三种形态：

**① 成功**（HTTP 200）：

```json
{ "data": { "me": { "id": "...", "email": "..." } } }
```

**② GraphQL 应用级错误**（HTTP 200，errors 可与部分 data 并存）：

```json
{ "errors": [ { "message": "Cannot query field 'xxx' on type 'UserType'. ..." } ] }
```

**③ 网关/传输层错误**（HTTP 400/401/403/429/500）：

```json
{ "detail": "Authentication required" }
```

### 2.4 错误码总表（实测）

| HTTP | 场景 | 响应体（`detail`） | 来源 |
|---|---|---|---|
| 200 | 成功 / GraphQL 应用级错误 | `{"data":...}` / `{"errors":[...]}` | 引擎 |
| 400 | 缺 `part_id` 头 | `Part-Id header is required to construct partition_key` | 路由 handler |
| 401 | 无令牌且非匿名白名单 | `Authentication required` | BanyanAuthorizerBridge |
| 401 | 令牌无效/过期 | `Invalid token: Signature verification failed` 等 | PermAuthorizer（桥转 401） |
| 401 | 网关域兑底（网关自有路由缺令牌，或未挂桥部署的路径） | `Not authenticated` | FlexJWT 中间件 |
| 403 | 授权拒绝（PermissionError，非认证类） | `Forbidden`（或具体原因） | PermAuthorizer（桥转 403） |
| 404 | 路径不存在（stage/area/function 错误） | `Not Found` | FastAPI 默认 |
| 429 | 超限流（默认 100 次/60s，`GATEWAY_RATE_LIMIT`/`GATEWAY_RATE_WINDOW` 配置） | `Rate limit exceeded` | RateLimitMiddleware |
| 500 | perm_engine 不可导入（fail-closed） | `Banyan authorizer unavailable` | BanyanAuthorizerBridge |

### 2.5 CORS

默认 `Access-Control-Allow-Origin: *`（无 credentials）；配置 `GATEWAY_CORS_ORIGINS`（逗号分隔）后按白名单回显并允许 credentials。

---

## 3. 认证与授权

### 3.1 Banyan JWT

| 属性 | 值 |
|---|---|
| 算法 | HS256 |
| issuer | `banyan`（`jwt_config.issuer`） |
| audience | `banyan-graphql`（`jwt_config.audience`） |
| secret | 部署种子 `se-configdata` 的 `jwt_secret`（ddb-init 幂等灌入 DDB Local，网关启动叠加） |
| 必备 claim | `sub`（用户 ID，必非空，否则 401 `Invalid token subject`） |
| 可选 claim | `tenant_id` / `merchant_id`（平台级用户为空） |

- 登录成功由 `login` 签发 `authToken`（访问令牌）与 `renewToken`（刷新令牌，经 `refreshToken` mutation 轮换）。
- 每次请求网关以真实 `perm_engine.PermAuthorizer` 验证令牌并从 PostgreSQL 聚合角色
  （直接角色 + 用户组角色）。**无角色用户可正常通过鉴权**（roles=[]，实测），拥有
  平台级角色（`tenant_id IS NULL` 且非域限定预设码）则 `is_admin=true`。

### 3.2 匿名操作白名单（ANONYMOUS_OPS，14 个）

以下操作设计上无需令牌即可调用（`perm_engine/perm_engine/authorizer.py` 权威源）：

| 引擎 | 操作 | 类别 |
|---|---|---|
| user_engine | `login`、`loginWithMfa`、`loginWithSso`、`refreshToken` | 认证 |
| user_engine | `registerUser`、`resendActivationEmail` | 注册 |
| user_engine | `requestPasswordReset`、`confirmPasswordReset`、`verifyResetCode` | 密码重置 |
| user_engine | `sendEmailVerificationCode`、`verifyEmailCode` | 邮箱验证 |
| user_engine | `checkPasswordStrength` | 查询 |
| user_engine | `authStatus`、`me` | 查询（`me` 无 token 返回 null，不拒绝） |

匹配规则：网关解析请求体 `query`，以正则 `\b<op>\s*[({]` 精确匹配操作名
（区分 `login` 与 `loginHistory` 等子串）。

> ✅ **P0 已修复（本批次实施，见 §3.3）**：鉴权桥对上述匿名操作放行后
> 不再被路由级依赖二次拦截，注册/登录/重置等匿名闭环在网关形态可用
> （§4 示例已完整走通；§9 有实测记录）。带令牌请求行为不变。

### 3.3 P0 修复记录（已实施）

- **历史现象**（实测 2026-09-28 修复前）：无令牌调用 `registerUser`/`login` →
  `401 {"detail":"Not authenticated"}`（该消息来自路由级依赖，非鉴权桥——
  鉴权桥的白名单放行与 FlexJWT 让位机制本身工作正常）。
- **根因**：12 个 Banyan 引擎路由 yaml 曾声明 `auth: true`，
  `build_router_from_manifest` 为其附加 `Depends(get_current_user)`；该依赖要求
  `request.state.user` 非空，而匿名白名单放行的请求没有 user claims。
- **修复**（已实施）：12 个 Banyan 引擎路由 yaml 改为 `auth: false` —— Banyan
  域的鉴权权威收敛到 fail-closed 鉴权桥（无令牌非白名单 401、令牌无效 401、
  perm_engine 不可导入 500）；FastAPI 全局中间件 FlexJWT 仍在，非 Banyan 路径
  不受影响；非 Banyan 模块路由仍 `auth: true`。
- **回归锁**（网关仓 `test_banyan_compat.py`，42 用例）：新增 4 个用例覆盖
  真实加载路径——真实 manifest 加载断言 12 模块全路由 `auth is False`（含
  非 Banyan 模块仍 true 的边界）；`build_router_from_manifest` 以真实
  `get_current_user` 断言 Banyan 路由不带路由级依赖；全链用例以真实路由
  + 鉴权桥匿名放行断言匿名 login 请求 200 且到达引擎 dispatch（回退 yaml
  即 401 失败）；认证变体断言 claims 仍按 Lambda 对齐提升到 context。

### 3.4 鉴权链与 fail-closed 语义

```
CORS → PathNormalizer → AuthorizerBridge → FlexJWT → RateLimit → 路由 → 引擎
```

- 仅当 `ENDPOINT_ID` 已配置（banyan 部署恒为 `banyan`）才挂载桥与规范化器。
- 桥对 `/{endpoint_id}/...` 路径合成 Lambda 代理事件，在真实
  `PermAuthorizer.verify_permission` 中验证：`AuthenticationError` → 401、
  其它 `PermissionError` → 403、`ImportError`（perm_engine 缺失）→ 500。
  **任何桥异常都不会让未认证请求到达引擎**（fail-closed）。
- 跨引擎服务令牌（`orchestration_engine` 回环调用）与用户令牌同验证路径，
  无内部旁路。

### 3.5 权限上下文注入（引擎可见的 claims）

鉴权通过后，网关将 claims 提升为引擎 GraphQL 顶层 context 键（Lambda 对齐）：

| 键 | 保证 |
|---|---|
| `user_id` / `is_admin` / `roles` | **防伪造**：authorizer 恒有值，覆盖 body 注入值 |
| `tenant_id` / `merchant_id` | claim 为空时**不覆盖** body 注入值（既有 S-1 已知边界，见 §8） |
| `context.user` | 完整 claims dict |

---

## 4. 认证闭环示例（注册 → 激活 → 登录 → 使用）

> ✅ 匿名闭环已随 §3.3 修复解锁，本节示例已可完整走通。部署阶段 10 还会
> 引导一个超级管理员（凭据见 .env 的 `ADMIN_ACCOUNT`/`ADMIN_PASSWORD`，
> README §部署后验证），可直接登录获得 `platform:super_admin` 角色。

### 4.1 注册（registerUser，匿名）

```bash
curl -sS -X POST http://127.0.0.1:8000/beta/core/banyan/user_engine_graphql \
  -H 'content-type: application/json' -H 'part_id: nestaging' \
  -d '{
    "query": "mutation Register($input: UserRegisterInput!, $idempotencyKey: ID!) { registerUser(input: $input, idempotencyKey: $idempotencyKey) { user { id email username status emailVerified } emailSent message } }",
    "variables": {
      "input": {
        "email": "user@example.com",
        "password": "Str0ng!Pass2026",
        "displayName": "示例用户"
      },
      "idempotencyKey": "01988888-0000-4000-8000-000000000001"
    }
  }'
```

- `UserRegisterInput`：`email`*、`password`*、`username`、`displayName`、`phone`、
  `inviteCode`、`locale`、`timeZone`（`*`=必填；未指定 `username` 时从邮箱本地部
  自动派生，冲突自动加随机后缀）。
- **密码策略**：长度 ≥ 12（`password_min_length` 配置）且至少含大写/小写/数字/符号
  中 3 类。
- 返回 `RegisterResultType`：`user`、`emailSent`、`message`。新用户状态 `PENDING`。
- **幂等**：`idempotencyKey` 必填（所有写操作通用约定）。
- 注册自动发送 6 位激活验证码（存 Redis，TTL 内有效；本地无 SMTP 时发送失败仅
  `emailSent=false`，验证码仍在 Redis 可取）。

### 4.2 激活（verifyEmailCode，匿名）

```bash
curl -sS -X POST http://127.0.0.1:8000/beta/core/banyan/user_engine_graphql \
  -H 'content-type: application/json' -H 'part_id: nestaging' \
  -d '{
    "query": "mutation Verify($input: VerifyEmailCodeInput!, $idempotencyKey: ID!) { verifyEmailCode(input: $input, idempotencyKey: $idempotencyKey) { success verified message } }",
    "variables": {
      "input": { "email": "user@example.com", "code": "123456", "purpose": "EMAIL_VERIFICATION" },
      "idempotencyKey": "01988888-0000-4000-8000-000000000002"
    }
  }'
```

- `purpose` 枚举：`EMAIL_VERIFICATION` / `EMAIL_CHANGE` / `SECURITY_SETTING` /
  `ACCOUNT_RECOVERY` / `EMAIL_OWNERSHIP` / `SENSITIVE_OPERATION` / `PASSWORD_RESET`。
- 验证通过即一次性删除验证码；`EMAIL_VERIFICATION` 用途自动将 `PENDING` 用户
  置为 `ACTIVE` 并标记 `emailVerified=true`。
- 错误形态：过期/已用/不匹配/尝试超限（5 次）分别对应明确 GraphQL 错误。

### 4.3 登录（login，匿名）

```bash
curl -sS -X POST http://127.0.0.1:8000/beta/core/banyan/user_engine_graphql \
  -H 'content-type: application/json' -H 'part_id: nestaging' \
  -d '{
    "query": "mutation Login($input: LoginInput!, $idempotencyKey: ID!) { login(input: $input, idempotencyKey: $idempotencyKey) { authToken renewToken expiresIn requiresMfa forceChangePassword user { id email username status } } }",
    "variables": {
      "input": { "email": "user@example.com", "password": "Str0ng!Pass2026" },
      "idempotencyKey": "01988888-0000-4000-8000-000000000003"
    }
  }'
```

- `LoginInput`：`email`*、`password`*、`captchaToken`、`rememberMe`。
- `LoginResultType`：`authToken`（访问令牌）、`renewToken`（刷新令牌）、
  `expiresIn`、`requiresMfa`*、`mfaType`、`mfaSessionToken`、
  `forceChangePassword`、`user`。MFA 开启时先返回 `requiresMfa=true` +
  `mfaSessionToken`，再走 `loginWithMfa`。
- 连续登录失败超限自动锁定（`LOCKED`，需管理员 `unlockUser` 解锁）。
- 状态机约束：仅 `ACTIVE` 可登录；`PENDING`/`DISABLED`/`LOCKED`/`DEACTIVATED`
  分别抛对应错误。

### 4.4 带令牌调用（已实测）

```bash
curl -sS -X POST http://127.0.0.1:8000/beta/core/banyan/user_engine_graphql \
  -H 'content-type: application/json' -H 'part_id: nestaging' \
  -H "authorization: Bearer ${AUTH_TOKEN}" \
  -d '{"query": "query { me { id email username status } }"}'
# → 200 {"data":{"me":{"id":"...","email":"...","username":"...","status":"ACTIVE"}}}
```

---

## 5. 各引擎操作明细

> 权威源：各引擎 `main.py` 的 `deploy()` 配置（与 `ANONYMOUS_OPS`、前端 action
> 名同源）。精确参数签名/返回类型请用 introspection 获取（§6）。
> 除 §3.2 白名单外，全部操作需带令牌。

### 5.1 agent_engine

| Query | 说明 | Mutation | 说明 |
|---|---|---|---|
| agent | Agent 详情 | createAgent | 创建 Agent |
| agents | Agent 列表 | updateAgent | 更新 Agent |
| searchAgents | 语义搜索 Agent | deleteAgent | 删除 Agent |
| agentVersion | Agent 版本详情 | publishAgentVersion | 发布 Agent 版本 |
| agentVersions | Agent 版本历史 | rollbackAgentVersion | 回滚 Agent 版本 |
| agentSession | 会话详情 | bindKnowledge | 绑定知识库 |
| agentSessions | 会话列表 | unbindKnowledge | 解绑知识库 |
| agentMessages | 消息历史 | bindTool | 绑定工具 |
| agentInvocation | 调用记录 | unbindTool | 解绑工具 |
| agentInvocations | 调用记录列表 | bindSkill | 绑定技能 |
| agentCollaborators | 协作 Agent 列表 | unbindSkill | 解绑技能 |
| agentEvaluation | 评估汇总 | bindMcpServer | 绑定 MCP Server |
| agentGraph | Agent 协作图 | addCollaborator | 添加协作 Agent |
| workflowDetail | Agent 工作流定义 | removeCollaborator | 移除协作 Agent |
| a2aCapabilities | A2A 能力清单 | invokeAgent | 调用 Agent |
| chatHistory | 会话对话历史 | invokeAgentStream | 流式调用 Agent |
| runtimeStatus | Agent 实时运行状态 | createSession | 创建会话 |
| runtimeLogs | Agent 执行日志 | endSession | 结束会话 |
| executionLogDetail | 执行日志详情 | sendMessage | 发送消息 |
| relatedOrchestrations | 关联编排列表 | submitFeedback | 提交反馈 |
| taskStatus | 任务状态摘要 | configureA2a | 配置 A2A 连接 |
| agentTaskHistory | Agent 任务历史 | testA2aConnection | 测试 A2A 连通性 |
| collaborativeTaskStatus | 协同任务状态 | patchAgent | 增量更新 Agent |
| | | activateAgent | 激活 Agent |
| | | deactivateAgent | 停用 Agent |
| | | validateAgentWorkflow | 校验 Agent 工作流 |
| | | buildAgentPrompt | 构建 Agent 提示词 |
| | | submitChatConversionTask | 提交 Chat 转 Workflow 异步分析任务 |
| | | convertWorkflowToChat | 工作流转对话 |
| | | sendTestMessage | 对话测试消息 |
| | | clearChatHistory | 清空对话历史 |
| | | executeAgentTask | 同步执行 Agent 任务 |
| | | executeAgentTaskAsync | 异步执行 Agent 任务 |
| | | cancelTask | 取消任务 |
| | | retryTask | 重试任务 |
| | | createCollaborativeTask | 创建协同任务 |
| | | cancelCollaborativeTask | 取消协同任务 |

### 5.2 capability_engine

| Query | 说明 | Mutation | 说明 |
|---|---|---|---|
| providerDetail | Provider 详情 | createProvider | 创建 Provider |
| providerList | Provider 列表 | updateProvider | 更新 Provider |
| toolDetail | Tool 详情 | deleteProvider | 删除 Provider |
| toolList | Tool 列表 | testProviderConnection | 测试 Provider 连接 |
| providerToolList | Provider 下 Tool 列表 | providerHealthCheck | Provider 健康检查 |
| toolRuntimeLogs | Tool Runtime Logs | downloadProviderPackage | 下载 Provider 源码包 |
| logDetail | 日志详情 | providerPackageUploadUrl | 生成 Provider 源码包上传 URL |
| toolAgentList | Tool 关联 Agent | createMcpServer | 创建 MCP Server |
| skillDetail | Skill 详情 | updateMcpServer | 更新 MCP Server |
| skillList | Skill 列表 | deleteMcpServer | 删除 MCP Server |
| skillMarkdown | Skill Markdown | syncMcpServerTools | 同步 MCP Server 工具 |
| sharedSkillLibrary | 共享 Skill 库 | discoverTools | 发现工具 |
| skillUsageStats | Skill 复用统计 | loadTools | 加载工具 |
| skillDiscoveryIndex | Skill Discovery 索引 | updateToolContext | 更新 Tool 上下文 |
| loadSkillReferences | Skill References | updateToolStatus | 更新 Tool 状态 |
| mcpServerDetail | MCP Server 详情 | batchUpdateToolStatus | 批量更新 Tool 状态 |
| mcpServerList | MCP Server 列表 | createTool | 创建 Tool |
| mcpServerTools | MCP Server 工具 | updateTool | 更新 Tool |
| aguiResourceDetail | AG-UI 资源详情 | deleteTool | 删除 Tool |
| aguiResourceList | AG-UI 资源列表 | batchDeleteTools | 批量删除 Tool |
| componentDetail | 组件详情 | invokeTool | 调用 Tool |
| componentList | 组件列表 | testTool | 测试 Tool |
| clientCapability | 客户端能力 | importToolFromOpenApi | 从 OpenAPI 导入 Tool |
| aguiSessionState | AG-UI 会话状态 | uploadSkill | 上传 Skill 包 |
| aguiEventHistory | AG-UI 事件历史 | updateSkillStatus | 更新 Skill 状态 |
| clientEventSubscription | 客户端事件订阅 | submitSkillReview | 提交 Skill 审核 |
| invocationDetail | 调用记录详情 | reviewSkill | 审核 Skill |
| invocationList | 调用记录列表 | activateSkill | 激活 Skill |
| toolAuthorizationPolicies | 工具授权策略 | executeSkillScripts | 执行 Skill 脚本 |
| toolGraph | 工具图谱 | createSkill | 创建 Skill |
| searchTools | 语义搜索工具 | updateSkill | 更新 Skill |
| skillDependencyGraph | 技能依赖图 | deleteSkill | 删除 Skill |
| searchSkills | 语义搜索技能 | composeSkillFromTools | 从工具组合 Skill |
| recommendSkills | 推荐 Skill | testSkill | 测试 Skill |
| authorizationDecision | 授权决策 | registerComponent | 注册组件 |
| capabilitySettings | 能力模块配置 | updateComponentSchema | 更新组件 Schema |
| dashboardCapabilityKpi | Dashboard KPI | deleteComponent | 下线组件 |
| | | registerClientCapability | 注册客户端能力 |
| | | createAguiSession | 创建 AG-UI 会话 |
| | | closeAguiSession | 关闭 AG-UI 会话 |
| | | pushAguiEvent | 推送 AG-UI 事件 |
| | | configureEventSubscription | 配置事件订阅 |
| | | heartbeatReport | 心跳上报 |
| | | ackAguiEvent | ACK 事件 |
| | | retryAguiEvent | 重投事件 |
| | | createAguiResource | 创建 AG-UI 资源 |
| | | updateAguiResource | 更新 AG-UI 资源 |
| | | deleteAguiResource | 删除 AG-UI 资源 |
| | | testComponent | 测试 AG-UI 组件 |
| | | grantToolAuthorization | 授权工具 |
| | | revokeToolAuthorization | 撤销授权 |
| | | upsertCapabilitySettings | 更新能力管理配置 |

### 5.3 knowledge_engine

| Query | 说明 | Mutation | 说明 |
|---|---|---|---|
| knowledge | 知识条目详情 | createKnowledge | 创建知识条目 |
| knowledges | 知识条目列表 | updateKnowledge | 更新知识条目 |
| searchKnowledges | 语义搜索知识 | deleteKnowledge | 删除知识条目 |
| askKnowledgeBase | GraphRAG 智能问答 | batchDeleteKnowledges | 批量删除知识条目 |
| knowledgeGraph | 知识图谱可视化 | importKnowledge | 导入知识（文件） |
| knowledgeGraphByCypher | Cypher 查询知识图谱 | importKnowledgeFromUrl | 导入知识（URL） |
| knowledgeGraphPath | 知识图谱路径 | refreshKnowledge | 刷新知识 |
| knowledgeGraphSchema | 知识图谱 Schema | createKnowledgeType | 创建知识类型 |
| knowledgeGraphIntrospection | 知识图谱 Schema 内省 | updateKnowledgeType | 更新知识类型 |
| knowledgeGraphAnalysis | 知识图谱分析 | deleteKnowledgeType | 删除知识类型 |
| knowledgeAnalysisReport | 知识分析报告 | saveExtractionRules | 保存提取规则 |
| executeKnowledgeTool | 知识 MCP 工具执行（tools/call 转发端点） | saveCleaningRules | 保存清洗规则 |
| knowledgeTypes | 知识类型列表 | saveVectorConfig | 保存向量配置 |
| knowledgeType | 知识类型详情 | saveGraphSchema | 保存图谱 Schema |
| knowledgeTypesTree | 知识类型树 | createKnowledgeDomain | 创建知识域 |
| knowledgeExtractionRules | 提取规则 | updateKnowledgeDomain | 更新知识域 |
| knowledgeCleaningRules | 清洗规则 | deleteKnowledgeDomain | 删除知识域 |
| knowledgeVectorConfig | 向量配置 | addDomainMember | 添加域成员 |
| knowledgeSources | 来源文档列表 | updateDomainMember | 更新域成员 |
| knowledgeSource | 来源文档详情 | removeDomainMember | 移除域成员 |
| knowledgeDomains | 知识域列表 | shareKnowledgeToDomain | 分享知识到域 |
| knowledgeDomain | 知识域详情 | copyKnowledgeToDomain | 复制知识到域 |
| knowledgeDomainMembers | 域成员列表 | submitPublishRequest | 提交发布申请 |
| knowledgeDomainStats | 域统计 | approvePublishRequest | 审批通过发布申请 |
| knowledgeVersions | 版本历史 | rejectPublishRequest | 驳回发布申请 |
| knowledgeVersionDiff | 版本对比 | bindKnowledgeToAgents | 绑定知识到 Agent |
| knowledgeAgents | 关联 Agent | unbindKnowledgeFromAgent | 解除知识与 Agent 绑定 |
| knowledgePublishRequests | 发布申请列表 | rollbackKnowledgeVersion | 回滚知识版本 |
| knowledgeRemoteApiConfigs | Remote API 配置列表 | exportKnowledge | 导出知识 |
| knowledgeRemoteApiConfig | Remote API 配置详情 | updateKnowledgeSource | 编辑知识来源文档元信息 |
| knowledgeModuleConfig | 模块级全局配置 | deleteKnowledgeSource | 删除知识来源文档 |
| | | createRemoteApiConfig | 创建 Remote API 配置 |
| | | updateRemoteApiConfig | 更新 Remote API 配置 |
| | | deleteRemoteApiConfig | 删除 Remote API 配置 |
| | | testRemoteApiConnection | 测试 Remote API 连接 |
| | | triggerRemoteApiFetch | 触发 Remote API 拉取 |
| | | saveKnowledgeModuleConfig | 保存模块级全局配置 |
| | | previewConfigMerge | 预览配置合并 |
| | | validateKnowledge | 知识质量校验 |
| | | scanPii | PII 敏感信息扫描 |
| | | remaskKnowledgePii | PII 存量脱敏（回溯掩码 + 重建索引） |
| | | registerMcpTools | 知识能力注册为 MCP 工具 |

### 5.4 llm_engine

| Query | 说明 | Mutation | 说明 |
|---|---|---|---|
| modelDetail | 模型详情 | createModel | 创建模型 |
| models | 模型列表 | updateModel | 更新模型 |
| modelTraceConnection | 模型调用链路 | deleteModel | 删除模型 |
| providerDetail | Provider 详情 | testModelConnection | 测试模型连接 |
| providers | Provider 列表 | fetchAvailableModels | 获取上游可用模型列表 |
| healthCheckHistory | 健康检查历史 | activateModel | 激活模型 |
| agentBindingConnection | Agent 绑定列表 | deactivateModel | 停用模型 |
| dashboardLlmKpi | Dashboard KPI | createProvider | 创建 Provider |
| modelUsage | 模型用量统计 | updateProvider | 更新 Provider |
| traceDetail | 调用链路详情 | bindAgents | 绑定 Agent |
| exportModelTraces | 导出调用日志 | unbindAgent | 解除 Agent 绑定 |
| protocolAdapters | 协议适配器列表 | batchUnbindAgents | 批量解除 Agent 绑定 |
| | | invokeLlm | 调用大语言模型 |
| | | submitToolCall | 提交 Tool 调用记录 |
| | | triggerHealthCheck | 触发健康检查 |
| | | setModelQuota | 设置模型配额 |

### 5.5 memory_engine

| Query | 说明 | Mutation | 说明 |
|---|---|---|---|
| memory | 记忆条目详情 | createMemory | 创建记忆条目 |
| memories | 记忆条目列表 | updateMemory | 更新记忆条目 |
| shortTermMemories | 短期记忆列表 | deleteMemory | 删除记忆条目 |
| longTermMemories | 长期记忆列表 | batchDeleteMemories | 批量删除记忆条目 |
| memorySessions | 记忆会话列表 | extractMemories | 从对话提取记忆 |
| memoryType | 记忆类型详情 | consolidateMemories | 记忆整合 |
| memoryTypes | 记忆类型列表 | archiveMemory | 归档记忆 |
| memoryExtractionRules | 提取规则 | restoreMemory | 恢复归档记忆 |
| memoryImportanceRules | 重要性规则 | linkMemories | 关联记忆 |
| searchMemories | 语义搜索记忆 | unlinkMemories | 解除记忆关联 |
| memoryGraph | 记忆关联图 | createMemorySession | 创建记忆会话 |
| memoryConsolidationHistory | 记忆整合历史 | endMemorySession | 结束记忆会话 |
| memoryConflicts | 记忆冲突列表 | createMemoryType | 创建记忆类型 |
| memoryConflict | 记忆冲突详情 | updateMemoryType | 更新记忆类型 |
| memoryDomainList | 共享域列表 | deleteMemoryType | 删除记忆类型 |
| memoryDomainDetail | 共享域详情 | saveExtractionRules | 保存提取规则 |
| memoryDomainStats | 共享域统计 | saveImportanceRules | 保存重要性规则 |
| memoryDomainMembers | 域成员列表 | summarizeMemory | 摘要记忆 |
| memoryQuota | 记忆配额 | searchMemoryByVector | 向量搜索记忆 |
| memoryQuotaRecords | 用户配额记录列表 | resolveConflict | 解决冲突 |
| memoryLifecyclePolicy | 生命周期策略 | toggleMemoryLock | 锁定/解锁记忆 |
| memoryMaskingRules | 脱敏规则 | createMemoryDomain | 创建共享域 |
| memoryMaskingTemplates | 脱敏模板 | updateMemoryDomain | 编辑共享域 |
| memoryAuditLogs | 审计日志 | deleteMemoryDomain | 删除共享域 |
| memoryMergeSuggestions | 合并建议 | addMemoryDomainMember | 添加域成员 |
| publicMemoryList | 公共记忆列表 | removeMemoryDomainMember | 移除域成员 |
| memoryKnowledgeLinks | 记忆知识链接 | updateMemoryDomainMemberRole | 修改成员角色 |
| agentMemoryStats | Agent 记忆统计 | shareMemoryToDomain | 共享记忆到域 |
| | | manageMemoryQuota | 调整用户配额 |
| | | saveMemoryLifecyclePolicy | 保存生命周期策略 |
| | | saveMemoryMaskingRules | 保存脱敏规则 |
| | | saveMemoryMaskingTemplate | 保存脱敏模板 |
| | | applyMemoryMaskingTemplate | 应用脱敏模板 |
| | | executeMemoryMerge | 执行合并操作 |
| | | rejectMemoryMerge | 拒绝合并建议 |
| | | mergeMemories | 合并记忆 |
| | | exportMemory | 导出记忆数据 |
| | | publishMemoryToPublic | 提交发布申请 |
| | | approveMemoryPublish | 审批发布申请 |
| | | rejectMemoryPublish | 拒绝发布申请 |
| | | convertMemoryToKnowledge | 记忆转知识 |
| | | convertMemoryType | 转换记忆类型 |
| | | agentReadMemory | Agent 读取记忆 |
| | | agentWriteMemory | Agent 写入记忆 |

### 5.6 merchant_engine

| Query | 说明 | Mutation | 说明 |
|---|---|---|---|
| merchantList | 商户列表 | createMerchant | 创建商户 |
| merchantDetail | 商户详情 | updateMerchant | 更新商户 |
| merchantChannels | 商户渠道列表 | deleteMerchant | 删除商户 |
| merchantQuota | 商户配额 | approveMerchant | 审核商户 |
| merchantLogs | 商户操作日志 | updateMerchantStatus | 更新商户状态 |
| channelList | 渠道列表 | transferMerchantOwnership | 转移商户所有权 |
| channelDetail | 渠道详情 | createChannel | 创建渠道 |
| channelLogs | 渠道操作日志 | updateChannel | 更新渠道 |
| themeList | 主题列表 | deleteChannel | 删除渠道 |
| themeDetail | 主题详情 | publishChannel | 发布渠道 |
| exportTheme | 导出主题配置 | suspendChannel | 停用渠道 |
| quota | 配额详情 | resumeChannel | 启用渠道 |
| merchantUsers | 商户用户列表 | archiveChannel | 归档渠道 |
| myMerchants | 当前用户所属商户列表 | regenerateChannelSecret | 重新生成渠道密钥 |
| | | createTheme | 创建主题 |
| | | updateTheme | 更新主题 |
| | | deleteTheme | 删除主题 |
| | | previewTheme | 预览主题 |
| | | importTheme | 导入主题配置 |
| | | createMerchantQuota | 创建商户配额 |
| | | updateMerchantQuota | 更新商户配额 |
| | | signup | 商户注册 |
| | | switchMerchant | 切换当前商户 |

### 5.7 monitor_engine

| Query | 说明 | Mutation | 说明 |
|---|---|---|---|
| metricDefinition | 指标定义详情 | createMetricDefinition | 创建指标定义 |
| metricDefinitions | 指标定义列表 | updateMetricDefinition | 更新指标定义 |
| metricSeries | 时序数据查询 | deleteMetricDefinition | 删除指标定义 |
| metricInstant | 即时指标值 | createAlertRule | 创建告警规则 |
| alertRule | 告警规则详情 | updateAlertRule | 更新告警规则 |
| alertRules | 告警规则列表 | deleteAlertRule | 删除告警规则 |
| alertRecord | 告警记录详情 | acknowledgeAlert | 确认告警 |
| alertRecords | 告警记录列表 | resolveAlert | 解决告警 |
| activeAlerts | 当前激活告警 | evaluateAlertRulesNow | 手动评估告警规则 |
| silencer | 静默规则详情 | silenceAlert | 创建静默规则 |
| silencers | 静默规则列表 | createDashboard | 创建仪表盘 |
| dashboard | 仪表盘详情 | updateDashboard | 更新仪表盘 |
| dashboards | 仪表盘列表 | deleteDashboard | 删除仪表盘 |
| slo | SLO 详情 | createSlo | 创建 SLO |
| slos | SLO 列表 | updateSlo | 更新 SLO |
| sloBurnRate | SLO 燃烧率 | deleteSlo | 删除 SLO |
| subscription | 订阅详情 | createSubscription | 创建订阅 |
| subscriptions | 订阅列表 | updateSubscription | 更新订阅 |
| logSearch | 日志查询 | deleteSubscription | 删除订阅 |
| traceDetail | 链路追踪 | testAlertChannel | 测试告警通道 |
| topErrors | Top 错误 | createPostmortem | 创建事后复盘 |
| serviceMap | 服务地图 | updatePostmortem | 更新事后复盘 |
| healthScore | 健康评分 | | |
| auditLogs | 审计日志 | | |
| capacityForecast | 容量预测 | | |
| anomalyDetectionResults | 异常检测结果 | | |
| rootCauseAnalysis | 根因分析 | | |
| postmortem | 事后复盘 | | |

### 5.8 orchestration_engine

| Query | 说明 | Mutation | 说明 |
|---|---|---|---|
| workflow | 编排详情 | createWorkflow | 创建编排 |
| workflows | 编排列表 | updateWorkflow | 更新编排 |
| workflowVersion | 编排版本详情 | deleteWorkflow | 删除编排 |
| node | 编排节点详情 | publishWorkflowVersion | 发布编排版本 |
| nodeExecutions | 节点执行记录 | rollbackWorkflowVersion | 回滚编排版本 |
| execution | 执行实例详情 | createNode | 创建编排节点 |
| executions | 执行实例列表 | updateNode | 更新编排节点 |
| a2aMessages | A2A 消息列表 | deleteNode | 删除编排节点 |
| schedules | 定时触发列表 | connectNodes | 连接编排节点 |
| workflowGraph | 工作流图 | disconnectNodes | 断开编排节点连接 |
| executionTrace | 执行链路追踪 | executeWorkflow | 执行编排 |
| workflowCode | 编排代码预览 | resumeExecution | 恢复执行 |
| workflowAgents | 编排 Agent 列表 | cancelExecution | 取消执行 |
| a2aCapabilities | A2A 能力列表 | retryNodeExecution | 重试节点执行 |
| agentWorkflow | Agent 内部工作流 | sendA2aMessage | 发送 A2A 消息 |
| previewLink | 预览链接 | replyA2aMessage | 回复 A2A 消息 |
| | | createSchedule | 创建定时触发 |
| | | updateSchedule | 更新定时触发 |
| | | deleteSchedule | 删除定时触发 |
| | | pauseSchedule | 暂停定时触发 |
| | | resumeSchedule | 恢复定时触发 |
| | | duplicateWorkflow | 复制编排 |
| | | activateWorkflow | 激活编排 |
| | | deactivateWorkflow | 停用编排 |
| | | updateWorkflowGraph | 更新工作流图 |
| | | validateWorkflow | 校验工作流 |
| | | convertChatToWorkflow | 对话转工作流 |
| | | convertWorkflowToChat | 工作流转对话 |
| | | updateWorkflowAgent | 更新编排 Agent |
| | | updateAgentWorkflow | 更新 Agent 工作流 |
| | | executePreview | 执行预览 |
| | | buildOrchestrationPrompt | 构建编排提示词 |

### 5.9 perm_engine

| Query | 说明 | Mutation | 说明 |
|---|---|---|---|
| userEffectivePermissions | 用户有效权限 | assignUserRole | 分配用户角色 |
| userRoles | 用户角色代码列表 | revokeUserRole | 撤销用户角色 |
| userDecisionPath | 决策路径追溯 | grantUserPermission | 授予用户权限 |
| myPermissions | 当前用户权限集 | revokeUserPermission | 撤销用户权限 |
| role | 角色详情 | createRole | 创建角色 |
| roles | 角色列表 | updateRole | 更新角色 |
| roleInheritanceTree | 角色继承树 | deleteRole | 删除角色 |
| roleVersions | 角色版本历史 | copyRole | 复制角色 |
| userGroup | 用户组详情 | grantRolePermission | 授予角色权限 |
| userGroups | 用户组列表 | revokeRolePermission | 撤销角色权限 |
| userGroupTree | 用户组嵌套树 | createRoleVersion | 创建角色版本 |
| userGroupMembers | 用户组成员列表 | rollbackRoleVersion | 回滚角色版本 |
| userGroupRoles | 用户组角色绑定列表 | createUserGroup | 创建用户组 |
| resource | 资源详情 | updateUserGroup | 更新用户组 |
| resources | 资源列表 | deleteUserGroup | 删除用户组 |
| permissionMatrix | 权限矩阵 | addGroupMember | 添加组成员 |
| matrixDiff | 矩阵差异 | removeGroupMember | 移除组成员 |
| policy | 策略详情 | assignGroupRole | 分配组角色 |
| policies | 策略列表 | revokeGroupRole | 撤销组角色 |
| policyVersions | 策略版本列表 | createResource | 创建资源 |
| permissionRequest | 申请单详情 | updateResource | 更新资源 |
| permissionRequests | 申请单列表 | deleteResource | 删除资源 |
| diagnose | 权限诊断 | batchUpdateMatrix | 批量更新矩阵 |
| whyDeny | 拒绝原因 | importResources | 导入资源 |
| ssoProvider | SSO 提供商详情 | exportResources | 导出资源 |
| ssoProviders | SSO 提供商列表 | registerResources | 资源注册 |
| mfaFactors | MFA 因子列表 | createPolicy | 创建策略 |
| auditChanges | 权限变更审计 | updatePolicy | 更新策略 |
| auditDecisions | 权限决策审计 | deletePolicy | 删除策略 |
| elevations | 临时提权列表 | publishPolicy | 发布策略 |
| permissionResourcesExported | 资源导出 | disablePolicy | 禁用策略 |
| configs | 系统配置列表 | enablePolicy | 启用策略 |
| ipWhitelists | IP 白名单列表 | rollbackPolicyVersion | 回滚策略版本 |
| userWhitelists | 用户白名单列表 | simulatePolicy | 策略模拟 |
| policyHitStats | 策略命中统计 | validatePolicy | 策略校验 |
| mfaBackupCodes | MFA 备用码列表 | createPermissionRequest | 创建权限申请 |
| mfaBackupCodesHistory | MFA 备用码历史 | cancelPermissionRequest | 撤回申请 |
| resourceRegistrationStats | 资源注册统计 | approveRequest | 审批通过 |
| | | rejectRequest | 审批拒绝 |
| | | delegateRequest | 转交审批 |
| | | createSsoProvider | 创建 SSO 提供商 |
| | | updateSsoProvider | 更新 SSO 提供商 |
| | | deleteSsoProvider | 删除 SSO 提供商 |
| | | testSsoConnection | 测试 SSO 连接 |
| | | enrollMfaFactor | 注册 MFA 因子 |
| | | removeMfaFactor | 移除 MFA 因子 |
| | | createTemporaryElevation | 创建临时提权 |
| | | revokeTemporaryElevation | 撤销临时提权 |
| | | gdprEraseUser | GDPR 抹除 |
| | | completeGdprErase | 完成 GDPR 抹除 |
| | | gdprExportUserData | GDPR 导出 |
| | | updateConfig | 更新配置 |
| | | addIpWhitelist | 添加 IP 白名单 |
| | | removeIpWhitelist | 移除 IP 白名单 |
| | | addUserWhitelist | 添加用户白名单 |
| | | removeUserWhitelist | 移除用户白名单 |
| | | batchDiagnose | 批量诊断 |
| | | generateMfaBackupCodes | 生成 MFA 备用码 |
| | | generateComplianceReport | 生成合规报告 |
| | | rollbackConfig | 回滚配置 |

### 5.10 prompt_engine

| Query | 说明 | Mutation | 说明 |
|---|---|---|---|
| tokenTelemetry | Token 遥测列表 | recordTokenTelemetry | 上报 Token 遥测 |
| countTokens | Token 计数 | enhancePrompt | 增强提示词 |
| tokenUsageStats | Token 用量统计 | compressPrompt | 压缩提示词 |
| analyzePrompt | 提示词分析 | | |
| optimizationRecords | 优化记录 | | |

### 5.11 setting_engine

| Query | 说明 | Mutation | 说明 |
|---|---|---|---|
| platformSetting | 平台设置 | updatePlatformSetting | 更新平台设置 |
| tenantSetting | 租户设置 | updateTenantSetting | 更新租户设置 |
| navigations | 导航树 | createNavigation | 创建导航项 |
| theme | 主题配置 | updateNavigation | 更新导航项 |
| i18nResources | 多语言资源 | deleteNavigation | 删除导航项 |
| emailTemplate | 邮件模板 | reorderNavigations | 导航排序变更 |
| smsTemplate | SMS 模板 | updateTheme | 更新主题 |
| webhooks | Webhook 列表 | updateBranding | 更新品牌定制 |
| branding | 品牌定制 | createEmailTemplate | 创建邮件模板 |
| | | updateEmailTemplate | 更新邮件模板 |
| | | deleteEmailTemplate | 删除邮件模板 |
| | | createSmsTemplate | 创建 SMS 模板 |
| | | updateSmsTemplate | 更新 SMS 模板 |
| | | deleteSmsTemplate | 删除 SMS 模板 |
| | | createWebhook | 创建 Webhook |
| | | updateWebhook | 更新 Webhook |
| | | deleteWebhook | 删除 Webhook |
| | | testWebhook | 测试 Webhook |
| | | updateI18nResource | 更新多语言资源 |

### 5.12 user_engine

| Query | 说明 | Mutation | 说明 |
|---|---|---|---|
| me | 当前登录用户（匿名时返回 null） | createUser | 管理员创建用户 |
| user | 用户详情 | registerUser | 注册用户（匿名） |
| users | 用户列表 | updateUserProfile | 更新个人信息 |
| userMfaFactors | 用户 MFA 因子 | avatarUploadUrl | 获取头像预签名上传 URL |
| userSessions | 用户会话列表 | updateUserByAdmin | 管理员更新用户 |
| userLoginHistory | 用户登录历史 | deleteUser | 删除用户 |
| userConsents | 用户同意列表 | batchDeleteUsers | 批量删除用户 |
| userSsoIdentities | 用户 SSO 身份 | changePassword | 修改密码 |
| checkPasswordStrength | 密码强度评估（匿名） | adminResetPassword | 管理员重置密码 |
| userLogs | 用户操作日志 | requestPasswordReset | 请求密码重置（匿名） |
| authStatus | 当前认证状态（匿名） | confirmPasswordReset | 确认密码重置（匿名） |
| authDevices | 可信设备列表 | enableMfa | 启用 MFA |
| | | verifyMfa | 验证 MFA |
| | | disableMfa | 禁用 MFA |
| | | beginWebAuthnRegistration | 开始 WebAuthn 注册 |
| | | completeWebAuthnRegistration | 完成 WebAuthn 注册 |
| | | beginWebAuthnAuthentication | 开始 WebAuthn 认证 |
| | | completeWebAuthnAuthentication | 完成 WebAuthn 认证 |
| | | logout | 退出登录 |
| | | logoutAll | 退出所有会话 |
| | | login | 用户登录（匿名） |
| | | loginWithMfa | MFA 登录（匿名） |
| | | loginWithSso | SSO 登录（匿名） |
| | | refreshToken | 续签 Token（匿名） |
| | | linkSsoIdentity | 关联 SSO 身份 |
| | | unlinkSsoIdentity | 解除 SSO 身份 |
| | | grantConsent | 授予同意 |
| | | revokeConsent | 撤销同意 |
| | | exportUserData | 导出用户数据 |
| | | deleteUserData | 删除用户数据 |
| | | enableUser | 启用用户 |
| | | disableUser | 禁用用户 |
| | | unlockUser | 解锁用户 |
| | | resendWelcome | 重发欢迎邮件 |
| | | resendActivationEmail | 重发账户激活邮件（匿名） |
| | | forceLogout | 强制用户下线 |
| | | verifyResetCode | 验证密码重置码（匿名） |
| | | deleteAuthDevice | 删除可信设备 |
| | | sendEmailVerificationCode | 发送邮箱验证码（匿名） |
| | | verifyEmailCode | 验证邮箱验证码（匿名） |
| | | purgeExpiredOperationalRecords | 清理过期运维记录 |

---

## 6. Introspection：获取精确 Schema

各引擎 schema 支持 GraphQL 标准 introspection（需带令牌）：

```bash
# 某操作的精确参数签名（以 registerUser 为例）
curl -sS -X POST http://127.0.0.1:8000/beta/core/banyan/user_engine_graphql \
  -H 'content-type: application/json' -H 'part_id: nestaging' \
  -H "authorization: Bearer ${AUTH_TOKEN}" \
  -d '{"query": "{ __schema { mutationType { fields { name args { name type { kind name ofType { kind name ofType { kind name } } } } } } } }"}'
```

> 本文档不逐一罗列 644 个操作的完整参数签名（篇幅与漂移成本考虑）；
> 集成时以 introspection 实时结果为准，`deploy()` 清单（§5）为操作名权威源。

---

## 7. 健康检查与运维

| 检查 | 方法 | 期望 |
|---|---|---|
| 网关存活 | `GET /health` | `200 {"status":"ok","service":"silvaengine-gateway"}`（无鉴权） |
| 鉴权链生效 | 无令牌 POST 任意引擎端点（非 §3.2 白名单操作） | `401 {"detail":"Authentication required"}` |
| 匿名白名单 | 无令牌 `login`（超管凭据） | `200 + authToken`（阶段 10 引导的超管） |
| 数据面健康 | `bash deploy.sh status` | 五容器 healthy + 两条 REQUIRED 日志 |
| 限流 | 超 100 次/60s | `429 {"detail":"Rate limit exceeded"}` |

> 注意：网关另有 `GET /me`（FastAPI 依赖读取**网关 FlexJWT** claims），与 Banyan
> JWT 体系无关；Banyan 用户信息查询请使用 `user_engine` 的 `me` GraphQL 操作。

---

## 8. 限制与已知边界

| 级别 | 事项 | 说明 |
|---|---|---|
| ~~P0~~ | ~~匿名白名单操作被路由级依赖拦截~~ | **已修复（§3.3）**：12 个 Banyan 引擎 yaml `auth: true→false`，鉴权权威收敛到 fail-closed 鉴权桥；网关仓 `test_banyan_compat.py` 新增 4 个回归锁用例（42 用例全绿） |
| P0 | `x-api-key` 透传不校验 | Lambda 链由云端 usage plan 强制；容器形态仅透传引擎（网关只记录其存在性，不记录值） |
| S-1 | `tenant_id`/`merchant_id` claim 为空时 body 注入值存活 | 平台级既有问题（Lambda 链同病）。引擎 resolver 读取的租户标识在 claim 为空时可被请求体 context 注入影响；已单独立项，集成时勿依赖 body 传入租户标识 |
| 边界 | 无 WebSocket/SSE 传输路由 | 12 引擎仅暴露 graphql handler；流式能力（`invokeAgentStream` 等）以 mutation 形态实现，无独立 SSE/WS 端点（网关的 sse/websocket handler 类型保留给其它模块） |
| 边界 | 单进程 MVP | WebSocket ConnectionManager 等按单进程设计；多 worker 部署不支持 |
| 边界 | OpenAPI 无动态路由 Schema | 动态路由注册用 `response_model=None`（FastAPI 兼容性取舍），`/docs` 不反映 12 引擎端点；以本文档 + introspection 为准 |
| 边界 | 引擎 schema 为启动时静态构建 | 12 引擎运行时不消费 `se-graphql-schemas` 表（该种子为惰性完备件，供未来动态 schema 能力） |
| 已知日志 | 首启迁移回填告警 | `[D3 backfill] setting 租户回填失败: relation "tenant_user" does not exist` —— 初始化顺序性告警，业务级非阻断 |

---

## 9. 附录：验证记录（2026-09-28 修复后复测，podman 五容器本地栈）

| # | 探针 | 结果 |
|---|---|---|
| 1 | `GET /health` | 200 `{"status":"ok","service":"silvaengine-gateway"}` |
| 2 | 无令牌 `POST /beta/core/banyan/user_engine_graphql`（`__typename`） | 401 `{"detail":"Authentication required"}`（鉴权桥 fail-closed） |
| 3 | 无令牌（网关原生路径 `/banyan/...`） | 401 同上（两种路径形态等价） |
| 4 | 无令牌 `registerUser`（白名单操作） | **200**（P0 已修复，修复前 401 `Not authenticated`，见 §3.3） |
| 5 | 无令牌 `login`（阶段 10 超管凭据） | **200 + `authToken`/`renewToken`/`expiresIn`**（登录闭环解锁） |
| 6 | 带 token `me` 查询 | 200 `{"data":{"me":{..."status":"ACTIVE","roles":["platform:super_admin"]}}}` |
| 7 | 错误签名令牌 | 401 `{"detail":"Invalid token: Invalid crypto padding"}` |
| 8 | 错误凭据 `login` | 200 GraphQL 应用级错误 `用户名或密码错误`（`data.login=null`） |
| 9 | 缺 `part_id` 头（带令牌） | 400 `{"detail":"Part-Id header is required to construct partition_key"}` |
| 10 | GraphQL 校验错误（不存在字段） | 200 `{"errors":[{"message":"Cannot query field ..."}]}` |
| 11 | 缺 `query` 键 | 200 `{"errors":"GraphQL query 不能为空。"}` |

> 令牌来源说明：探针 6-10 使用阶段 10 引导的超管经真实 `login` 获取的
> JWT（此前 PG 无用户，修复前只能以种子 secret 手工签发测试 JWT）；探针 5/6
> 令牌验证了完整链路：admin-init 建户+角色绑定 → 登录签发 → 鉴权桥验签、
> 查角色、注入 claims → 引擎 resolver 读取。探针 4 注册的探针用户已从 PG
> 清理（tenant_user 仅剩超管一行）。幂等复验：admin-init 重跑输出
> `already-active` / `already present`，密码未被重置。