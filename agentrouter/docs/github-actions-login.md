# Agent Router 登录说明

部署与账号格式统一维护在 [根目录 README](../../README.md)。请运行根目录 `.github/workflows/daily-checkin.yml` 的 **Daily Check-in**，无需另建独立工作流。

## 账号与代理

`ACCOUNTS_JSON` 是非空数组，每项要求 `name`、`username`、`password`；`name` 使用不含个人信息的别名。逐账号结果显示在 Actions Summary，公开 README 和 Telegram 只记录服务状态。

每个账号使用独立 Chromium 上下文，按数组顺序执行；某个账号失败不会阻止后续账号，但任一失败会使整个 Agent Router 任务失败。

全局 Secret `PROXY_URL` 支持 VLESS、VMess、Trojan 和 Shadowsocks 节点，启动后优先用于全部账号，失败时不回退直连。配置方法见 [全局节点代理](../../README.md#全局节点代理)。

未配置全局节点时，可选 `socks5` 仅接受无认证的 `socks5://host:port`，省略、`null` 或空字符串表示直连。账号代理连接失败、登录页无法打开或返回错误 HTTP 状态时直连重试一次；成功打开页面后的认证或交互失败不切换网络。

## 成功判定与排障

- 登录入口为 `https://agentrouter.org/login`，脚本等待账号输入框或“使用邮箱或用户名登录”入口出现。
- 输入账号密码并提交后，等待离开 `/login`，随后访问受保护页面 `/console/topup`。
- 只有受保护页面返回成功状态且最终地址位于 `/console` 下，才判定成功；返回 `/login` 时判定失败。
- 失败诊断记录阶段、网络方式、HTTP 状态、去除查询参数的最终 URL 和脱敏页面标题，不记录页面正文。Actions 会屏蔽解析出的用户名、密码与代理地址，账号别名仍可见。

当前不处理验证码、两步验证或人工确认。离线测试不能代替真实账号验证；页面或站点网络变化后，请查看 Actions 详情。

## 本地检查

在仓库根目录执行 `npm --prefix agentrouter test` 和 `npm --prefix agentrouter run check`。浏览器安装与真实运行命令见 [统一使用指南](../../README.md#本地开发与验证)。
