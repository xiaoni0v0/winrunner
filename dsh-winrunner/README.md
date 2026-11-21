# WinRunner 客户端（DeepSeek Harness）

WinRunner 的 Host-only 客户端。无需安装额外的 Node 依赖。WinRunner 服务端仍独立部署；此插件只发送已有 API 的请求。

## 配置和安装

1. 在**运行 DSH Host 的账号**下新建 `~/.winrunner/client.json`（Windows 为 `%USERPROFILE%\.winrunner\client.json`）。可复制 `client.example.json` 的结构，填入 `baseUrl`（例如 `http://192.0.2.10`，不要加路径）、`port`（例如 `8080`）和 `token`。也可写成 `"baseUrl": "http://192.0.2.10:8080"`，此时将 `port` 设为 `null` 或省略；两处都填时必须一致。实际凭据文件应放在仓库之外，不要上传或粘贴到对话。
2. 若必须使用非回环明文 HTTP，在配置中显式设置 `"allowInsecureHttp": true`。HTTP 不加密令牌或请求内容；优先用 HTTPS，明文时应依赖受控的专用网络。插件不跟随重定向，不自动重试写操作。
3. 在 DSH 的 **插件** → **添加插件** → **本地目录** 选择本目录。首次安装如果显示已应用，不用重启，开一个新会话确认工具可见；如果提示需要重启，或者要加载已安装插件的新代码，则重启 DSH。安装会影响当前 profile；只给可信的 profile 启用。插件的命令工具会以远端 Runner 账号运行命令，Action 提交会替换上一轮结果。

已在本机验证（WinRunner 实际服务端）：三个工具出现在工具列表；改配置里的端口后无需重启即生效；项目创建、目录列表、文件读取、Action 全链路（提交解析→检出→解释器启动→stderr 捕获）、`commit_not_found` 与项目 `404` 错误路径、命令启动与 `delete` 取消均正常。

如需换位置，可在 **Host 启动环境**设置 `WINRUNNER_CLIENT_CONFIG` 为绝对路径。每次 HTTP 请求都会重新打开和读取该文件，切换地址或轮换令牌无需重启插件。不要把 token 放在工具参数里。请把同一项目的提交与轮询绑定在同一台 Runner 上：改配置后，下一次调用可能指向另一台机器。

## 三个 AI 工具

工具只暴露 `op` 和 `params_json`。后者是该操作的 JSON 对象字符串，只写需要的字段；`health` 和 `list` 填 `""` 或 `"{}"`，不需要其他占位参数。

`health` 的 `cpu` 中，`usage_percent` 是瞬时值（最近约 1 秒），`usage_5s_percent` 和 `usage_60s_percent` 分别是过去 5 秒和 60 秒的时间加权平均。均为整机百分比（0–100），不是 0–1 比例；启动初期按已有样本计算，无有效样本时为 `null`，不代表 0%。

| 工具 | `op` | 常用调用 |
|---|---|---|
| `winrunner_project` | `health`、`list`、`get`、`create`、`delete`、`files` | `{"op":"files","params_json":"{\"name\":\"demo\",\"path\":\"result.txt\"}"}` |
| `winrunner_action` | `start`、`get`、`wait`、`cancel` | `{"op":"start","params_json":"{\"name\":\"demo\",\"commit\":\"abc1234\",\"interpreter\":\"python\",\"script\":\"task.py\",\"wait_seconds\":5}"}` |
| `winrunner_command` | `start`、`list`、`get`、`wait`、`delete` | `{"op":"start","params_json":"{\"mode\":\"ps\",\"command\":\"Write-Output '第一行'\\nWrite-Output '第二行'\",\"wait_seconds\":5}"}` |

- 短任务可以给 `start` 传 `wait_seconds: 5`。任务已结束且有输出时，客户端顺带读取两路短日志；如果补读失败，仍返回原任务结果，并单列 `logs_error`。`start` 默认不等（0 秒），最多等 60 秒；终态日志是额外的只读请求，最多再等 4 秒。
- `pending` 时优先用返回的 `next_wait`，不要重复提交。`wait` 默认最多等 15 秒，可指定 1–60 秒。它把约每秒一次的查询合在一次工具调用里；等满还没结束时返回最后一次状态，标记 `wait_expired: true`；续接的 `next_wait` 回到默认 15 秒，不沿用本次的短等待上限。`start` 和 `wait` 的等待上限都不会停止远端任务。要停止 Action 用 `cancel`，要停止临时命令并丢弃记录用 `delete`。
- 优先读 `outcome`：`succeeded` / `failed` / `cancelled` 是任务终态；`pending` 表示仍在准备、运行或取消；`request_error` 表示请求失败，`unknown` 表示没有可靠结论。`http_status` 只用于诊断。任务的 `status`、`phase`、`output_source` 和 `error` 仍保留；请求错误也保留服务端提供的 `action` 诊断。
- `get` / `wait` 不传偏移就从头预览两路日志（每路最多 2048 字节）。只看状态用 `status_only: true`。返回 `next_get` 时可直接用它续读；如果两路已暂时追平而任务仍在运行，返回的是 `next_wait`，不必反复读同一页。每路的 `remaining` 表示 `available`（有更多已存字节）、`caught_up`（暂时追平）或 `complete`（已读完）。默认 UTF-8；跨页字符或非 UTF-8 输出可用 `encoding: "base64"` 保留原字节。
- 文件和目录有下一页时，直接把 `next_get` 传回 `winrunner_project`；原始偏移仍分别在 `next_offset` 和 `next_entry_offset`。UTF-8 页尾会尽量避开跨页字符；需要逐字节还原时用 `encoding: "base64"`。翻页不保证快照。`entry_offset` / `entry_limit` 由 Runner 处理，`entry_limit` 默认 100，最大 1000。文件的 `length` 和日志的 `limit` 在 UTF-8 模式下至少 4 字节，更小的页用 Base64。
- `outcome_unknown: true` 表示写操作结果无法确认。超时或断线后，先在同一台 Runner 核对状态，不要盲目重试。Action 可以用项目名和计划提交哈希核对，但同一哈希的两次运行可能无法区分；命令提交响应里的 ID 丢失时，可以通过 `list` 查候选，但未必能唯一定位。

Action 和命令的 `start` 可选 `idempotency_key`（1–128 个字母、数字或 `._~-`）。新提交用新键。只有确认地址未变、Runner 未重启，才用原键和原参数（包括 `wait_seconds`）重试；客户端不会自动重提。键只在本次 Runner 进程内有效，服务端最多记录 4096 个键。带键的响应是首次提交快照，可能不是当前状态；客户端不把当前日志合并进旧快照，另用返回的 `next_get` 或 `next_wait` 查询。同键正在处理时稍后用原键重试；已明确收到失败结果后若要发起新提交，应换新键。

长任务开始前用 `winrunner_project` 的 `health` 检查 Runner，必要时记住 `run_id`。命令 ID 只在 Runner 本轮运行内有效；收到中断或重启通知后，不要继续操作旧 ID。Action 只按项目名查询当前轮次，日志读完前不要对同一项目再次 `start`。客户端补读时会拒绝明显不同的快照，但无法区分状态、提交和输出长度都相同的两次运行；不会在每次轮询时另外调用 `health`。

例如 `start` 返回 `{"outcome":"pending","next_wait":{"op":"wait","params_json":"{\"name\":\"demo\"}"}}`，直接用同一个工具执行 `next_wait`。脚本退出码非零时，`outcome` 为 `failed`，即使 `http_status` 为 200。PowerShell 命令传原文，不用额外加一层引号或自行 Base64；CMD 语法与变量展开仍由 CMD 解释。

## Git 复合操作

第一版不会在本机运行 Git。后续可加一个显式调用的复合操作：只提交明确选定的改动，保留当前分支和暂存区，解析完整提交哈希，推到指定仓库的临时分支，再提交 Action。应逐步返回 `commit` / `push` / `action` 状态、完整哈希和远端分支；失败时给出明确的恢复点。推送成功但 Action 返回 `409` 时，可只重试 Action，不必再次提交或推送。不要自动删除远端临时分支，尤其不能在 Runner 完成 fetch 前删除；删除可能让该提交不再可达。

## 本机测试

`node --test`。测试使用本机回环模拟服务和自动清理的临时配置，不需要 WinRunner 实例或真实凭据。
