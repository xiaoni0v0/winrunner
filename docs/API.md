# WinRunner HTTP API

[项目](<#项目>)（管理、文件、Action、项目命令） · [全局命令](<#全局命令>) · [主机状态](<#主机状态>) · [任务响应与日志](<#任务响应与日志>) · [幂等重试](<#幂等重试>)

## HTTP 约定

- 所有接口要求 `Authorization: Bearer <token>`。缺失或错误时返回 `401`，并封禁 TCP 对端 IP 10 秒（不信任 `X-Forwarded-For`）。封禁期间不校验令牌，直接返回 `429 auth_ip_banned` 和 `Retry-After`（剩余秒数向上取整），不会延长封禁。未被封禁的 IP 不受鉴权频率限制。共享出口或代理 IP 的客户端共用封禁；已通过鉴权的请求不会被中断。封禁仅存内存，重启清除。
- 默认最多接纳 64 个并发 HTTP 连接；满额会在鉴权前尝试返回 `503` 和 `Retry-After: 1`；已收到方法的非 HEAD 请求附 `server_busy` JSON，尚无法识别方法时不附报文体。这个限制不控制后台任务并发数。
- 除命令 `/cancel` 不接受正文外，POST 用 `Content-Length` 发送 UTF-8 JSON 对象，默认上限 1 MiB。请直接发送正文，不要等待 `Expect: 100-continue`；Runner 不发送该临时响应。除文件下载、`204` 和无报文体的 `HEAD` 外，响应为 JSON。
- 错误体为 `{"error":{"code":"…","message":"…"}}`。`400` 表示请求不合法，`404` 表示路由或资源不存在，`405` 表示该路由不支持此方法（附 `Allow`），`409` 表示状态冲突，`413` 表示请求体过大，`500` 表示服务端错误。

## 项目

Action、项目命令和文件接口使用同一个工作树。不同项目的任务可以并行；同一项目内的规则如下：

| 操作 | 对其他任务的影响 | 工作树 | 日志与记录 |
|---|---|---|---|
| 提交 Action | 同项目只能跑一个 Action；不检查是否有命令在运行。 | 一旦占槽就停止提供旧文件、中断旧下载，尝试删除旧工作树；清理成功后才能重新检出。 | 覆盖上一轮 Action，不覆盖命令记录。 |
| 提交项目命令 | 可与其他命令、Action 并行，不排队。 | 从当前工作树启动；Runner 不重置、不拉取、不切换提交。命令自身可以改文件。 | 每次分配新 ID，不覆盖任何已有日志。 |
| 取消 Action | 只停止该 Action 的进程树。 | 保留当时可用的文件。 | 保留该轮 Action 记录与日志。 |
| 取消项目命令 | 只强制终止该命令的进程树。 | 不清理文件，不回退命令已做的修改。 | 保留该 ID 的记录与日志。 |
| 删除项目命令 | 同取消。 | 同取消。 | 删除该 ID 的记录与日志。 |

### 创建与管理

| 接口 | 结果 |
|---|---|
| `GET /projects` | `200`：`{"projects":[Project, …]}`。 |
| `POST /projects/{name}` | 同步克隆并检出默认分支，成功返回 `201` 和 `Project`；只创建，不覆盖。 |
| `GET /projects/{name}` | `200`：`Project`；项目不存在返回 `404`。 |
| `DELETE /projects/{name}` | 停止项目的克隆、Action 和所有项目命令，中断该项目的下载；删除工作树、Git 缓存及记录后释放名字，返回 `204`。不影响其他项目和全局命令。 |

创建时由调用端在路径中指定 `name`，请求体只传 `{"repo_url":"https://example.com/owner/repo.git"}`。

- `name` 匹配 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`，大小写不敏感、全局唯一，不接受 Windows 设备保留名。工作目录按注册名命名；重名返回 `409`，不重新克隆或改动已有项目。
- `repo_url` 是公开 HTTPS Git 仓库的最终地址，不含凭据、查询或片段。Runner 不跟随重定向，不取子模块或 Git LFS 内容。创建从同一次克隆的快照初始化工作树；后续 Action 以远端分支和标签为准重新检出，Runner 自身不会 commit、push、merge 或 rebase。Git 克隆失败返回 `502`，含原始原因，不留下可见项目。

创建请求断线后，可用同一路径的 GET 核对 `repo_url` 和 `status`；`cloning` 表示同步克隆尚未完成。`POST /projects` 不再接受创建请求，返回 `405`。

创建时先占用名字并清理同名残留目录，不复用残留。清理受阻返回 `409 project_cleanup_blocked`，释放占用后可重试创建。删除项目时，默认最多等 5 秒让任务和下载退出，超时返回 `409 project_busy`；磁盘清理受阻返回 `409 project_cleanup_blocked`。这两种删除失败都可重试 DELETE。项目不存在时 DELETE 返回 `404`。

`Project` 含 `name`、`repo_url` 和 `status`。`cloning` / `deleting` 表示正在创建或删除；`delete_failed` 表示删除未完成，此时不能提交 Action 或读取文件，但创建回滚完成后仍可提交项目命令。`idle` 表示项目已创建且没有 Action；其他值是当前或最近一次 Action 的 [任务状态](<#任务响应与日志>)。项目命令不改变 `Project.status`。

### 文件

`GET /projects/{name}/files` 浏览或下载当前工作树。可选查询 `path` 为相对路径，省略或留空表示根目录；空格等字符按 URL 查询参数编码。允许访问 `.git`，拒绝绝对路径、父目录组件和 Windows 重解析点；即使链接或目录联接指向工作树内，也不跟随。

- 目录返回 `200` 和 `{"path":"…","entries":[{"name":"…","type":"file","size_bytes":42}]}`。根目录的 `path` 为 `""`；`type` 为 `file`、`directory` 或 `other`，非普通文件的 `size_bytes` 为 `null`。
- 目录按文件名忽略大小写排序，比较结果相同时按原名排序。查询可传 `entry_offset`（从 0 开始，默认 0）或 `entry_limit`（1 至 1000，默认 100）。传任一项即启用分页，响应附 `next_entry_offset`，`null` 表示末页。分页 JSON 以 512 KiB 为上限；目录变化时可能重复或漏掉条目。不传分页参数时返回完整列表，不附游标。
- 文件返回 `200` 和原始字节（`application/octet-stream`）；单段 `Range`（如 `bytes=0-1023`）返回 `206` 和 `Content-Range`，不可满足时返回 `416`。文件不接受目录分页参数（`400`）。
- 工作树不可用或路径不存在时返回 `404`；文件或目录被独占时返回 `409 file_busy`，可重试。下载从打开的文件句柄读取；文件重命名或删除后，已有下载可以继续，但原地写入不保证快照。Windows 旧式 `os.replace` 可能无法替换正在读取的目标。提交下一轮 Action 或删除项目之前，先取完所需文件。

### Action

| 接口 | 结果 |
|---|---|
| `POST /projects/{name}/actions` | 提交一轮 Action。返回 `200` / `202` 及 `Task`，另含 `commit`（解析出的完整哈希，未解析时为 `null`）和本轮的 `action_id`。 |
| `GET /projects/{name}/actions` | `200`：当前或最近一次 Action，默认不含 `action_id`；可[读取日志](<#增量日志>)。 |
| `DELETE /projects/{name}/actions` | 等待进程树退出后返回 `204`。已经结束则不改动；不删除记录、日志或产物。 |

GET 和 DELETE 在没有 Action 时返回 `404`，项目正在删除或删除未完成时返回 `409`。POST 在项目未就绪或同项目的 Action 仍在准备、运行或取消时返回 `409`，不覆盖现有任务。Action 不通过单独的 ID 寻址；需要识别轮次时，可读取下文的 `action_id`。

| POST 字段 | 含义 |
|---|---|
| `commit` | string；十六进制哈希或唯一前缀，必须能从本次远端分支或标签到达。 |
| `interpreter` | string；[config.py](<../winrunner/config.py>) 中白名单里的名字，默认有 `python`、`python3`、`node`。 |
| `script` | string；检出目录内的脚本相对路径。验证未越界后，以绝对路径作为单个参数传给解释器，不经 shell。 |
| `args`（可选） | string[]；默认 `[]`，逐项传给解释器。 |
| `timeout`（可选） | number；见[提交与等待](<#提交与等待>)。 |

- 请求一旦占槽，就替换旧 Action 记录，使旧文件不可读，并尝试重置工作树。即使在重置前取消、旧文件仍在磁盘上，也不会重新对外提供。即使随后发现 JSON 或字段不合法，这轮也会替换旧结果；POST 返回 `400 invalid_request`。如果重置也失败，`400` 响应会附 `action` 状态快照（不含日志正文）。
- 重置完成后刷新并清理远端分支和标签引用，按目标提交干净检出，不 merge。遇到 Windows 文件式引用的大小写冲突时，Runner 用远端的裸克隆快照重建本地 Git 缓存，不合并或改写远端。脚本以检出后的工作树为工作目录。找不到可达提交（包括只残留在本地缓存的情况）或匹配不唯一时，分别报 `commit_not_found`、`commit_ambiguous`。
- 工作树清理受阻时，Action 状态为 `failed`，`phase: reset`，`error.code: reset_blocked`；错误消息包含系统原因，残留文件不会当作新产物提供。若是项目命令占用了目录，暂停提交新命令，等占用目录的命令结束，或逐条取消并等到 `204`，然后重新提交 Action（如使用幂等键，换新键）。已结束的命令不用删。其他进程仍可能占用文件，停止项目命令不保证 Action 一定成功。

### 项目命令

| 接口 | 结果 |
|---|---|
| `POST /projects/{name}/commands` | 提交一条 CMD 或 PowerShell 命令，返回 `200` / `202` 和 `Command`。字段见[命令请求与响应](<#命令请求与响应>)。 |
| `GET /projects/{name}/commands` | `200`：`{"commands":[Command, …]}`，只列该项目的命令，不含日志正文，可[按状态过滤](<#命令状态过滤>)。 |
| `GET /projects/{name}/commands/{id}` | `200`：指定的 `Command`，可[读取日志](<#增量日志>)。 |
| `POST /projects/{name}/commands/{id}/cancel` | 强制终止该命令的进程树，等待退出，保留记录和日志，返回 `204`。 |
| `DELETE /projects/{name}/commands/{id}` | 强制终止该命令的进程树，等退出后删除记录和日志，返回 `204`。 |

项目或命令不存在时返回 `404`。项目正在删除时，新命令返回 `409`；其他项目状态不拦截提交，包括克隆中、Action 准备或运行、项目删除未完成。如果启动时工作树不存在或无法用作起始目录，仍保留已分配的 ID，任务以 `failed` 结束，`error.code` 为 `launch_failed`。Runner 不建目录，也不退回用户目录。克隆失败回滚时会停止并丢弃该项目已接收的命令。

并行时由调用端协调共享文件。Action 不会停止正在运行的项目命令，但重置可能删除命令依赖的文件；目录被占用时，Action 可能以 `reset_blocked` 失败。起始目录不是权限边界。

每个项目名（忽略大小写）独立编号，从 `0` 开始。同一 Runner 内同名项目重建后继续编号；不同项目或全局集合可以有相同的 `id`。日志独立保存在内存，直到删除该命令、删除项目或 Runner 退出。Runner 重启后重新计数。

## 全局命令

全局命令从 Runner 账号的用户目录启动，不归属项目。可同时运行多条，也不与 Action 或项目命令互斥。Runner 不重置项目工作树，后续提交不会覆盖已有命令的记录和日志。

| 接口 | 结果 |
|---|---|
| `POST /commands` | 提交一条 CMD 或 PowerShell 命令，返回 `200` / `202` 和 `Command`。字段见[命令请求与响应](<#命令请求与响应>)。 |
| `GET /commands` | `200`：`{"commands":[Command, …]}`，只列全局命令，不含日志正文，可[按状态过滤](<#命令状态过滤>)。 |
| `GET /commands/{id}` | `200`：指定的 `Command`，可[读取日志](<#增量日志>)。 |
| `POST /commands/{id}/cancel` | 强制终止该命令的进程树，等待退出，保留记录和日志，返回 `204`。 |
| `DELETE /commands/{id}` | 强制终止该命令的进程树，等退出后删除记录和日志，返回 `204`。 |

全局 ID 独立从 `0` 编号，本轮 Runner 内不复用。命令完成后继续保留记录和日志，直到删除命令或 Runner 退出。

### 命令状态过滤

全局和项目的命令列表都可用 `?status=…`。省略时照旧返回全部；没有匹配项时返回 `200` 和空列表。参数区分大小写，仅接受下列值：

| 值 | 含义 |
|---|---|
| `active` | 尚未结束：`preparing`、`running` 或 `cancelling`。仅用于筛选，不是任务状态。 |
| `preparing` | 已受理，正在校验或启动。 |
| `running` | 子进程已启动，任务还未结束。 |
| `cancelling` | 已请求强制终止，尚未结束。 |
| `succeeded` | 已成功结束。 |
| `failed` | 已失败，包括校验或启动失败。 |
| `cancelled` | 已因取消而结束。 |

### 取消与删除命令

两类命令的 `/cancel` 都不接收查询参数或正文，也不发送优雅退出信号。未结束的命令在强制终止期间为 `cancelling`，退出后为 `cancelled`。对已有终态（成功、失败、已取消）的命令再调用 `/cancel`，仍返回 `204`，不修改状态、结束时间或日志。`DELETE` 同样使用强制终止，但会删除记录和日志；命令已删除或从未存在时，查询、取消和删除均返回 `404`。

## 主机状态

`GET /health` 返回 `200` 和以下字段：

| 字段 | 含义 |
|---|---|
| `cpu` | `logical_cores` 为逻辑核数。`usage_percent` 为最近约 1 秒的使用率，`usage_5s_percent`、`usage_60s_percent` 为相应窗口的时间加权平均。后台采样不受查询频率影响；启动初期按已有样本计算，没有有效样本时为 `null`。 |
| `memory` / `disk` | `{"total_bytes":…, "available_bytes":…}`，分别是物理内存和项目数据目录所在卷的容量。 |
| `uptime_seconds` | Runner 进程已运行的秒数。 |
| `run_id` | 本轮 Runner 的 32 位小写十六进制随机指纹。每次重启重新生成，不是机器标识或令牌。跨轮核对命令 ID 或重试提交前先检查它。 |

## 任务响应与日志

### 提交与等待

Action、项目命令和全局命令的 POST 都可传 `timeout`：非负有限秒数，可带小数，默认 `10`，`0` 不等待。它只限制 POST 等待时间（含任务准备），不限制任务运行时长。任务结束返回 `200` 和终态；仍未结束返回 `202`，任务继续。`200` 不代表成功：检查 `status` 和 `error`。请求参数错误等仍按 HTTP 错误返回。

### 命令请求与响应

| POST 字段 | 含义 |
|---|---|
| `mode` | string；`cmd`（Windows 命令提示符）或 `ps`（Windows PowerShell）。 |
| `command` | string；一段 shell 命令，不接受数组。`ps` 支持多行，JSON 中用 `\n` 传输换行。 |
| `timeout`（可选） | number；见[提交与等待](<#提交与等待>)。 |

命令提交被接收后先分配 ID，再校验正文。正文校验失败时仍返回 `400 invalid_request`，并附 `command` 状态快照（`phase: validate`，`mode: null`）；可按其中的 `id` 查询或删除该记录。幂等重放不会分配新 ID。

`Command` 是 `Task` 加 `id` 和 `mode`。`id` 为小写 36 进制字符串（`0` 至 `z`，然后 `10` 等）。两种命令均非交互运行，可以自行 `cd`；一次命令改变的当前目录不会传给下一次。

全局和项目的单条命令 GET 都可加 `include_input=1`，额外返回 `command`（提交时的完整字符串，含原有换行）。单条 Action GET 加同一参数时额外返回 `action_id`，可用来识别 Action 是否被替换。该参数可与日志读取参数一起使用，只接受值 `1`。默认响应、列表和服务日志不包含命令原文；原文可能含敏感信息，按需读取。

PowerShell 不加载用户配置，默认关闭进度提示，并以 UTF-8（无 BOM）向管道写入文本。可用 `$ProgressPreference = 'Continue'` 恢复进度。PowerShell 向原生程序传递管道文本时也使用 UTF-8；原生子程序自己的输出可能使用其他编码，Runner 不转码、不剥除 CLIXML。若执行策略禁止加载本地模块，在 [config.py](<../winrunner/config.py>) 中把 `PS_SUPPRESS_PROGRESS` 设为 `False` 可跳过这些默认设置，无需放宽系统策略。内联命令受 Windows 命令行长度限制；PowerShell 按编码后的命令行计，超过上限时报告 `The command line is too long.`。

### Task 字段

| 字段 | 含义 |
|---|---|
| `status` | `preparing`、`running`、`cancelling`、`succeeded`、`failed`、`cancelled`；后三项为终态。 |
| `phase` | Action：`reset`、`validate`、`fetch`、`resolve`、`checkout`、`launch`、`run`；命令：`validate`、`launch`、`run`。终态保留最后阶段。 |
| `output_source` | `null`（尚无来源）、`runner`（Runner 诊断）、`git`（Git 失败）、`launch`（启动失败）、`process`（进程输出，包括解释器报错）。成功的 Git 准备输出不混入脚本日志。 |
| `started_at` | Runner 创建任务记录的时间，含准备阶段，不等子进程启动。Unix 时间戳，单位为毫秒，类型为整数，不受时区影响。 |
| `finished_at` | 任务进入成功、失败或取消终态的时间，同为 Unix 毫秒时间戳；未结束时为 `null`。 |
| `exit_code` | 主进程退出码（integer），未取得时为 `null`。 |
| `error` | 失败或取消时的诊断 `{"code":"…","message":"…"}`，没有则为 `null`。 |
| `stdout_bytes` / `stderr_bytes` | 两路日志当前各自保存的字节数。 |

### 增量日志

Runner 分开保存 stdout / stderr 原始字节，写入管道后即可轮询，不等换行或退出。Python Action 以 `-u` 启动；其他程序自行处理缓冲。日志只存在内存中，不限量、不自动淘汰。

单任务 GET 默认返回 `Task` 及对应的 Action 或命令字段，不附日志正文。传 `stdout_offset`、`stderr_offset`（从 0 开始的非负字节偏移）可分别读取对应流；两路可一起取。`limit` 是每路每次的上限，默认 `65536`，取值 `1` 至 `1048576`。每路以 `{"data_base64":"…","next_offset":123}` 返回。下一次从 `next_offset` 读；到当前末尾时数据为空、偏移不变，偏移越过末尾返回 `400`。Action 被下一轮替换后，旧偏移不可续用。

用 `stdout_tail=N` 或 `stderr_tail=N` 读取指定流当前最后 N 个原始字节（1 至 1048576，不足则返回全部）。日志与长度来自同一次快照，响应形状不变，`next_offset` 指向当时的末尾，可接续增量读取。两路可以混用尾部和偏移，但同一路不能同时指定 `_tail` 和 `_offset`（`400`）；`limit` 只限制按偏移读取的流。尾部按字节截取，不保证对齐行或字符。

## 幂等重试

Action、项目命令和全局命令的 POST 都可带 `Idempotency-Key`（1 至 128 个 ASCII 字符，只允许字母、数字和 `._~-`）。每次新提交用新键。网络中断后，确认目标 Runner 和 `run_id` 未变，再用原键和原请求体重试；项目命令还须确认项目未删除重建。

- 同键、同请求体重放首次提交的 HTTP 状态和响应，不再启动任务；首次请求错误也会记录。JSON 空白和对象键顺序不影响匹配，字段和值须一致（包括 `timeout`）。无法解析的请求体按原始字节比较。
- 同键、不同请求体返回 `409 idempotency_conflict`。首次提交仍在生成响应时，同参数重试返回 `409 idempotency_pending`，稍后用原键重试。重放的是首次快照；当前状态和日志用 GET 查看，已删除或覆盖的结果不会恢复。
- 全局命令、按项目名区分的 Action、按项目实例区分的命令各有独立作用域。项目名不区分大小写。找不到项目实例的 `404` 不会记录项目命令键。同名项目重建后，项目命令的旧键不会重放旧实例的结果；删除单条命令不会释放旧键。
- 所有作用域合计最多 4096 条记录，已删除项目的旧记录也占容量。满额时新键返回 `503 idempotency_capacity`，仍可访问的已有键可继续重放。Runner 重启清空记录。
