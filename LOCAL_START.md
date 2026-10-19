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
3. 运行配置选择 **DevFlow AI - Full Stack** 并点击运行。

该运行配置会启动 Docker 数据库、Milvus、FastAPI 和 Next.js，检查它们的就绪状态。停止运行会关闭前后端，数据库容器保留，数据不会丢失。已有其他实例时请先停止原来的 DevFlow 运行，避免端口冲突。

也可以在项目根目录运行：

```sh
./start_local.command
```

启动前请确保 Docker Desktop 正在运行。运行日志位于 `.devflow/backend.log` 和 `.devflow/frontend.log`。

## 本地模型与配置

DeepSeek Key 仅保存在权限为 `600` 的 `.env`，前端配置仅包含后端地址。不要提交或分享 `.env`。

已配置 `deepseek-v4-pro`，真实本地 Qwen3 Embedding（1024 维）、Milvus 混合检索和本地启发式重排。Qwen 缓存在 `.data/huggingface`，启动器自动加载缓存路径。DeepSeek Key 无法用于百炼重排接口，因此示例知识库已明确选择可用的本地重排器。

`docker-compose.local.yml` 使用 Milvus 内置 etcd 和本地对象存储，无需单独部署 MinIO；独立端口避免与电脑上现有项目冲突。停用基础设施（保留数据）可运行：

```sh
docker compose -f docker-compose.local.yml stop
```

项目已导入自带的 `course-demo/devflow-sample-api` 示例数据。真实 GitHub 仓库的私有访问和同步需要你自己的 GitHub Token，可在项目连接页面填写。

## 重新安装依赖

```sh
backend/.venv/bin/python -m pip install -r backend/requirements-macos.lock.txt
cd frontend
npm ci
```

Python 原始依赖中的 Pydantic 版本约束已修复，当前 macOS 安装版本保存于 `backend/requirements-macos.lock.txt`。前端 Next.js 与 eslint-config-next 已更新为同主版本的 `15.5.27`，并已完成生产构建验证。
