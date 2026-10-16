# 每日自动签到

在一个 GitHub Actions 工作流中运行 Agent Router 多账号登录和 AnyRouter 签到，并通过 Telegram 发送一次汇总通知。

- 工作流：[Daily Check-in](.github/workflows/daily-checkin.yml)。每日北京时间 **08:07** 调度（UTC cron：`7 0 * * *`），也支持手动触发。
- 两项服务独立执行；其中一项失败不会阻止另一项。Agent Router 内部按顺序处理账号，单个账号失败后继续处理其余账号。
- 最终汇总包含两项任务的成功、失败、取消或跳过状态、北京时间和本次 Actions 链接。通知和 README 记录不包含账号明细。

## 部署到 GitHub

1. 使用 [zzdccww/any_agent_checkin](https://github.com/zzdccww/any_agent_checkin)，或 Fork 到自己的账号，并在 **Actions** 中启用工作流。
2. 打开 **Settings → Secrets and variables → Actions → New repository secret**，添加下表中的 Secrets。
3. 在 **Settings → Actions → General → Workflow permissions** 中允许工作流所需的写入权限；组织策略也必须允许。
4. 在 **Actions → Daily Check-in → Run workflow** 选择分支并执行，检查两项服务的结果与最终汇总。定时调度使用默认分支上的工作流。

GitHub 定时任务可能延迟或漏跑，不保证准点执行；公开仓库长期没有活动时，定时工作流可能被平台停用，需到 Actions 页面检查并重新启用。

## Secrets 配置

| 名称 | 必填 | 用途 |
| --- | --- | --- |
| `ACCOUNTS_JSON` | 是 | Agent Router 账号数组，格式见下文。 |
| `USER_ID` | 是 | AnyRouter 用户 ID，在登录后的个人设置中查看。 |
| `SESSION` | 是 | AnyRouter 登录 Cookie 中 `session` 字段的值。 |
| `TG_BOT_TOKEN` | 否 | Telegram Bot Token，与 `TG_CHAT_ID` 配对配置。 |
| `TG_CHAT_ID` | 否 | 接收汇总的用户或群组 ID。 |

三个必填项用于完整运行两项服务；缺少某项服务的配置时，对应任务失败，另一项仍会执行。凭据只放入 Secrets，勿提交到仓库、Issue 或日志中。

### Agent Router 账号

`ACCOUNTS_JSON` 必须是非空 JSON 数组，每项包含 `name`、`username`、`password`。以下均为占位示例，请替换：

```json
[
  {
    "name": "账号一",
    "username": "account1@example.com",
    "password": "replace-with-your-password"
  },
  {
    "name": "账号二",
    "username": "account2@example.com",
    "password": "replace-with-your-password",
    "socks5": "socks5://proxy.example.com:1080"
  }
]
```

`name` 应使用不含邮箱或其他个人信息的别名，用于 Actions 日志和 Summary；公开仓库的运行记录也可能被公开查看。每个账号使用独立浏览器上下文。只要存在一个登录失败，Agent Router 任务即标记失败。

`socks5` 可省略、置为 `null` 或留空，此时直连。配置时仅支持无认证的 `socks5://host:port`。代理连接失败、无法打开登录页或登录页返回错误状态时，会关闭代理并直连重试一次；页面打开后的密码错误、验证码和页面操作失败不触发直连重试。详情见 [Agent Router 说明](agentrouter/docs/github-actions-login.md)。

### AnyRouter SESSION

登录 [AnyRouter](https://anyrouter.top)，打开浏览器开发者工具的 **Application / 应用程序 → Cookies**，复制站点的 `session` 值到 `SESSION`，将个人设置中的用户 ID 填入 `USER_ID`。

SESSION 会过期，需要重新登录后手动更新 Secret；失效、退出登录或服务端撤销会使签到失败。脚本按约 30 天估算有效期，该估算不能代替服务端状态。**把同一个 SESSION 再写入 Secret 不会续期**，此工作流不提供自动续期能力。

## Telegram 通知与运行记录

通过 Telegram 的 BotFather 创建机器人，取得 Token；先与机器人发起会话，或将它加入目标群组，再将 Token 与目标 Chat ID 分别填入 `TG_BOT_TOKEN`、`TG_CHAT_ID`。

最终汇总任务会等待两项服务结束，再尝试发送**一条**通知。统一工作流关闭 AnyRouter 自带的独立通知，避免重复推送。任一 Telegram Secret 缺失时明确跳过通知；发送失败会报告错误，检查签到结果时仍以两项服务的任务状态为准。README 提交失败时仍会尝试通知。强制取消整个工作流可能导致汇总无法执行。

自动记录只替换下方标记之间的内容，保留其余部署文档。公开 README 和 Telegram 只显示服务状态；逐账号别名结果在 Actions Summary 中查看。

### 最近一次运行

<!-- checkin-results:start -->
尚无运行记录。配置 Secrets 后，可在 Actions 中手动执行一次。
<!-- checkin-results:end -->

### 权限与保留策略

- `contents: write`：供最终汇总任务提交根目录 README 的结果区；不需要手动创建个人访问令牌。
- `actions: write`：供汇总后的清理步骤删除已完成的旧运行，明确排除当前运行和其他仍在执行的运行。
- 分支保护、仓库规则或组织策略可能阻止自动推送，需按仓库政策允许机器人更新，或接受 README 无法自动记录。不要为此把个人令牌写进代码。
- Git 提交历史会保留旧结果；旧 Actions 记录可能被清理，需要长期保存时请自行归档。

## 本地开发与验证

使用 Node.js 20+、Python 3.11；从仓库根目录执行：

```sh
npm --prefix agentrouter ci
npm --prefix agentrouter test
npm --prefix agentrouter run check
python -m unittest discover -s tests -v
```

Node 测试覆盖账号解析和登录流程；Python 测试使用模拟响应覆盖汇总、通知、README 更新及签到失败状态，不会登录真实账号或发送真实通知。语法及模拟测试通过，仍需在配置 Secrets 后检查一次真实 Actions 运行。

需要本地运行真实服务时，先安装依赖及 Chromium，再通过本机环境变量提供上述凭据：

```sh
npm --prefix agentrouter exec -- playwright install chromium
python -m pip install requests playwright pynacl
python -m playwright install chromium
npm --prefix agentrouter run login
python AnyRouter/app.py
```

Linux 上可为 Playwright 安装命令添加 `--with-deps` 以安装系统依赖。本地真实运行会访问对应站点；AnyRouter 单独运行时仍支持自身的 Telegram 通知。

## 项目结构与限制

- `.github/workflows/daily-checkin.yml`：统一调度、服务任务、汇总与旧运行清理。
- `agentrouter/`：Node.js / Playwright 多账号登录脚本与测试。
- `AnyRouter/app.py`：Python / Playwright 签到脚本。
- `tests/`：Python 离线回归测试。

Agent Router 当前不处理验证码、两步验证或人工确认；AnyRouter 使用单个 `USER_ID` / `SESSION`。站点页面、接口、WAF 或 GitHub 网络变化都可能影响运行；日志或任务失败时需结合 Actions 详情排查。
