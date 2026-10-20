# RepoPilot AI 名称统一

更新日期：2026-10-20。

已统一前端产品标识、浏览器标题、装饰文字、后端应用名、健康检查及 OpenAPI 标题、Agent 系统提示词、通知与错误提示、MCP 服务/客户端元信息、npm 包名、启动提示、IDE 运行配置的显示名称、README、架构/API 文档和三份简历模板。

本地 `.env` 的 `APP_NAME` 已同步；没有更改任何密钥或数据库连接。演示仓库中一条已知旧品牌描述已更新，仓库标识保持原样。

## 兼容范围

为保证原有数据和工具调用继续有效，保留以下内部标识：数据库用户名与数据库名、Docker 容器和数据卷、Milvus collection、缓存键、UUID 命名空间、MCP 工具调用名、已有 `.devflow` 索引与运行路径、Git 分支和快照文件名、现有 IDE 模块/解释器引用、实际检出目录，以及外部仓库的 owner/name/URL。它们属于既有资源地址或协议，不是产品展示名称。

已有用户消息、Agent 历史回答、运行事件和审计记录没有改写。现有截图为相应改版阶段的记录，最新品牌截图见 `ui-mission-control/workspace-repopilot-ai.jpg`。

IDE 运行配置已重命名为 `.run/RepoPilot_AI.run.xml`，显示名称为 `RepoPilot AI - Full Stack`；内部模块引用仍指向实际已有模块，避免使 IDE 无法运行。

## 验证

- 前端 `npm run build`：成功；保留已有的 13 条 Hooks 依赖警告。
- 后端相关测试：67 项通过，覆盖 ChatAgent、Skill/MCP、上下文压缩、Issue/PR/CI Agent、反馈及评测。
- 独立新进程验证：Settings 的应用名、`/health` 的 `app` 字段、OpenAPI 的 `info.title` 均为 `RepoPilot AI`。
- 21 个 Python 文件的 AST 比较：与改名前相比，仅品牌字符串常量改变，没有更改控制流程或事件处理。
- 三份 Word 模板的正文、属性、文件名与生成脚本已同步；已使用 documents 技能渲染并检查全部页面。后端平台模板在相同字体回退环境下，改名前后均为两页。
- 浏览器已核对新品牌标识和页面标题。
- `git diff --check`：通过。

当前本机 8001 端口没有运行中的后端，所以浏览器的数据面板尚未加载；这次没有为改名自动启动后台任务。启动后端后，新名称和 Agent 提示词会生效。已运行的其他环境也需要重启后端以重新加载配置。

## 模板

- [校招 AI 应用开发](resume-templates/RepoPilot_AI_简历模板_校招_AI应用开发.docx)
- [社招 AI 应用工程](resume-templates/RepoPilot_AI_简历模板_社招_AI应用工程.docx)
- [社招后端 Agent 平台](resume-templates/RepoPilot_AI_简历模板_社招_后端Agent平台.docx)
