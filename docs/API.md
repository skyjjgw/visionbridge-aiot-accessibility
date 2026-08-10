# HTTP API 概览

默认前缀为 `/api/v1`。完整请求字段以 `services/api/app.py` 的 Pydantic 模型为准；生产环境应通过 HTTPS 访问。

| 领域 | 方法与路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| 健康 | `GET /health` | 无 | 服务存活与版本信息 |
| 公共配置 | `GET /config/public` | 无 | 地图公开 Key、默认中心点等非秘密配置 |
| 邮箱登录 | `POST /auth/email/request` | 无 | 发送验证码，带频率限制 |
| 邮箱登录 | `POST /auth/email/verify` | 无 | 校验验证码并创建会话 |
| 当前用户 | `GET /auth/me` | 用户令牌 | 返回志愿者资料 |
| 上报 | `POST /volunteer/reports` | 用户令牌 | multipart 图片与障碍描述 |
| 上报 | `GET /volunteer/reports/mine` | 用户令牌 | 当前用户历史上报 |
| 上报 | `DELETE /volunteer/reports/{id}` | 用户令牌 | 仅删除仍待审核的本人上报 |
| 地图 | `GET /map/obstacles` | 用户令牌 | 公共障碍物与关联任务状态 |
| 任务 | `GET /volunteer/tasks` | 用户令牌 | 可接公共任务 |
| 任务 | `POST /volunteer/tasks/{id}/claim` | 用户令牌 | 原子认领任务 |
| 任务 | `POST /volunteer/tasks/{id}/complete` | 用户令牌 | 上传处理照片与说明 |
| 管理 | `GET /admin/reports` | 管理员 | 待审核/已审核上报 |
| 管理 | `PATCH /admin/reports/{id}` | 管理员 | 审核、入库并可发布任务 |
| 管理 | `PATCH /admin/tasks/{id}` | 管理员 | 复核、驳回或关闭任务 |
| 分析 | `GET /admin/analysis/status` | 管理员 | 分析提供方、任务状态和质量标记汇总 |
| 分析 | `GET /admin/analysis/jobs` | 管理员 | 查询异步清洗任务与错误 |
| 分析 | `POST /admin/analysis/jobs/{id}/retry` | 管理员 | 人工重试失败或待配置任务 |
| 边缘 | `POST /telemetry` | 上传令牌 | 设备心跳、识别指标、事件和快照 |
| 大屏 | `GET /overview` | 只读策略 | 系统概览 |
| 大屏 | `GET /events` | 只读策略 | 事件地图与列表 |
| 设备 | `GET /devices` | 只读策略 | 多设备健康与媒体状态 |
| 媒体 | `POST /media/auth` | MediaMTX | 发布/读取授权回调 |
| 实时 | `WS /ws/realtime` | 同源策略 | 业务状态失效通知，客户端收到后重取 REST 快照 |

## 错误约定

- `400`：业务前置条件不满足；
- `401`：令牌缺失、过期或无效；
- `404`：资源不存在或不属于当前用户；
- `409`：任务已被认领等并发冲突；
- `413`：上传图片超过限制；
- `422`：请求字段校验失败；
- `429`：验证码等接口请求过于频繁。

客户端应展示面向用户的错误说明，不应直接显示 Pydantic 原始校验结构或服务器堆栈。
