# WinRunner

把一台 Windows 竞价实例作为 HTTP 算力节点。调用方提交 Git 仓库里的脚本或临时 Windows 命令，轮询状态，取回日志和文件。接口见 [API.md](<API.md>)。

## 运行方式

- 服务端用 Python 标准库，不依赖第三方 Python 包。镜像预装 Python、Git for Windows，以及任务需要的 Node.js 等解释器。
- 在 `config.py` 配置监听地址、专用数据目录和 Action 解释器白名单；首版提供 `python`、`python3` 和 `node`。令牌通过环境变量 `RUNNER_TOKEN` 提供，不进镜像或日志。
- 客户端拿到实例公网 IP 后用 HTTP 直连。**云安全组只放行调用端的出口 IP**，所有 API 均要求 `Authorization: Bearer <token>`。HTTP 不防窃听或篡改；任务以 Runner 账号权限运行，不做沙箱，只接收可信调用方和脚本。
- 数据目录由 Runner 独占。**每次启动都清理其中的旧项目和缓存，不自动恢复。** 镜像用来复用工具和配置，不用来续接任务。

## 任务和结果

- **Project**：同步克隆公开 HTTPS 仓库。创建后即可浏览和下载当前检出目录的内容，包括 `.git`。不自动拉取子模块或 Git LFS 文件。项目列表和详情直接显示 Action 状态；`GET /projects/{name}/actions` 可查详情和日志。
- **Action**：每个 Project 只有一个槽位。请求一旦占槽就清除上一轮文件和输出，即使这次失败。每次刷新远端分支和标签，按目标提交干净检出，不 merge；强推或删引用后，仅残留在本地缓存里的提交不能执行。停止 Action 会保留当时的文件和输出。
- **命令**：`/commands` 支持 `cmd` 和多行 PowerShell，允许并行，按 ID 查询和删除。删除会终止任务并丢弃记录和输出。
- **等待与终止**：`timeout` 只限制 POST 等响应的时间，默认 10 秒；设为 `0` 即不等，不限制任务运行时长。每项任务用独立 Windows Job Object 管理自身的进程树；不影响其他任务。

脚本和命令写入 stdout/stderr 管道的数据可立即轮询，不等换行或退出。日志在内存中按原始字节保存，不限量、不自动淘汰。实例回收或 Runner 退出后不会恢复；调用方负责控制输出量、定期取走结果和检查点，并让脚本能从外部检查点继续运行。
