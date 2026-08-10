# 自有云 API

FastAPI + SQLite 业务服务，是设备、事件、地图、志愿者上报和公共任务的唯一事实源。

## 运行

从仓库根目录执行：

```bash
python -m venv .venv
pip install -r services/api/requirements.txt
uvicorn services.api.app:app --reload --port 8000
```

本地环境变量可参考 `apps/dashboard/.env.example`。服务不生成演示数据；空库会返回明确的空状态。生产环境必须关闭调试验证码，并由 Nginx 或独立身份网关保护管理接口。

志愿者和边缘上报会先写入 `raw_ingest`，同步完成确定性质量检查与时空去重，再由后台任务异步调用研华 Agent 网关或 Dify 工作流。未配置外部凭据时，数据仍会可靠入库并标记为 `pending_config`，不会阻塞主链路。生产凭据放在 `/etc/visionbridge/agent.env`，禁止提交仓库。

## 测试

```bash
python -m pytest services/api/test_volunteer_api.py
```

接口列表见 [docs/API.md](../../docs/API.md)，数据和状态流见 [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md)。
