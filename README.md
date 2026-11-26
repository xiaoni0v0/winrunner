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

默认同时打开 WinRunner 本机控制台。点关闭会短暂隐藏窗口，随后自动打开，不影响服务和任务；要停止服务并取消正在运行的任务，请在启动服务的终端按 **Ctrl+C**。纯命令行或无桌面环境使用 `python winrunner.py --nogui`（不加载 Tkinter）。Windows 上可用 `python -m unittest discover -s tests -v` 运行测试。

服务日志输出到控制台，并追加到程序同目录的 `winrunner.log`，不自动轮转。每行最多 80 列；来自 `127.0.0.1` 的 HTTP 请求不记请求日志；其他来源的请求日志包含操作、HTTP 状态、耗时和部分参数，不记录鉴权头、命令正文或任务 stdout/stderr。任务输出仍只在内存中，通过 API 读取。

## 本机控制台

内置窗口和 HTTP 服务操作同一个 Runner。GUI 不占 HTTP 连接，也不产生 HTTP 请求日志。顶部约每秒刷新 CPU（含 5 秒和 60 秒平均值）、内存、磁盘、运行时长和 Runner ID。

- **项目**：「创建项目」打开独立窗口填写项目名和公开 HTTPS 仓库地址。选中项目可查看仓库和状态、打开项目文件或删除项目。删除时会先确认；成功后清理项目工作树、Git 缓存、任务记录和日志。
- **Action**：在选中项目后点「运行 Action」，填写提交哈希或前缀、解释器和脚本相对路径；有参数时填 JSON 字符串数组。提交前会提示新一轮将重置工作树、覆盖上轮 Action 记录；「取消 Action」只停止该轮，保留日志和当时仍可用的文件。
- **命令**：「项目」和「全局命令」各有「提交命令」和「删除命令」。可以提交 CMD 或多行 Windows PowerShell；删除前需要确认，随后停止该条命令并删除记录和日志。
- **文件**：「项目文件」打开可独立操作的窗口。支持目录分页、直接输入项目相对路径、下载完整文件或单个字节范围（如 `bytes=0-1023`）。关闭文件窗口可取消尚未完成的下载。
- **日志**：选中命令或点「查看 Action」打开详情；可查看退出码、诊断、分页日志及命令原文。stdout 和 stderr 页都可将当前已保存的原始字节导出到本机文件；导出和下载的目标须在 Runner 数据目录之外。命令原文和输出可能包含敏感信息，分享屏幕时请留意。

任务提交窗口的「进阶选项」支持设置等待时间（默认 `0`，立即返回；留空用服务默认值）和幂等键。幂等键与 HTTP 共用；重放需使用相同的作用域、字段和值。克隆、任务提交、取消、删除及文件读写都在后台执行；关闭提交窗口不会取消已经提交的任务。

本机控制台需要带 Tcl/Tk 的 Python 和交互桌面；用 `--nogui` 启动时不会打开窗口，也不能事后附加独立窗口。

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
