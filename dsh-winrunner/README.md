# WinRunner 客户端（DeepSeek Harness）

WinRunner 的 Host-only 客户端，不需要额外安装 Node 依赖。WinRunner 服务端仍独立部署；此插件只发送已有 API 的请求。

## 配置和安装

1. 在**运行 DSH Host 的账号**下新建 `~/.winrunner/client.json`（Windows 为 `%USERPROFILE%\.winrunner\client.json`）。可参考 [`dsh-winrunner/client.example.json`](<client.example.json>)，填入 `baseUrl`（例如 `http://192.0.2.10`，不要加路径）、`port`（例如 `8080`）和 `token`。也可写成 `"baseUrl": "http://192.0.2.10:8080"`，此时将 `port` 设为 `null` 或省略；两处都填时必须一致。实际凭据文件应放在仓库之外，不要上传或粘贴到对话。
2. 若必须使用非回环明文 HTTP，在配置中显式设置 `"allowInsecureHttp": true`。HTTP 不加密令牌或请求内容；优先用 HTTPS，明文时应依赖受控的专用网络。插件不跟随重定向，不自动重试写操作。
3. 在 DSH 的**插件**页点击**添加插件**（或从旁边的下拉菜单选择**安装第三方插件**），将 `github:xiaoni0v0/winrunner` 填入**包名或地址**，点击**安装**并确认。已克隆仓库时也可以填入**仓库根目录的绝对路径**（含 [`package.json`](<../package.json>)），不要选择 `dsh-winrunner/` 子目录。不需要执行 `npm install` 或单独部署客户端。
4. 打开新会话，确认 `winrunner_project`、`winrunner_action`、`winrunner_command` 可用。如果 DSH 提示需要重启，先重启再验证。插件会影响当前 profile；只在可信的 profile 启用。命令工具以远端 Runner 账号运行命令，提交 Action 会替换上一轮结果。

从旧版升级时，在 DSH 中卸载同名插件（包括此前从 `dsh-winrunner/` 安装的版本），再从上述 GitHub 地址或新仓库根目录安装，重启 DSH 后验证。无需改动仓库外的 `~/.winrunner/client.json`。GitHub 地址未指定提交，不代表插件会自动更新。

三个工具及基础任务流程已在旧版 WinRunner 上做过本机验证。新版接口的回归测试使用回环模拟服务，不需要部署 WinRunner 或更改当前 DSH profile。

如需换位置，可在 **Host 启动环境**设置 `WINRUNNER_CLIENT_CONFIG` 为绝对路径。每次 HTTP 请求都会重新打开和读取该文件，切换地址或轮换令牌无需重启插件。不要把 token 放在工具参数里。请把同一项目的提交与轮询绑定在同一台 Runner 上：改配置后，下一次调用可能指向另一台机器。

## 三个 AI 工具

工具只暴露 `op` 和 `params_json`。后者是该操作的 JSON 对象字符串，只写需要的字段；`health`、项目列表和全局命令列表填 `""` 或 `"{}"`。

`health` 的 `cpu` 中，`usage_percent` 是瞬时值（最近约 1 秒），`usage_5s_percent` 和 `usage_60s_percent` 分别是过去 5 秒和 60 秒的时间加权平均。均为整机百分比（0 至 100），不是 0 至 1 的比例；启动初期按已有样本计算，无有效样本时为 `null`，不代表 0%。

| 工具 | `op` | 常用调用 |
|---|---|---|
| `winrunner_project` | `health`、`list`、`get`、`create`、`delete`、`files` | `{"op":"files","params_json":"{\"name\":\"demo\",\"path\":\"result.txt\"}"}` |
| `winrunner_action` | `start`、`get`、`wait`、`cancel` | `{"op":"start","params_json":"{\"name\":\"demo\",\"commit\":\"abc1234\",\"interpreter\":\"python\",\"script\":\"task.py\",\"wait_seconds\":5}"}` |
| `winrunner_command` | `start`、`list`、`get`、`wait`、`cancel`、`delete` | `{"op":"start","params_json":"{\"mode\":\"ps\",\"command\":\"Write-Output '第一行'\\nWrite-Output '第二行'\",\"wait_seconds\":5}"}` |

`winrunner_command` 的所有操作都接受可选的 `name`；省略则使用全局集合。`list` 可传 `status: "active"`（准备中、运行中、取消中），或 `preparing`、`running`、`cancelling`、`succeeded`、`failed`、`cancelled` 之一精确筛选；省略列全部。各集合可以有相同的 `id`，查询、等待、取消或删除时要带上启动时的项目名。`next_get` 和 `next_wait` 会保留这个参数。

项目命令从当前工作树启动，不重置目录、不覆盖已有日志。命令之间以及命令与 Action 可以并行，调用端需协调共享文件；Action 仍可能重置工作树。详见 [项目命令 API](<../docs/API.md#项目命令>)。

- 短任务可以给 `start` 传 `wait_seconds: 5`。任务已结束且有输出时，客户端顺带读取两路短日志；如果补读失败，仍返回原任务结果，并单列 `logs_error`。`start` 默认不等（0 秒），最多等 60 秒；终态日志是额外的只读请求，最多再等 4 秒。
- `pending` 时优先用返回的 `next_wait`，不要重复提交。`wait` 默认最多等 15 秒，可指定 1 至 60 秒。它把约每秒一次的查询合在一次工具调用里；等满还没结束时返回最后一次状态，标记 `wait_expired: true`；续接的 `next_wait` 回到默认 15 秒，不沿用本次的短等待上限。`start` 和 `wait` 的等待上限都不会停止远端任务。Action 和命令都可用 `cancel` 停止并保留记录和日志；命令的 `cancel` 强制终止进程树，可重复调用，不改已结束命令的状态。命令的 `cancel` 返回成功表示操作完成，不等于任务一定变成 `cancelled`；用返回的 `next_get` 核对。只有要丢弃命令记录和日志时才用 `delete`。
- 优先读 `outcome`：`succeeded` / `failed` / `cancelled` 是任务终态；`pending` 表示仍在准备、运行或取消；`request_error` 表示请求失败，`unknown` 表示没有可靠结论。`http_status` 只用于诊断。任务的 `status`、`phase`、`output_source` 和 `error` 仍保留；请求错误也保留服务端提供的 `action` 诊断。
- `get` / `wait` 不传日志选择参数时，从头预览两路日志（每路最多 2048 字节）。可用 `stdout_tail` / `stderr_tail` 取指定流最后 N 字节；同一路不能混用尾读和偏移，`limit` 仅控制偏移读取。尾读可能从行或字符中间开始；需要原始字节时用 `encoding: "base64"`。返回 `next_get` 或 `next_wait` 时可直接续读，续读用偏移而不是再取一次尾部。每路的 `remaining` 表示 `available`（有更多已存字节）、`caught_up`（暂时追平）或 `complete`（已读完）。
- 只看状态用 `status_only: true`。`get` / `wait` 显式传 `include_input: true` 可查看命令原文（可能含敏感信息），或查询 Action 的 `action_id`；可与 `status_only` 一起用。正常的日志续读提示不再次请求命令原文。新版 Runner 返回 `started_at`（任务记录创建，含准备阶段）和 `finished_at`（进入终态，没结束为 `null`），都是 Unix 毫秒时间戳；旧版可能没有这两个字段。
- 文件和目录有下一页时，直接把 `next_get` 传回 `winrunner_project`；原始偏移仍分别在 `next_offset` 和 `next_entry_offset`。UTF-8 页尾会尽量避开跨页字符；需要逐字节还原时用 `encoding: "base64"`。翻页不保证快照。`entry_offset` / `entry_limit` 由 Runner 处理，`entry_limit` 默认 100，最大 1000。文件的 `length` 和日志的 `limit` 在 UTF-8 模式下至少 4 字节，更小的页用 Base64。
- `outcome_unknown: true` 表示写操作结果无法确认。超时、断线，或收到无法明确判断结果的 `5xx` 后，先在同一台 Runner 核对状态，不要盲目重试。DSH 开始执行写操作后若取消本地工具调用，也会提示远端操作可能已生效。项目创建用 `get` 和原 `name` 核对仓库地址与状态；同名创建不会覆盖已有项目。Action 可用项目名和计划提交哈希核对，但同一哈希的两次运行可能无法区分；命令提交响应里的 ID 丢失时，可在原命令集合用 `list` 查候选，但未必能唯一定位。

Action 和命令的 `start` 可选 `idempotency_key`（1 至 128 个字母、数字或 `._~-`）。新提交用新键。确认地址未变、Runner 未重启，且项目命令所属的项目未删除重建后，才用原键和原参数（包括 `wait_seconds`）重试；客户端不会自动重提。键只在本次 Runner 进程内有效，服务端最多记录 4096 个键。带键的响应是首次提交快照，可能不是当前状态；客户端不把当前日志合并进旧快照，另用返回的 `next_get` 或 `next_wait` 查询。同键正在处理时稍后用原键重试；已明确收到失败结果后若要发起新提交，应换新键。

长任务开始前用 `winrunner_project` 的 `health` 检查 Runner，必要时记住 `run_id`。命令 ID 只在 Runner 本轮运行内有效；收到中断或重启通知后，不要继续操作旧 ID。Action 的 `next_wait` / `next_get` 会保留已知的 `action_id`。手动续查时也可把该值传给工具的 `get` / `wait`；从第一次响应起就核对，轮次不同返回 `task_changed`，不返回其他轮次的日志。如果指定了轮次而响应没有标记，插件不会把它认作原任务。旧版 Runner 若从未返回轮次标记，仍可按项目名查当前轮次，但不能保证跨调用指向同一轮。`cancel` 仍按项目名操作当前轮次，日志读完前也不要对同一项目再次 `start`。插件不会在每次轮询时另外调用 `health`；命令 ID 和操作对象仍由调用方核对。列表不分页；命令列表过大时优先用 `status: "active"` 或精确状态缩小范围，插件不会自动拉取全量列表。

收到 `429 auth_ip_banned` 时，`retry_after_seconds` 是封禁剩余秒数；先检查本机令牌配置，等待封禁到期再请求。插件不会自动重试。

如果服务端还在 `b46ae98`，需要升级才能使用状态筛选、尾读和仅取消。旧版仍可无筛选列出命令、查询原文、按偏移读日志；插件不会把 `cancel` 降级成 `delete`。需要在旧版看尾部时，可先只查状态和日志字节数，再用 `max(字节数 - N, 0)` 作为偏移；这两次查询之间日志可能增长。

例如 Action `start` 返回 `pending` 时，把 `next_wait`（包括 `params_json` 中的 `name` 和 `action_id`）原样传回同一个工具。脚本退出码非零时，`outcome` 为 `failed`，即使 `http_status` 为 200。PowerShell 命令传原文，不用额外加一层引号或自行 Base64；CMD 语法与变量展开仍由 CMD 解释。

## Git 复合操作

第一版不会在本机运行 Git。后续可加一个显式调用的复合操作：只提交明确选定的改动，保留当前分支和暂存区，解析完整提交哈希，推到指定仓库的临时分支，再提交 Action。应逐步返回 `commit` / `push` / `action` 状态、完整哈希和远端分支；失败时给出明确的恢复点。推送成功但 Action 返回 `409` 时，可只重试 Action，不必再次提交或推送。不要自动删除远端临时分支，尤其不能在 Runner 完成 fetch 前删除；删除可能让该提交不再可达。

## 本机测试

在仓库根目录运行 `npm test`。测试使用本机回环模拟服务和自动清理的临时配置，不需要 WinRunner 实例或真实凭据。用 `npm pack --dry-run` 可检查插件包会包含哪些文件。
