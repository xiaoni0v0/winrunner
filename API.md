# WinRunner HTTP API

## 通用规则

- 连接被接纳后，先检验 `Authorization: Bearer <token>`；缺失或错误返回 `401`。并发 HTTP 连接默认上限 64（[config.py](<config.py>) 中的 `MAX_HTTP_CONNECTIONS`），满额会在鉴权前尝试返回 `503 server_busy` 和 `Retry-After: 1`。这不是后台任务并发上限。
- POST 用 `Content-Length` 发送 UTF-8 JSON 对象，请求体默认上限 1 MiB；表格未标为可选的字段必填。除文件下载、`204` 和无报文体的 `HEAD` 外，响应为 JSON。
- HTTP 错误体为 `{"error":{"code":"…","message":"…"}}`。`400` 参数或请求体不合法，`404` 路由或资源不存在，已知路由不支持的方法返回 `405` 和 `Allow`，`409` 与当前状态冲突，`413` 请求体过大，`500` 服务端错误。各接口另有专用状态码时单独列出。
- `timeout` 是可选的非负有限秒数，支持小数，默认 `10`。仅用于 Action、命令 POST；从接受请求开始计时，包含准备和执行。`0` 不等待，`60` 最多等待 60 秒。提前结束立即返回 `200` 与终态，未结束返回 `202` 与当前状态，任务继续。任务执行失败也用终态表达，不用 HTTP 错误状态；请求体不合法等 HTTP 错误除外。

### 启动请求重放

`POST /projects/{name}/actions`、`POST /commands` 和 `POST /projects/{name}/commands` 可带一个 `Idempotency-Key` 请求头（1–128 个 ASCII 字符，只允许字母、数字和 `._~-`）。每次新提交用新键；网络中断后用原键、原请求体重试。全局命令、每个项目的 Action 和每个项目实例的命令分别有自己的作用域；项目名不区分大小写。同名项目删除后重建，项目命令的旧键不会重放旧项目的响应。

- 同键、同参数返回首次提交的 HTTP 状态和响应体，不再启动任务。JSON 空白和对象键顺序不影响匹配；字段和值须一致（包括 `timeout`），无法解析的请求体按原始字节比较。首次提交错误同样记录；例如首次因槽位忙收到 `409`，之后要重新提交时需换新键。
- 同键、不同参数返回 `409 idempotency_conflict`。首次提交仍在生成响应时，同参数重试会返回 `409 idempotency_pending`；稍后继续用原键重试。
- 记录只存在本次 Runner 进程中；删除单条命令不会释放旧键。项目删除后，该实例的命令提交不可再重放，但旧记录仍计入缓存上限。所有作用域合计最多 4096 条记录，满后新键返回 `503 idempotency_capacity`，仍可访问的既有键可以重放。重启后记录丢失，不能盲目跨重启重试。
- 重放的是原始提交结果，不是当前状态。Action 仍只有一个槽位，无 Action ID；重放不会恢复已被下一轮覆盖的日志或产物。命令仍用现有短 ID 查询。

## Project

### `POST /projects`

| 请求字段 | 类型 | 规则 |
|---|---|---|
| `name` | string | `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`；大小写不敏感、全局唯一；拒绝 Windows 设备保留名。工作目录按注册名命名。 |
| `repo_url` | string | 公开 HTTPS Git 仓库的最终地址；不跟随重定向，不带凭据、查询参数或片段。 |

先原子注册名字，再删除同名残留目录并同步克隆，检出远端默认分支。不自动取子模块或 Git LFS 内容。返回 `201` + `Project`。名称已占用返回 `409`；Git 失败返回 `502` 和原始原因，不留下可见项目。若同名残留暂时无法删除，重试创建会返回 `409 project_cleanup_blocked`，直到释放占用并清理完成；不会沿用残留。

### `GET /projects`

`200`：`{"projects":[Project, …]}`。

### `GET /projects/{name}`

`200`：`Project`；名称不存在返回 `404`。

`Project` 字段：`name`（string）、`repo_url`（string）、`status`（见下表）。

| `status` | 含义 |
|---|---|
| `cloning` / `deleting` | 正在创建或删除项目。 |
| `delete_failed` | 删除未完成，项目不再接收 Action，也不提供文件；请重试 DELETE。 |
| `idle` | 项目已建好，还没有 Action。 |
| `preparing` / `running` / `cancelling` | 当前 Action 的状态。 |
| `succeeded` / `failed` / `cancelled` | 最近一次 Action 的结果，保留到下次提交；项目仍可用。 |

### `DELETE /projects/{name}`

终止该 Project 的克隆、Action 和所有命令，中断旧下载，删除目录、Git 缓存和记录后释放名字。成功 `204`，不存在 `404`。发出停止和中断请求后，默认最多等 5 秒让任务、下载退出；尚未退出返回 `409 project_busy`。磁盘清理受阻返回 `409 project_cleanup_blocked`；两种情况均可重试 DELETE。全局或其他项目的命令不受影响。

### `GET /projects/{name}/files`

| 请求参数 | 类型 | 规则 |
|---|---|---|
| `path`（查询，可选） | string | 相对项目根目录；省略或空字符串表示根目录。含空格等字符时按 URL 查询参数编码。允许访问 `.git`，拒绝绝对路径、越界路径和能造成越界的链接或目录联接。 |
| `Range`（请求头，可选） | string | 仅文件支持单段字节范围，如 `bytes=0-1023`。 |
| `entry_offset`（查询，可选） | integer | 目录起始条目，从 0 开始，默认 0。 |
| `entry_limit`（查询，可选） | integer | 目录页面最多返回的条目数，1–1000，默认 100。 |

- 目录：`200` JSON，`{"path":"…","entries":[{"name":"…","type":"file","size_bytes":42}]}`。`path` 为相对路径，根目录为 `""`；`type` 为 `file` / `directory` / `other`；`size_bytes` 对非普通文件为 `null`。
- 传任一分页参数时，按文件名忽略大小写排序（相同则按原名排序），并附 `next_entry_offset`；为 `null` 表示没有下一页。分页 JSON 不超过 512 KiB，因此实际条目数可能少于 `entry_limit`。目录在两次查询之间变化时，偏移不是快照，可能重复或跳过条目。不传分页参数仍返回完整列表且不附游标。
- 文件：分页参数不适用（返回 `400`）；`200` 原始字节（`application/octet-stream`）；单段范围返回 `206` 和 `Content-Range`；不可满足的范围返回 `416`。
- 创建成功后可读；当前路径不存在或已删除时返回 `404`。文件或目录被独占时返回 `409 file_busy`，可重试。普通文件下载读取已打开的文件对象；重命名或删除后已有下载可继续，但原地写入不保证静态快照。Windows 旧式 `os.replace` 不一定能替换正在读取的目标。
- 下一次 Action 占槽或 Project 删除会中断旧下载；请先取完所需文件再提交下一轮。

## Action

### `POST /projects/{name}/actions`

| 请求字段 | 类型 | 规则 |
|---|---|---|
| `commit` | string | 十六进制 commit hash 或缩写。必须在本次远端分支和标签可达的提交中唯一匹配。 |
| `interpreter` | string | `config.py` 白名单中的名字，首版为 `python` / `python3` / `node`。 |
| `script` | string | 检出目录中的脚本相对路径。确认路径未越界后，以绝对路径作为单个参数传给解释器，不经 shell。 |
| `args`（可选） | string[] | 脚本参数，默认 `[]`，逐项传递。 |
| `timeout`（可选） | number | 只限制 POST 等待时间，见通用规则。 |

- 每个 Project 只有一个槽位，没有 Action ID。正在准备、运行、取消中，或 Project 不可用时返回 `409`，不覆盖现有任务。
- 已鉴权且 HTTP 层能接收的请求一旦占槽，旧日志和文件立即不再可读；重置工作树并清理旧文件后才准备新任务。JSON 或字段不合法也算一轮，返回 `400`；可通过 GET 查询失败阶段。若 Windows 文件占用导致未能清理，Action 在 `reset` 阶段失败，残留不会作为新产物提供。
- 如果请求体非法，且工作线程在重置工作区时也失败，POST 仍返回 `400 invalid_request`，并附顶层 `action` 字段，含这次 Action 的状态快照（与 GET 相同，不附日志内容）。例如 `action.phase` 为 `reset`，`action.error.code` 为 `reset_blocked`。
- 干净检出目标提交后，以仓库目录为工作目录运行。刷新并清理远端分支和标签引用，不 merge；不可达的缓存提交不能运行。找不到或匹配不唯一时不启动脚本，`error.code` 分别为 `commit_not_found`、`commit_ambiguous`。
- `200` / `202`：`Task` 加字段 `commit`（解析后的完整 hash；尚未解析或解析失败为 `null`）。

### `GET /projects/{name}/actions`

`200`：当前或最近一次 Action，结构同 POST 成功响应；`status` 与 Project 列表和详情中的 Action 状态一致，可按偏移取日志。没有 Action 返回 `404`；Project 正在删除或删除未完成时返回 `409`。

### `DELETE /projects/{name}/actions`

终止当前 Action 的整个进程树，保留已有日志和文件。已经结束则不改动。成功 `204`；没有 Action 返回 `404`。

## 临时命令

### `POST /commands`

| 请求字段 | 类型 | 规则 |
|---|---|---|
| `mode` | string | `cmd`（Windows 命令提示符）或 `ps`（Windows PowerShell）。 |
| `command` | string | 一段 shell 命令；`ps` 支持多行，换行用 JSON 转义 `\n` 传输。不接受数组。 |
| `timeout`（可选） | number | 只限制 POST 等待时间，见通用规则。 |

非交互运行，不加载 PowerShell 用户配置。工作目录为 Runner 账号的用户目录，命令可自行 `cd`。可并发。`200` / `202`：`Command`，即 `Task` 加字段 `id`（服务端分配的 string）、`mode`（`cmd` / `ps`）。

`ps` 默认关闭进度提示，并将 PowerShell 自己写入管道的文本设为 UTF-8（无 BOM）。命令可用 `$ProgressPreference = 'Continue'` 恢复进度提示。原生子程序仍可能使用自己的编码，Runner 不转码、不剥除 CLIXML，日志 API 仍返回原始字节。若执行策略禁止加载本地脚本模块，可在 [config.py](<config.py>) 将 `PS_SUPPRESS_PROGRESS` 设为 `False`，跳过这两项启动设置，无需放宽系统策略。

`id` 是从 `0` 开始的 36 进制小写字符串（`0`–`z`，随后是 `10` 等），在本次服务运行中不复用。服务重启后从头编号；跨轮保存或操作任务时，应同时核对 `/health` 的 `run_id`。

### `GET /commands`

`200`：`{"commands":[Command, …]}`，不附日志内容。

### `GET /commands/{id}`

`200`：`Command`，可按偏移取日志；ID 不存在返回 `404`。

### `DELETE /commands/{id}`

终止此命令的整个进程树并删除记录、日志。成功 `204`；ID 不存在返回 `404`。

## 项目命令

项目命令与全局 `/commands` 使用相同的请求字段、响应结构、等待语义、Shell 行为和日志接口，但进程从该项目的当前工作树启动，不使用 Runner 用户目录。四个接口如下：

| 接口 | 行为 |
|---|---|
| `POST /projects/{name}/commands` | 提交一条 `mode: "cmd"` 或 `mode: "ps"` 的命令。完成返回 `200`，仍在执行返回 `202`。 |
| `GET /projects/{name}/commands` | `200`：`{"commands":[Command, …]}`，仅列该项目的命令，不附日志正文。 |
| `GET /projects/{name}/commands/{id}` | `200`：该项目的指定命令，可按偏移读取日志。 |
| `DELETE /projects/{name}/commands/{id}` | 停止该命令的整个进程树，并删除记录和日志。成功 `204`。 |

- 项目或命令不存在返回 `404`。项目正在删除时，新的提交返回 `409`；删除失败退出后仍可提交。其他项目状态不阻止提交，包括正在克隆或执行 Action。启动时工作树不存在或不可用，则用已分配的 ID 记录终态 `failed` / `launch_failed`，不会新建工作树或退回用户目录。
- 项目命令不 fetch、不 checkout、不重置工作树，也不改 Action 状态。它们可以与同项目的其他命令和 Action 并行。**调用端负责协调共享文件**：命令可修改工作树，Action 仍可能在命令运行期间重置它。起始目录不是权限边界。
- 每个项目名（忽略大小写）有独立的 ID 计数，首次从 `0` 开始；同一 Runner 进程内，同名项目删除重建后继续编号。不同项目或全局集合可以有相同 ID。Runner 重启后重新计数。
- 每条命令单独保存 stdout/stderr，内存中不限量、不自动淘汰。后续提交和 Action 不覆盖它们。删除命令、删除整个项目或重启 Runner 后，记录和日志不可再查。克隆失败回滚会取消并丢弃该项目已接收的命令。

## Task 与增量日志

| `Task` 字段 | 类型与含义 |
|---|---|
| `status` | `preparing` / `running` / `cancelling` / `succeeded` / `failed` / `cancelled`。后三项为终态。 |
| `phase` | Action：`reset` / `validate` / `fetch` / `resolve` / `checkout` / `launch` / `run`；命令：`launch` / `run`。终态保留最后阶段。 |
| `output_source` | `null`（尚无来源）、`runner`（请求、文件或 commit 解析错误等）、`git`（Git 命令失败）、`launch`（进程启动失败）、`process`（成功启动后的进程输出，包括解释器报错）。成功的 Git 准备输出不混入脚本日志。 |
| `exit_code` | integer 或 `null`；已取得的解释器或 shell 主进程退出码，没有则为 `null`。 |
| `error` | `{"code":"…","message":"…"}` 或 `null`；失败或取消时的诊断，例如取消等待后仍未能清理旧目录。 |
| `stdout_bytes`, `stderr_bytes` | integer；当前各路已存储的字节数。 |

脚本和命令写入管道的 stdout/stderr 分开读取并立即对轮询可见，不等换行或进程退出。Python Action 以 `-u` 启动；其他程序如果自身缓存输出，需自行 flush。Git 准备中只显示阶段，失败时才公开诊断。

查询单个 Action 或命令时，默认返回状态和日志字节数，不附日志内容。可传以下查询参数读取日志：

| 查询参数 | 规则 |
|---|---|
| `stdout_offset`、`stderr_offset` | 可分别传入从 `0` 开始的非负整数字节偏移；未传的一路不返回内容。 |
| `limit` | 每路最多读取的字节数；可选，默认 `65536`，范围 `1`–`1048576`。只限制一次读取，不限制保存量。 |

每路请求的日志分别在 `stdout` / `stderr` 字段返回 `{"data_base64":"…","next_offset":123}`。从 `next_offset` 继续轮询；已到当前末尾时返回空数据，偏移不变。偏移超过当前长度返回 `400`。Action 开始下一轮后旧偏移不可续用；命令日志保留到 DELETE 或服务退出。

## 主机状态

### `GET /health`

`200` JSON：

| 字段 | 内容 |
|---|---|
| `cpu` | `logical_cores` 为逻辑核数；`usage_percent` 为最近约 1 秒的 CPU 使用率，`usage_5s_percent` 和 `usage_60s_percent` 为相应窗口的时间加权平均。启动初期按窗口内已有有效样本计算；无对应有效样本时为 `null`。采样由后台定时进行，查询频率不影响数值。 |
| `memory` | `{"total_bytes":…,"available_bytes":…}`；物理内存。 |
| `disk` | `{"total_bytes":…,"available_bytes":…}`；项目数据目录所在卷。 |
| `uptime_seconds` | number；Runner 进程运行秒数。 |
| `run_id` | string；启动时生成的 32 位小写十六进制随机指纹。本轮运行内不变，每次重启重新生成；不是机器标识，也不是令牌。 |
