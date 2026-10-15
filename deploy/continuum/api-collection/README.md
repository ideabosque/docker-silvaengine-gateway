# Continuum API 集合（reqable/postman 通用）

## 文件清单

| 文件 | 用途 |
|---|---|
| `continuum.postman_collection.json` | Continuum 后端 8 模块 API 集合，Postman v2.1 格式；Reqable 与 Postman 均可直接导入 |
| `README.md` | 本说明 |

## 导入方式（Reqable）

1. 启动 Reqable → 菜单「文件」→「导入」；
2. 选择格式 **Postman v2**，选取本目录 `continuum.postman_collection.json`；
3. 导入后在 API 调试侧边栏出现集合「Continuum」，含 10 个文件夹（00 Health & Auth + 8 引擎模块）。

## 集合变量（导入后填写）

| 变量 | 说明 |
|---|---|
| `base_url` | 默认 `http://127.0.0.1:8766`（Podman 网关容器宿主端口） |
| `part_id` | 默认 `gpt`（Partition 标识，业务路由必带） |
| `access_token` | 从「00 Health & Auth / 获取访问令牌」响应中复制；**默认 15 分钟过期**，过期后重跑该请求 |
| `username` / `password` | 本地网关登录凭据，**留空不入仓**；本地默认值见 `docker-silvaengine-gateway/.env`（容器内注入），勿提交 |

## 鉴权约定（网关 FlexJWT 中间件）

- 公开路径：`GET /health`、`POST /auth/token`（form-urlencoded）。
- 业务路径：请求头必带 `Authorization: Bearer {{access_token}}` 与 `Part-Id: {{part_id}}`；manifest 置 `auth: false` 也不能旁路中间件。

## 集合内容（v1 仅读操作）

| 文件夹 | 端点 | 请求 |
|---|---|---|
| 00 Health & Auth | /health、/auth/token | 健康检查、获取访问令牌 |
| 01 dashboard_engine | /gpt/graphql | schema 自省 + 仪表盘四页（pm/sales/exec/claims） |
| 02 analytics_engine | /gpt/analytics_graphql | schema 自省（9 query / 10 mutation） |
| 03 campaign_engine | /gpt/campaign_graphql | schema 自省（3 / 9） |
| 04 content_engine | /gpt/content_graphql | schema 自省（6 / 8） |
| 05 flow_engine | /gpt/flow_graphql | schema 自省（13 / 25） |
| 06 message_engine | /gpt/message_graphql | schema 自省（6 / 12） |
| 07 platform_engine | /gpt/platform_graphql | schema 自省（18 / 42） |
| 08 userops_engine | /gpt/userops_graphql | schema 自省（2 / 13） |

> mutation 写操作（写容器 SQLite）v1 未收录；如需扩展，按各引擎 introspection 清单补充并禁用执行或加保护。

## 验证快照（2026-10-14，网关容器 silvaengine-gateway-local）

- `GET /health` → 200 ok；token（urlencoded）→ 200，len=144；
- dashboard 四页 `run=101426`，顶层键数 pm=25 / sales=20 / exec=28 / claims=20；
- 8 引擎 introspection 全部 200，query/mutation 字段数与盘点清单一致；
- 合计 14 请求：13 PASS + 1 设计置空（token 占位凭据，填变量后即可 PASS）。