# Security

- API Key 和 GitHub Token 只通过环境变量或用户输入提供。
- 不在日志中打印 token。
- Agent 不自主执行 GitHub 写入：先生成草稿，经人工确认、权限和状态校验后，由确认接口执行受支持的操作并记录审计日志。
- GitHub Token 如需持久化，必须加密保存。
