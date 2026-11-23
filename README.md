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

默认同时打开本机监控窗口。点关闭会短暂隐藏窗口，随后自动打开，不影响服务和任务；要停止服务并取消正在运行的任务，请在启动服务的控制台按 **Ctrl+C**。纯命令行或无桌面环境使用 `python winrunner.py --nogui`（不加载 Tkinter）。Windows 上可用 `python -m unittest discover -s tests -v` 运行测试。

服务日志输出到控制台，并追加到程序同目录的 `winrunner.log`，不自动轮转。每行最多 80 列；来自 `127.0.0.1` 的 HTTP 请求（包括 GUI 轮询）不记请求日志；其他来源的请求日志包含操作、HTTP 状态、耗时和部分参数，不记录鉴权头、命令正文或任务 stdout/stderr。任务输出仍只在内存中，通过 API 读取。

## 本机监控窗口

内置窗口约每秒在后台自动刷新，不占 HTTP 连接，也不产生请求日志。顶部保留 CPU（含 5 秒和 60 秒平均值）、内存、磁盘和运行时长。「项目」页选中项目即可查看仓库、当前 Action 的阶段和诊断提示，以及项目命令列表；「全局命令」页单独展示全局命令。

选中一条命令后，下方才展开详情，可以复制完整命令原文，查看退出码、stdout、stderr 和失败诊断。Action 点「查看 Action」展开日志、诊断和已解析的提交。输出较长时可按页查看；收起后停止读取该任务的日志。监控窗口是只读的，不提交或取消任务。命令原文和输出可能包含敏感信息，分享屏幕时请留意。

若希望手动打开或关闭监控窗口，可用 `--nogui` 启动服务，再单独运行 `python winrunner_gui.py`。独立窗口通过本机 HTTP API 自动刷新，需要服务监听 `127.0.0.1` 或 `0.0.0.0`。两种窗口都需要带 Tcl/Tk 的 Python 和交互桌面。

## 任务和结果

[Project](<API.md#项目>) 同步克隆公开 HTTPS 仓库。项目下可以浏览文件、提交 Action，也可以在当前工作树里运行命令：

- [Action](<API.md#action>)：同一项目一次只能跑一个。新请求一旦占槽，就替换旧 Action 日志并尝试重置工作树，即使请求随后校验失败。取消 Action 会保留当时的日志和文件。
- [项目命令](<API.md#项目命令>)：支持 CMD 和多行 PowerShell，从当前工作树启动。每条命令有独立的 ID 和日志，不重置目录，也不覆盖已有记录。可以与其他命令和 Action 并行；共享文件由调用方协调。删除命令会终止其进程树并丢弃该条日志，删除项目会一并清理其命令。

[全局命令](<API.md#全局命令>) 同样支持并行，但从 Runner 账号的用户目录启动，有独立的 ID 集合。

`timeout` 只限制 POST 等待响应的时间，不限制任务运行时长。每项任务用独立的 Windows Job Object 管理进程树。任务输出可通过 API 增量读取；日志只存在内存中，不限量、不自动淘汰。服务重启或实例回收前，请取走所需的日志、文件和检查点。

## 开发检查

若本机未安装 Black 或 mypy，可将它们装进项目的 `.venv`；运行服务和 GUI 不依赖这两个工具。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install black==26.3.1 mypy==1.19.1
$sources = @(git ls-files '*.py')
.\.venv\Scripts\python.exe -m black --check @sources
.\.venv\Scripts\python.exe -m mypy
```
