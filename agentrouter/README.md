# Agent Router 多账号登录

本服务由根目录的统一 GitHub Actions 工作流每天运行。

部署、`ACCOUNTS_JSON`、SOCKS5 代理、Telegram 通知和运行记录说明见 [统一使用指南](../README.md)。登录判定与失败处理见 [服务说明](docs/github-actions-login.md)。

本地验证（在仓库根目录执行）：

```sh
npm --prefix agentrouter ci
npm --prefix agentrouter test
npm --prefix agentrouter run check
```
