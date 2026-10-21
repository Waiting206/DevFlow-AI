# macOS 本地启动

本项目已安装到 `/Users/waiting_/Downloads/DevFlow-AI`，使用独立 Python 3.12 虚拟环境及 Docker 数据卷。

## 访问地址

- 应用：<http://127.0.0.1:3000>
- 后端及 API 文档：<http://127.0.0.1:8001/docs>
- PostgreSQL：`127.0.0.1:5433`，数据库及用户名均为 `devflow`，密码见本地 `.env`。
- Milvus：`http://127.0.0.1:19531`。

## PyCharm 启动

1. 打开项目根目录。
2. Python 解释器选择 `backend/.venv/bin/python`。
3. 运行配置选择 **RepoPilot AI - Full Stack** 并点击运行。

该运行配置会启动 Docker 数据库、Milvus、FastAPI 和 Next.js，检查它们的就绪状态。停止运行会关闭前后端，数据库容器保留，数据不会丢失。已有其他实例时请先停止原来的 RepoPilot AI 运行，避免端口冲突。

也可以在项目根目录运行：

```sh
./start_local.command
```

启动前请确保 Docker Desktop 正在运行。运行日志位于 `.devflow/backend.log` 和 `.devflow/frontend.log`。

同一个项目只运行一个前端开发服务，即使换成 3001 端口，也不能与 3000 同时启动：它们会共用 `.next-dev`，互相覆盖页面和错误组件的编译文件。现在 `npm run dev`、PyCharm 和直接运行 Next.js 都会经过项目启动锁；重复启动会明确报错，不会写入正在使用的缓存。正常退出会释放锁，进程被强制结束后可自动回收旧锁。

每次新的开发服务启动会在获得锁后清理 `.next-dev`，重新生成一致的开发缓存；业务源码、数据库和生产构建 `.next` 都会保留。`npm run build` 使用独立的 `.next`。前端运行保护的回归测试：`cd frontend && npm run test:runtime`。

## 本地模型与配置

DeepSeek Key 仅保存在权限为 `600` 的 `.env`，前端配置仅包含后端地址。不要提交或分享 `.env`。

已配置 `deepseek-v4-pro`，真实本地 Qwen3 Embedding（1024 维）、Milvus 混合检索和本地启发式重排。Qwen 缓存在 `.data/huggingface`，启动器自动加载缓存路径。DeepSeek Key 无法用于百炼重排接口，因此示例知识库已明确选择可用的本地重排器。

这台机器的 Qwen 模型已完整缓存，`.env` 中设置 `HF_HUB_OFFLINE=1` 和 `TRANSFORMERS_OFFLINE=1`，避免聊天预检索在加载模型时反复联网检查并超时。启动器会在导入模型库前加载这些变量。更换模型或首次下载模型时需单独准备缓存；离线模式不会下载新模型，也不会影响 DeepSeek API 请求。

`docker-compose.local.yml` 使用 Milvus 内置 etcd 和本地对象存储，无需单独部署 MinIO；独立端口避免与电脑上现有项目冲突。停用基础设施（保留数据）可运行：

```sh
docker compose -f docker-compose.local.yml stop
```

项目已导入自带的 `course-demo/devflow-sample-api` 示例数据。真实 GitHub 仓库的私有访问和同步需要你自己的 GitHub Token，可在项目连接页面填写。

## 仓库连接与项目资料扫描

RepoPilot 分别展示源码准备状态和资料扫描状态。“连接成功”表示仓库身份已验证，不代表后台源码已经下载完成。

1. 连接仓库后，在首页总览或仓库接入页查看源码状态；准备中会自动刷新状态。
2. 源码就绪后，“扫描项目资料”才可点击。未就绪时，后端也会拒绝扫描，不会把半成品目录误当成完整项目。
3. 下载失败时显示脱敏后的原因，可以点击“重试准备源码”，无需重复同步 Issue、PR、CI。
4. 资料扫描成功后，README、项目文档和配置清单进入数据库；需要语义检索时，再到 RAG 页面重建向量索引。

扫描入口始终可见，不再依赖当前会话是否为空。服务中断后遗留的准备或扫描任务，超过 15 分钟会显示为可重试，避免一直停留在进行中。旧数据库的源码状态字段会在后端启动时自动补齐，已有完整 checkout 仍可直接使用。

## 重新安装依赖

```sh
backend/.venv/bin/python -m pip install -r backend/requirements-macos.lock.txt
cd frontend
npm ci
```

Python 原始依赖中的 Pydantic 版本约束已修复，当前 macOS 安装版本保存于 `backend/requirements-macos.lock.txt`。前端 Next.js 与 eslint-config-next 已更新为同主版本的 `15.5.27`，并已完成生产构建验证。
