# macOS 本地启动

本文使用项目相对路径。先完成 Python 3.12 虚拟环境、前端依赖和 Docker Desktop 的安装；通用安装步骤见 [README](README.md#快速开始)。

## 本地基础设施与配置

`docker-compose.local.yml` 将 PostgreSQL、Milvus 映射到独立的本机端口，避免与标准 Compose 配置冲突：

| 服务 | 默认地址 |
| --- | --- |
| 前端 | <http://127.0.0.1:3000> |
| 后端健康检查 | <http://127.0.0.1:8000/health> |
| API 文档 | <http://127.0.0.1:8000/docs> |
| PostgreSQL | `127.0.0.1:5433` |
| Milvus | `http://127.0.0.1:19531` |

首次配置时，在项目根目录复制 `.env.example` 为 `.env`，并针对本地 Compose 修改以下地址：

```dotenv
API_PORT=8000
DATABASE_URL=postgresql+psycopg://devflow:devflow@127.0.0.1:5433/devflow
MILVUS_URI=http://127.0.0.1:19531
```

数据库账号是本地示例值，自定义账号时应与 `POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB` 一致。模型 Key、GitHub Token 和其他私有配置放在 `.env`，该文件不提交到 Git；可执行 `chmod 600 .env` 限制本机访问权限。

## 启动完整应用

从项目根目录运行：

```sh
./start_local.command
```

或者在 PyCharm 中选择 `backend/.venv/bin/python` 作为解释器，运行共享配置 **RepoPilot AI - Full Stack**。

启动器会加载 `.env`，启动本地 Docker 服务、后端和前端，并检查就绪状态。后端端口读取 `API_PORT`，前端 API 地址同步指向该端口。运行日志位于已忽略的 `.devflow/backend.log` 和 `.devflow/frontend.log`。

停止该运行会关闭前后端，数据库容器和数据卷保留。单独停止基础设施但保留数据：

```sh
docker compose -f docker-compose.local.yml stop
```

## 分别启动前后端

基础设施尚未启动时，先从项目根目录执行：

```sh
docker compose -f docker-compose.local.yml up -d --wait
```

另开终端，从项目根目录启动后端：

```sh
cd backend
.venv/bin/python -m uvicorn app.main:app --env-file ../.env --host 127.0.0.1 --port 8000
```

不需要切换 Conda 环境；也可以先 `source .venv/bin/activate`，再用 `python -m uvicorn` 启动。`--env-file` 在导入应用及模型库之前加载配置。

前端从项目根目录启动：

```sh
cd frontend
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000 npm run dev -- --hostname 127.0.0.1 --port 3000
```

手动修改后端端口时，需同步修改前端 API 地址并重启前端。已运行的前端可能仍保留启动时的地址，刷新浏览器不能替代进程重启。

同一项目只运行一个前端开发实例。即使端口不同，多个实例仍会共用 `.next-dev`；项目启动锁会拦截重复启动。新的开发实例取得锁后重建 `.next-dev`，生产构建使用独立的 `.next`。`next-env.d.ts` 会在 `next dev` 或 `next build` 时自动生成，不纳入版本控制。

## 本地模型

默认模型配置使用 `deepseek-v4-pro` 和本地 `Qwen/Qwen3-Embedding-0.6B`。本地 Embedding 依赖通过 `backend/requirements-local-embedding.txt` 安装，首次使用需要下载模型。

模型缓存可通过环境变量配置。确认所需模型已完整缓存后，才在 `.env` 设置 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`，避免运行时反复检查远程文件；首次下载或更换模型时需取消离线设置。离线设置不影响远程 LLM API 请求。

重排器按知识库配置选择。使用远程重排时需提供对应服务凭据；未配置时核对实际降级状态，不把本地启发式重排当成远程模型已接通。

## 仓库与示例数据

`scripts/seed_demo_data.py` 可初始化 `repopilot-demo/sample-api` 合成示例，用于本地功能验证，不对应真实 GitHub 仓库。旧示例可重新运行脚本原位更新标识，关联记录保留。真实仓库的私有访问和同步需要自己的 GitHub Token。

连接仓库后，先确认后台源码准备完成，再扫描项目资料。源码下载、资料扫描和向量索引是不同阶段；状态与失败重试入口位于总览及仓库页面。需语义检索时，再到 RAG 页面建立向量索引。

## 常见问题与检查

- `uvicorn: command not found`：使用项目 `.venv/bin/python -m uvicorn`，或先激活虚拟环境。
- Python 加载应用前报 `failed to make path absolute`：先运行 `/bin/pwd -P` 检查当前目录；若项目位于下载、文稿等受保护目录，核对 Terminal / IDE 的文件夹访问权限。
- 端口占用：先停止原实例，避免启动第二套前端覆盖缓存。
- 重新安装 macOS 依赖可使用 `backend/requirements-macos.lock.txt`；其他环境从 `requirements.txt` 和本地 Embedding 依赖文件安装。

前端构建与运行保护检查：

```sh
cd frontend
npm run build
npm run test:runtime
```
