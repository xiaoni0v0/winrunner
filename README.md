# WinRunner

在 Windows 上运行 Git 仓库里的脚本和临时命令，通过 HTTP 查询任务状态、读取日志和下载文件。

## 安装 DeepSeek Harness 插件

在 DSH 的 **插件 → 添加插件** 中输入 `github:xiaoni0v0/winrunner`。如果从本地安装，选择包含插件清单的仓库根目录。插件只连接已经部署的 WinRunner，不会安装或启动 Python 服务。

在 **DSH Host 所用账号**的 `~/.winrunner/client.json` 中配置 `baseUrl`（服务地址，含端口、不带路径）和 `token`。用非回环明文 HTTP 时还需显式设置 `allowInsecureHttp: true`；优先用 HTTPS。安装后，新会话可用 `winrunner_project` 管理项目和读取文件、`winrunner_action` 运行 Git 脚本、`winrunner_command` 管理并运行 Windows 命令。

任务以远端 Runner 账号执行，新一轮 Action 会替换上一轮结果。按工具返回的 `next_get` / `next_wait` 继续查询；提交中断或结果为 `unknown` 时，先在同一台 Runner 核对状态，不要直接重提。配置示例和完整工具用法见 [DSH 插件说明](<dsh-winrunner/README.md>)。

## 运行 WinRunner 服务

在 Windows 上使用 Python 3.10+ 和 Git for Windows。在 [winrunner/config.py](<winrunner/config.py>) 中设置自己的令牌，不要将真实令牌提交到 Git。然后在仓库根目录运行：

```powershell
python main.py
```

默认打开本机控制台；不需要桌面时用 `python main.py --nogui`。

- [部署、本机控制台和回归测试](<docs/README.md>)
- [HTTP API 和任务生命周期](<docs/API.md>)
