# WinRunner

在 Windows 上运行 Git 仓库里的脚本和临时命令，通过 HTTP 查询任务状态、读取日志和下载文件。

## 安装 DeepSeek Harness 插件

在 DSH 的 **插件 → 添加插件** 中输入 `github:xiaoni0v0/winrunner`。如果从本地安装，选择包含插件清单的仓库根目录。插件只连接已经部署的 WinRunner，不会安装或启动 Python 服务。

在 **DSH Host 所用账号**的 `~/.winrunner/client.json` 中填写 Runner 的地址和令牌。配置示例、三个工具的用法和断线后的核对流程见 [DSH 插件说明](<dsh-winrunner/README.md>)。

## 运行 WinRunner 服务

在 Windows 上使用 Python 3.10+ 和 Git for Windows。在 [winrunner/config.py](<winrunner/config.py>) 中设置自己的令牌，不要将真实令牌提交到 Git。然后在仓库根目录运行：

```powershell
python main.py
```

默认打开本机控制台；不需要桌面时用 `python main.py --nogui`。

- [部署、本机控制台和回归测试](<docs/README.md>)
- [HTTP API 和任务生命周期](<docs/API.md>)
