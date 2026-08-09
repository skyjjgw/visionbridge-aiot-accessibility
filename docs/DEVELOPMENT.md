# 开发与本地联调

## 1. 首次准备

从仓库根目录执行：

```bash
python -m venv .venv
pip install -r services/api/requirements-dev.txt

cd apps/dashboard
npm ci

cd ../volunteer
flutter pub get
```

复制 `apps/dashboard/.env.example` 为本地 `.env`，只在本地文件中填写配置。不要修改示例文件来保存真实凭据。

## 2. 推荐启动顺序

1. 从仓库根目录启动 `uvicorn services.api.app:app --reload --port 8000`；
2. 在 `apps/dashboard` 运行 `npm run dev`；
3. 在 `apps/volunteer` 运行 Flutter Web 或 Android；
4. 使用调试验证码创建志愿者上报；
5. 在管理大屏完成审核与派单；
6. 回到 App 从地图或任务页认领并提交处理证据；
7. 在管理端复核，确认公共地图和未处理队列同步变化。

## 3. 本地邮件模式

```text
VISIONBRIDGE_EMAIL_DEBUG=1
VISIONBRIDGE_SEED_DEMO_DATA=1
VISIONBRIDGE_AUTH_SECRET=仅用于本次开发的随机值
VISIONBRIDGE_INGEST_TOKEN=仅用于本次开发的随机值
```

调试邮件模式会在 API 响应中返回验证码，只能绑定到回环地址或受信任开发网络。

## 4. 测试矩阵

| 模块 | 命令 | 覆盖重点 |
| --- | --- | --- |
| Dashboard | `npm run lint && npm test` | 构建、关键页面渲染 |
| API | `python -m pytest services/api/test_volunteer_api.py` | 登录、上报、删除、审核、派单、接单、复核、媒体授权 |
| Flutter | `flutter analyze && flutter test` | 模型解析、坐标工具、关键 Widget |
| Edge | `python -m py_compile edge/pi-runtime/visionbridge_edge_agent.py` | 基础语法；真实摄像头/GNSS 仍需实机验证 |

## 5. 接口改动规则

修改 API 字段或状态时，同时更新：

- `services/api/app.py` 的模型和测试；
- `apps/dashboard` 的类型与页面；
- `apps/volunteer/lib/models.dart` 和 `api_client.dart`；
- `docs/API.md` 与相关需求文档。

## 6. 常见问题

- Android 真机不能访问电脑的 `127.0.0.1`，请使用局域网地址并确认防火墙；
- 地图空白通常与高德 Key、Web 服务安全密钥、域名白名单或 WebView 网络权限有关；
- 手机定位精度主要由系统定位权限、GNSS/网络定位环境和采样窗口决定，不是云服务器计算；
- WebRTC 能播放但延迟高时，先检查边缘编码帧率、时间戳、是否走 TURN，以及公网丢包和上行带宽。
