# Tenant 多租户配置管理设计与实现

## 1. 目标与边界

系统以 `tenant_id` 作为配置、任务、日志和前端查询的强制隔离边界。系统提供一个
管理员账户，并支持直接绑定 `tenant_id` 的普通账号，不引入通用角色权限系统。
普通 CDN 页面只展示账号绑定租户的数据，不存在跨租户的默认“全部”选项。

`config.json` 降级为首次导入和旧 CLI 兼容入口。后台编辑、草稿、发布和回滚全部写入
SQLite 配置数据库，不能再直接覆盖配置文件。

## 2. 核心流程

```text
[管理员登录]
      ↓
[Tenant 列表] → [创建/选择 Tenant]
      ↓
[编辑草稿] → [校验] → [发布不可变版本] → [CLI 读取生效版本]
                                      ↓
                  [generation_job + 租户独立输出目录]
                                      ↓
              [带 tenant/version/job 的 CDN 日志]
                                      ↓
              [/tenants/{tenant_id}/... 页面查询]
```

回滚不修改历史记录，而是复制目标历史版本，创建并发布一个新版本。这样每次变更都有
完整的版本链与审计记录。

## 3. 数据模型

配置数据库默认位于 `output/config.db`，日志数据库仍为 `output/cdn_logs.db`。

- `tenants`
  - `tenant_id`：大小写敏感的业务主键，创建后不可修改。
  - `active_version_id`：当前已发布版本。
  - `latest_draft_id`：最近草稿。
  - `revision`：乐观锁版本，阻止并发覆盖。
- `config_versions`
  - 配置 JSON、校验和、版本号、状态、来源版本、创建与发布信息。
  - 版本内容不可变；状态仅在发布时从草稿变为发布，旧发布版本转为历史。
- `config_audit`
  - 记录租户创建、草稿保存、发布、回滚和任务创建。
- `generation_jobs`
  - 记录任务使用的租户、配置版本、模式、输出目录、状态和统计。
- `cdn_logs`
  - 原有 `tenant_id`、`project` 保留。
  - 新增 `config_version_id`、`generation_job_id`。
  - 新增租户+时间、租户+域名+时间、租户+项目+时间和租户+任务复合索引。

`project` 是租户内部的业务维度，不能替代 `tenant_id`。

## 4. 后台信息架构

- `/config`：Tenant 列表、创建租户、进入配置或数据页。
- `/config/tenants/{tenant_id}`：六步编辑器、当前版本身份、版本回滚。
- `/config/tenants/{tenant_id}/audit`：单租户审计。
- `/config-audit`：管理员全局审计。
- `/tenants/{tenant_id}/overview`：租户概览。
- `/tenants/{tenant_id}/analytics`：租户流量分析。
- `/tenants/{tenant_id}/report`：租户月报。
- 其他 CDN 功能页面沿用同一租户 URL 前缀。

顶栏持续展示 Tenant 选择器；侧边栏的 CDN 链接会保留当前租户。租户切换通过 URL
完成，页面刷新、收藏和分享都能保持明确的数据上下文。

## 5. 后端查询约束

Dashboard 共用 SQL 构造器在缺少 `tenant_id` 时直接拒绝查询。所有概览、分析、域名、
报告、时间范围和活动查询都先使用：

```sql
WHERE tenant_id = ?
```

再叠加时间、域名和项目条件。租户选择只能收窄项目或域名，不能移除租户边界。

## 6. 生成链路

推荐命令：

```bash
python -m fake_cdn simulation --tenant-id hccl --dry-run
python -m fake_cdn realtime --tenant-id hccl --once
python -m fake_cdn catchup --tenant-id hccl --start-date 2026-03-01 --end-date 2026-03-31
```

CLI 只读取 Tenant 的已发布版本，并生成任务记录。运行时配置补充：

- `config_version_id`
- `config_checksum`
- `generation_job_id`

调度计划 ID 包含租户、配置版本和校验和，避免不同租户或不同配置复用同一计划。
统计、曲线、计划和状态等任务产物目录固定为：

```text
output/tenants/{tenant_id}/jobs/{job_id}/
```

日志统一写入中央 `output/cdn_logs.db`（可由 `FAKE_CDN_DB_PATH` 覆盖），Dashboard
无需扫描任务目录即可查询新数据；日志内的租户和任务字段负责隔离与追溯。

## 7. 迁移策略

执行：

```bash
python -m fake_cdn tenant-migrate --config ./config.json
```

命令扫描同目录下的 `config*.json`：

1. 校验并按文件中的精确 `tenant_id` 导入首个发布版本。
2. 已存在的同名租户跳过，不覆盖数据库配置。
3. 对日志数据库中的租户做差集检查，打印“待映射”列表。
4. 不把 `hccl`、`LITTLEHCCL` 等近似名称自动合并。

旧命令未传 `--tenant-id` 时仍能读取 `config.json`，但会显示兼容模式提示；此路径不具备
数据库版本和任务追溯能力，建议仅用于迁移期。

## 8. 安全与验收

- 配置、审计、Dash 布局、依赖和回调接口均受单管理员登录保护。
- 普通账号通过 `DASHBOARD_TENANT_USERS` 配置密码和固定 `tenant_id`，登录后不显示
  Tenant 切换、配置中心、审计及其他 CDN 配置入口。
- 普通账号的页面路由和所有数据回调都会重新使用会话中的 `tenant_id`，不能通过修改
  URL、隐藏字段或回调请求查询其他租户。
- 创建租户、保存/发布配置和回滚回调会再次验证管理员身份。
- 初始 Dash 布局不包含默认租户或 Tenant 列表，避免普通账号首次加载时获得其他租户信息。
- API endpoint 与 VIP 可由环境变量运行时覆盖，不写入版本数据库。
- 发布使用乐观锁；旧页面保存时若 revision 已变化会被拒绝。
- 双租户使用相同项目名、相同时间和不同域名写入后，查询结果必须严格隔离。
- 回滚必须产生新版本，历史版本内容不得被修改。
- 日志必须能追溯到精确配置版本与任务。
