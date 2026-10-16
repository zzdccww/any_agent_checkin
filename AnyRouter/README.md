# AnyRouter 签到

本服务由根目录的统一 GitHub Actions 工作流每天运行。

部署、`USER_ID` / `SESSION` 获取、Telegram 汇总通知和手动运行步骤见 [统一使用指南](../README.md)。SESSION 过期后需重新登录并手动更新；重复保存同一个值不会续期。

本地配置环境变量、安装依赖后，可运行 `python AnyRouter/app.py`。单独运行仍支持自身的 Telegram 通知；统一工作流由最终汇总任务发送一次通知。
