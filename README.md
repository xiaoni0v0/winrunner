# WinRunner

把一台 Windows 竞价实例作为 HTTP 算力节点。调用方提交 Git 仓库里的脚本或临时 Windows 命令，轮询状态，取回日志和文件。接口见 [API.md](<API.md>)。

## 运行方式

- 支持 Windows 10 / Windows Server 2016 或更新版本。镜像预装 Python 3.10+、Git for Windows，以及任务需要的 Node.js 等解释器；Runner 不依赖第三方 Python 包。
- 先在 [config.py](<config.py>) 中把 `TOKEN = "123456"` 改为自己的随机令牌；占位值不能启动。监听地址和端口设为 `0.0.0.0:18080`，不需要设置环境变量。不要提交真实令牌，制作通用镜像前应还原占位值。
- **启动前在云安全组中只允许调用端的出口 IP 访问 TCP 18080。** 所有 API 均要求 `Authorization: Bearer <token>`。HTTP 不防窃听或篡改；任务以 Runner 账号权限运行，不做沙箱，只接收可信调用方和脚本。
- 数据目录默认是项目目录下的 `.winrunner-data`；Runner 每次启动会清掉其中的旧项目和缓存。Action 解释器白名单也在 [config.py](<config.py>) 中，默认提供 `python`、`python3` 和 `node`。

从项目目录在 PowerShell 启动：

```powershell
python winrunner.py
```

按 **Ctrl+C** 停止。Windows 上可用 `python -m unittest discover -s tests -v` 运行测试。

服务日志输出到控制台，并追加到程序同目录的 `winrunner.log`，不自动轮转。每行最多 80 列；请求日志包含操作、HTTP 状态、耗时和部分参数，不记录鉴权头、命令正文或任务 stdout/stderr。任务输出仍只在内存中，通过 API 读取。

## 本机监控窗口

服务启动后，在同一台机器的另一个终端运行：

```powershell
python winrunner_gui.py
```

窗口用同一份配置里的令牌和端口访问 `127.0.0.1`，只读显示 CPU、内存、磁盘、运行指纹，以及项目和临时命令状态。窗口不负责启停服务，关闭它不影响任务。需要带 Tcl/Tk 的 Python 和交互桌面；无桌面环境仍可单独运行服务。若服务改成绑定单个非回环 IP，监控窗口无法连接。

## 任务和结果

- **Project**：同步克隆公开 HTTPS 仓库。创建后即可浏览和下载当前检出目录的内容，包括 `.git`。不自动拉取子模块或 Git LFS 文件。项目列表和详情直接显示 Action 状态；`GET /projects/{name}/actions` 可查详情和日志。
- **Action**：每个 Project 只有一个槽位。请求一旦占槽就清除上一轮文件和输出，即使这次失败。每次刷新远端分支和标签，按目标提交干净检出，不 merge；强推或删引用后，仅残留在本地缓存里的提交不能执行。停止 Action 会保留当时的文件和输出。
- **命令**：`/commands` 支持 `cmd` 和多行 PowerShell，允许并行。短 ID 仅在本轮服务内唯一；重启可通过 `/health` 的 `run_id` 识别。删除会终止任务并丢弃记录和输出。
- **等待与终止**：`timeout` 只限制 POST 等响应的时间，默认 10 秒；设为 `0` 即不等，不限制任务运行时长。每项任务用独立 Windows Job Object 管理自身的进程树；不影响其他任务。

脚本和命令写入 stdout/stderr 管道的数据可立即轮询，不等换行或退出。日志在内存中按原始字节保存，不限量、不自动淘汰。实例回收或 Runner 退出后不会恢复；调用方负责控制输出量、定期取走结果和检查点，并让脚本能从外部检查点继续运行。

## 开发检查

若本机未安装 Black 或 mypy，可将它们装进项目的 `.venv`；运行服务和 GUI 不依赖这两个工具。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install black==26.3.1 mypy==1.19.1
$sources = @(git ls-files '*.py')
.\.venv\Scripts\python.exe -m black --check @sources
.\.venv\Scripts\python.exe -m mypy
```
