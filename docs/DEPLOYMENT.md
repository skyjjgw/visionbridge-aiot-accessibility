# 自有云部署

## 1. 推荐拓扑

单节点原型可以在 Ubuntu Server 上运行 Nginx、FastAPI、MediaMTX、coturn 和 SQLite。公网只暴露 HTTPS 与必要的 TURN/WebRTC 端口；FastAPI、MediaMTX 管理 API 和数据库仅监听本机。

## 2. 网络端口

| 端口 | 协议 | 用途 | 公网策略 |
| --- | --- | --- | --- |
| `22` | TCP | SSH 管理 | 仅可信源 IP，优先密钥登录 |
| `80` | TCP | HTTP 跳转/证书签发 | 可选 |
| `443` | TCP | 网站、API、WHEP/HLS | 必需 |
| `3478` | TCP/UDP | TURN | 需要 TURN 时开放 |
| `49160-49200` | UDP | TURN 中继 | 需要 TURN 时开放 |
| `8189` | UDP/TCP | MediaMTX WebRTC ICE | 按部署方式开放 |

`8000`、`8554`、`8888`、`8889`、`9997` 应由 Nginx 或本机服务访问，不建议直接暴露公网。

## 3. 生产配置

服务器环境文件建议保存为 `/etc/visionbridge/api.env`，权限 `0600`；边缘端配置保存在 `/etc/visionbridge/edge.env` 和 `/etc/visionbridge/media-publisher.env`，权限不高于 `0640`。

除 README 列出的密钥外，还应配置：

- `VISIONBRIDGE_CLOUD_HOST`、`VISIONBRIDGE_CLOUD_USER`；
- `AMAP_JS_KEY`、`AMAP_SECURITY_CODE`；
- `VISIONBRIDGE_DEFAULT_LNG`、`VISIONBRIDGE_DEFAULT_LAT`；
- 服务不生成演示数据，禁止在生产库手工写入演示事件；
- `VISIONBRIDGE_EMAIL_DEBUG=0`。

## 4. 发布流程

1. CI 全部通过；
2. 构建 Dashboard 静态文件与 Flutter Web；
3. 将 `services/api`、构建产物和 `deploy` 配置打入带时间戳的发布包；
4. 计算 SHA-256；
5. 备份 SQLite 数据库与当前配置；
6. 上传并切换原子目录；
7. 执行 API、页面、媒体和任务闭环验收；
8. 失败时恢复数据库、服务目录和 Nginx 配置。

`deploy/scripts/deploy_release.py` 和 `deploy/scripts/deploy_media_stack.py` 是当前单节点自动化脚本。它们读取进程环境变量，不应接收写死在命令行或源码中的密码。

## 5. 上线验收

- `GET /api/v1/health` 返回成功；
- Dashboard 与 `/volunteer/` 静态资源无 404；
- 邮箱验证码能发送且公网响应不含 `debugCode`；
- 管理接口要求有效管理员身份；
- 边缘心跳、坐标、识别帧率和事件在刷新周期内出现；
- WebRTC 首帧、实际帧率、丢包和 TURN 回退符合现场网络；
- 志愿者上报、审核、派单、地图接单、处置、复核完整通过；
- 待审核上报可由本人删除，公共记录不能越权删除；
- 数据库、上传目录、环境文件和回滚包已纳入备份。

## 6. 当前限制

SQLite 单写节点、共享边缘上传令牌和本地图片目录都不适合水平扩展。对公网长期运行前，还必须补齐管理员 RBAC、HTTPS、访问日志脱敏、备份恢复演练、速率限制、漏洞扫描和数据保留策略。
