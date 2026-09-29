# vendor/ — 内嵌框架支撑包（勿以 clone 上游替代）

本目录内嵌三份 Banyan 网关运行所必需的框架支撑包，随交付仓一并分发。
部署脚本（`deploy.sh` 阶段 2）**不会** clone 这三个包，而是直接使用本目录内容。

## 包清单

| 包 | 作用 |
|---|---|
| `silvaengine_constants` | 框架级常量定义 |
| `silvaengine_definitions` | 框架级 GraphQL 定义与数据结构 |
| `silvaengine_dynamodb_base` | DynamoDB 数据访问基座（含 se-* 配置表 TTL 缓存增强） |

## 来源与补丁说明

三包均快照自 api-runtime 交付仓（`docker/api-runtime/vendor/`），其中：

- `silvaengine_constants` / `silvaengine_definitions`：与上游仓库
  `ideabosque/silvaengine_constants` / `ideabosque/silvaengine_definitions`
  逐字一致（上游仅这两包可作为对照参考）。
- `silvaengine_dynamodb_base`：**与上游 `ideabosque/silvaengine_dynamodb_base`
  存在实质差异**。本快照在 upstream 基础上叠加了 se-* 配置表 TTL 缓存增强
  （`hybrid_cache` 装饰器与 `SILVAENGINE_CACHE_TTL` / `SILVAENGINE_CACHE_ENABLED`
  环境变量控制），为 Banyan 网关生产链路所必需。

因此，**不得**用 `git clone` 上游仓库来替代本目录内容；升级时必须先确认上游
已包含（或以补丁形式重新应用）上述增强，再人工同步并重新验证。