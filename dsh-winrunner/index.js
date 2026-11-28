import { setTimeout as delay } from 'node:timers/promises'
import { ClientError, limits, requestRunner } from './client.js'

export const inject = ['tools']
const modes = ['cmd', 'ps']
const encodings = ['utf8', 'base64']
const projectName = /^[a-z\d][a-z\d_-]{0,63}$/i
const commandId = /^[a-z\d_-]{1,128}$/i
const hex = /^[a-f\d]{1,64}$/i
const reserved = /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/i
const choice = (description, choices) => ({ type: 'string', description, enum: choices })
const taskFlow = '长任务前用 winrunner_project 的 health 查一次。看 outcome 判定结果；有 next_wait 或 next_get 就原样传回同一工具。pending 或 unknown 时先查询，别重复 start。'
const taskOptions = `start/wait 可传 wait_seconds（start 默认 0，wait 默认 15 且至少 1；都至多 60）。到点不停止任务，wait 不重提。start/get/wait 可传 status_only:true 不读日志。get/wait 默认从头预览双路；可按流传 stdout_offset / stderr_offset（从偏移读，limit 限制页长）或 stdout_tail / stderr_tail（最后 N 字节），同一路不能混用，只传一路就只读该路。日志页长最大 ${limits.log} 字节；encoding 用 utf8（默认，至少 4 字节）或 base64（无损）。`
const startKeyHelp = 'start 可传 idempotency_key（1–128 个字母、数字或 ._~-）；新提交用新键，只有确认同一 Runner 进程后，才可用原键和原参数（包括 wait_seconds）恢复断线提交。重放仅返回原提交快照，当前状态用 get 查询。'
const logKeys = ['stdout_offset', 'stderr_offset', 'stdout_tail', 'stderr_tail', 'limit', 'encoding']
const taskLogKeys = ['status_only', 'include_input', ...logKeys]
const terminal = new Set(['succeeded', 'failed', 'cancelled'])
const active = new Set(['preparing', 'running', 'cancelling'])
const commandStatuses = ['active', ...active, ...terminal]
const streams = ['stdout', 'stderr']
const exampleCall = (op, params = {}) => JSON.stringify({ op, params_json: JSON.stringify(params) })
const cpuMetric = description => ({ description: `${description} 数字百分比，或 null。` })
const cpuOutput = {
  type: 'object',
  description: 'health 返回的整机 CPU 指标，百分比为 0–100；缺少有效采样时为 null。',
  properties: {
    logical_cores: { type: 'integer', description: '逻辑核数。' },
    usage_percent: cpuMetric('瞬时 CPU 使用率：最近约 1 秒的样本。'),
    usage_5s_percent: cpuMetric('最近 5 秒的时间加权平均 CPU 使用率。'),
    usage_60s_percent: cpuMetric('最近 60 秒的时间加权平均 CPU 使用率。'),
  },
}
const groups = {
  project: {
    description: '管理 WinRunner 项目、读取目录或文件。health 的 cpu.usage_percent / usage_5s_percent / usage_60s_percent 分别是瞬时（约 1 秒）、5 秒和 60 秒整机 CPU 使用率（0–100；无样本为 null）。delete 会停止项目并删除其文件和缓存。',
    ops: ['health', 'list', 'get', 'create', 'delete', 'files'],
    params: `health/list：传空字符串或 "{}"，如 ${exampleCall('health')}。get/delete/files：{"name":"demo"}。create：{"name":"demo","repo_url":"https://gitee.com/owner/repo.git"}（无凭据、查询或片段的公开 HTTPS Git 地址）。files 有 next_get 就原样续页；可传 path（相对路径，省略列根目录）、offset（文件字节偏移）、length（默认 4096，最大 ${limits.file}）、entry_offset / entry_limit（服务端目录分页，默认 100，最多 1000）、encoding（utf8/base64；UTF-8 每页至少 4 字节，无损用 base64）。`,
  },
  action: {
    description: `提交、查询或取消项目的 Git Action。start 覆盖上轮工作区和日志；忙时返回 409。先取完旧日志和产物再提交下一轮。${taskFlow}`,
    ops: ['start', 'get', 'wait', 'cancel'],
    params: `start：{"name":"demo","commit":"abc1234","interpreter":"python","script":"task.py","args":["--help"],"wait_seconds":5}。commit 用远端可达的完整十六进制哈希或唯一前缀，不用分支或标签；script 是仓库内相对路径；args 可省略，逐项传递，无需 shell 引号。get/wait/cancel：{"name":"demo"}。get/wait 可用 include_input:true 查询本轮 action_id。${taskOptions}${startKeyHelp}`,
  },
  command: {
    description: `运行和管理项目内或全局的 Windows 命令。项目命令不重置工作树，可与 Action 并行。cancel 强制终止进程树并保留记录和日志；delete 强制终止并删除。${taskFlow}`,
    ops: ['start', 'get', 'wait', 'list', 'cancel', 'delete'],
    params: `所有操作可传 name 指定项目，省略则用全局。list 可传 status：active 表示未结束（preparing/running/cancelling），也可精确匹配这三个状态或 succeeded/failed/cancelled；省略列全部。start：{"name":"demo","mode":"ps","command":"Write-Output 'hello'"}，mode 用 ps 或 cmd。get/wait/cancel/delete 用同一作用域的 id，如 {"name":"demo","id":"0"}。get/wait 可用 include_input:true 读取原命令，注意其中可能含敏感信息。${taskOptions}${startKeyHelp}项目删后重建时，不用旧键恢复提交。`,
  },
}

const invalid = message => { throw new ClientError('invalid_input', message) }
const clean = value => typeof value === 'string' && !/[\u0000\p{Surrogate}]/u.test(value)
const text = (value, key, allowEmpty = false) => {
  if (!clean(value) || (!allowEmpty && !value.length)) invalid(`${key} 必须是有效${allowEmpty ? '' : '且非空的'}字符串。`)
  return value
}
const enumValue = (value, key, choices) => {
  if (!choices.includes(value)) invalid(`${key} 必须是 ${choices.join(' 或 ')}。`)
  return value
}
const nonnegative = (value, key, lower, upper) => {
  if (!Number.isSafeInteger(value) || value < lower || value > upper) invalid(`${key} 必须是 ${lower}–${upper} 的整数。`)
  return value
}
const select = (obj, keys) => {
  for (const key of Object.keys(obj)) if (key !== 'op' && !keys.includes(key)) invalid(`当前操作不接受 ${key}。`)
}
const match = (value, regex, key) => {
  if (typeof value !== 'string' || /[\r\n\u2028\u2029]/.test(value) || !regex.test(value)) invalid(`${key} 格式无效。`)
  return value
}
const projectPath = value => {
  const valid = match(value, projectName, 'name')
  if (reserved.test(valid)) invalid('name 不能是 Windows 设备名。')
  return `/projects/${encodeURIComponent(valid)}`
}
const request = (method, path, extra) => ({ method, path, ...extra })
const queryPath = (path, query) => query.size ? `${path}?${query}` : path

function waitSeconds(args) {
  const minimum = args.op === 'wait' ? 1 : 0
  const seconds = args.wait_seconds === undefined ? (minimum ? 15 : 0) : args.wait_seconds
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < minimum || seconds > 60) {
    invalid(`wait_seconds 必须是 ${minimum}–60 之间的秒数。`)
  }
  return seconds
}

function validateStatusOnly(args) {
  if (args.status_only !== undefined && typeof args.status_only !== 'boolean') invalid('status_only 必须是布尔值。')
}

function pageBytes(value, key, fallback, max, encoding) {
  if (encoding === 'utf8' && Number.isSafeInteger(value) && value >= 1 && value < 4) {
    invalid(`${key} 使用 UTF-8 时至少为 4 字节；更小的逐字节读取请用 base64。`)
  }
  return nonnegative(value === undefined ? fallback : value, key, encoding === 'utf8' ? 4 : 1, max)
}

function startOptions(args, timeout) {
  const key = args.idempotency_key
  if (key !== undefined && (typeof key !== 'string' || key.length < 1 || key.length > 128 || /[^A-Za-z0-9._~-]/.test(key))) {
    invalid('idempotency_key 必须是 1–128 个字母、数字或 ._~-。')
  }
  return { idempotencyKey: key, deadline: Math.ceil((timeout + 20) * 1000) }
}

function getTask(path, args, identifyAction = false) {
  validateStatusOnly(args)
  const params = new URLSearchParams()
  if (args.include_input !== undefined && typeof args.include_input !== 'boolean') invalid('include_input 必须是布尔值。')
  if (identifyAction || args.include_input) params.set('include_input', 1)
  if (args.status_only) {
    if (logKeys.some(key => args[key] !== undefined)) invalid('status_only 为 true 时不要传日志参数。')
    return request('GET', queryPath(path, params))
  }
  const logEncoding = enumValue(args.encoding === undefined ? 'utf8' : args.encoding, 'encoding', encodings)
  const hasOffsets = streams.some(stream => args[`${stream}_offset`] !== undefined)
  const hasTails = streams.some(stream => args[`${stream}_tail`] !== undefined)
  for (const stream of streams) {
    const offset = `${stream}_offset`
    const tail = `${stream}_tail`
    if (args[offset] !== undefined && args[tail] !== undefined) invalid(`${offset} 和 ${tail} 不能混用。`)
    if (args[offset] !== undefined) params.set(offset, nonnegative(args[offset], offset, 0, Number.MAX_SAFE_INTEGER))
    if (args[tail] !== undefined) params.set(tail, pageBytes(args[tail], tail, undefined, limits.log, logEncoding))
    if (!hasOffsets && !hasTails) params.set(offset, 0)
  }
  if (!hasTails || hasOffsets) {
    params.set('limit', pageBytes(args.limit, 'limit', hasOffsets ? 4096 : 2048, limits.log, logEncoding))
  } else if (args.limit !== undefined) {
    invalid('limit 只适用于按偏移读取的日志。')
  }
  return request('GET', queryPath(path, params), { logEncoding })
}

function forProject(args) {
  const op = args.op
  if (op === 'health' || op === 'list') {
    select(args, [])
    return request('GET', op === 'health' ? '/health' : '/projects')
  }
  const path = projectPath(args.name)
  if (op === 'create') {
    select(args, ['name', 'repo_url'])
    let repoUrl
    try {
      const raw = text(args.repo_url, 'repo_url')
      const url = new URL(raw)
      if (!/^https:\/\//.test(raw) || url.protocol !== 'https:' || !url.hostname || url.username || url.password ||
          /[?#\\\s]/.test(raw) || !/^https:\/\/[^/]+\/.+/.test(raw) || url.port === '0' || url.href.split('/', 3)[2].includes('@')) throw new Error()
      repoUrl = raw
    } catch {
      invalid('repo_url 必须是无凭据、查询或片段的 HTTPS Git 最终地址。')
    }
    return request('POST', path, { body: { repo_url: repoUrl }, deadline: 120000 })
  }
  if (op === 'files') {
    select(args, ['name', 'path', 'offset', 'length', 'entry_offset', 'entry_limit', 'encoding'])
    const explicitDir = args.entry_offset !== undefined || args.entry_limit !== undefined
    const explicitFile = ['offset', 'length', 'encoding'].some(key => args[key] !== undefined)
    if (explicitDir && explicitFile) invalid('目录分页参数不能和文件字节分页参数混用。')
    const relative = args.path === undefined ? '' : text(args.path, 'path', true)
    const params = new URLSearchParams()
    if (relative) params.set('path', relative)
    const offset = nonnegative(args.offset === undefined ? 0 : args.offset, 'offset', 0, Number.MAX_SAFE_INTEGER - limits.file)
    const encoding = enumValue(args.encoding === undefined ? 'utf8' : args.encoding, 'encoding', encodings)
    const length = pageBytes(args.length, 'length', 4096, limits.file, encoding)
    const entryLimit = nonnegative(args.entry_limit === undefined ? limits.entries : args.entry_limit, 'entry_limit', 1, 1000)
    const entryOffset = nonnegative(args.entry_offset === undefined ? 0 : args.entry_offset, 'entry_offset', 0, Number.MAX_SAFE_INTEGER - entryLimit)
    const file = { offset, length, entryOffset, entryLimit, encoding, explicitFile }
    const plain = request('GET', queryPath(`${path}/files`, params), { file })
    if (explicitFile) return plain
    params.set('entry_offset', entryOffset)
    params.set('entry_limit', entryLimit)
    return { ...plain, path: queryPath(`${path}/files`, params),
      ...(!explicitDir && relative && relative !== '.' ? { fileFallback: plain } : {}) }
  }
  select(args, ['name'])
  return op === 'delete' ? request('DELETE', path, { deadline: 30000 }) : request('GET', path)
}

function forAction(args) {
  const path = `${projectPath(args.name)}/actions`
  if (args.op === 'start') {
    select(args, ['name', 'commit', 'interpreter', 'script', 'args', 'wait_seconds', 'status_only', 'idempotency_key'])
    validateStatusOnly(args)
    const commit = match(args.commit, hex, 'commit')
    const interpreter = text(args.interpreter, 'interpreter')
    const script = text(args.script, 'script')
    const argv = args.args === undefined ? [] : args.args
    if (!Array.isArray(argv) || !argv.every(clean)) invalid('args 必须是字符串数组。')
    const timeout = waitSeconds(args)
    return request('POST', path, { body: { commit, interpreter, script, args: argv, timeout }, ...startOptions(args, timeout) })
  }
  if (args.op === 'get' || args.op === 'wait') {
    select(args, ['name', ...taskLogKeys, ...(args.op === 'wait' ? ['wait_seconds'] : [])])
    return getTask(path, args, args.op === 'wait')
  }
  select(args, ['name'])
  return request('DELETE', path, { deadline: 30000 })
}

function forCommand(args) {
  const collection = args.name === undefined ? '/commands' : `${projectPath(args.name)}/commands`
  if (args.op === 'list') {
    select(args, ['name', 'status'])
    const query = new URLSearchParams()
    if (args.status !== undefined) query.set('status', enumValue(args.status, 'status', commandStatuses))
    return request('GET', queryPath(collection, query))
  }
  if (args.op === 'start') {
    select(args, ['name', 'mode', 'command', 'wait_seconds', 'status_only', 'idempotency_key'])
    validateStatusOnly(args)
    const mode = enumValue(args.mode, 'mode', modes)
    const command = text(args.command, 'command')
    const timeout = waitSeconds(args)
    return request('POST', collection, { body: { mode, command, timeout }, ...startOptions(args, timeout) })
  }
  const path = `${collection}/${encodeURIComponent(match(args.id, commandId, 'id'))}`
  if (args.op === 'get' || args.op === 'wait') {
    select(args, ['name', 'id', ...taskLogKeys, ...(args.op === 'wait' ? ['wait_seconds'] : [])])
    return getTask(path, args)
  }
  select(args, ['name', 'id'])
  return args.op === 'cancel'
    ? request('POST', `${path}/cancel`, { deadline: 30000 })
    : request('DELETE', path, { deadline: 30000 })
}

const builders = { project: forProject, action: forAction, command: forCommand }
const readOnly = new Set(['health', 'list', 'get', 'wait', 'files'])
const pollInterval = 1000
const transientWaitErrors = new Set(['request_timeout', 'transport_error', 'wait_expired'])

function taskTarget(group, args, body) {
  if (group === 'action') return { name: args.name }
  const command = args.id ?? body.id
  if (typeof command !== 'string' || !commandId.test(command)) return null
  return args.name === undefined ? { id: command } : { name: args.name, id: command }
}

function taskHints(args, body, target, outcome) {
  if (!target || ['task_changed', 'runner_changed'].includes(body.logs_error?.code)) return
  if (body.logs_error || outcome === 'unknown') {
    body.next_get = { op: 'get', ...target }
    return
  }
  const pagination = {}
  if (args.status_only) pagination.status_only = true
  else {
    for (const stream of streams) {
      const chunk = body[stream]
      if (chunk && chunk.remaining !== 'complete') pagination[`${stream}_offset`] = chunk.next_offset
    }
    for (const key of ['limit', 'encoding']) {
      if (args[key] !== undefined) pagination[key] = args[key]
    }
  }
  if (streams.some(stream => body[stream]?.remaining === 'available')) {
    body.next_get = { op: 'get', ...target, ...pagination }
  } else if (outcome === 'pending') {
    body.next_wait = { op: 'wait', ...target, ...pagination }
  }
}

async function withStartLogs(group, args, response, signal, call) {
  if (!response.ok || args.status_only || !terminal.has(response.status) ||
      !(response.stdout_bytes > 0 || response.stderr_bytes > 0)) return response
  const target = taskTarget(group, args, response)
  if (!target) return { ...response, logs_error: { code: 'missing_task_id', message: '未收到命令 ID，无法查询日志。' } }
  try {
    const preview = group === 'action' && typeof response.action_id === 'string'
      ? getTask(`${projectPath(target.name)}/actions`, {}, true)
      : builders[group]({ op: 'get', ...target })
    const logged = await requestRunner({ ...preview, deadline: 4000 }, signal, call)
    if (!logged.ok) {
      return { ...response, logs_error: { ...logged.error, http_status: logged.http_status } }
    }
    const fields = ['status', 'phase', 'commit', 'id', 'exit_code', 'output_source', 'stdout_bytes', 'stderr_bytes']
    if ((typeof response.action_id === 'string' && logged.action_id !== response.action_id) ||
        fields.some(key => logged[key] !== response[key]) ||
        logged.error?.code !== response.error?.code || logged.error?.message !== response.error?.message) {
      throw new ClientError('task_changed', '补读时当前任务已不同，没有合并日志。')
    }
    if (streams.some(stream => !logged[stream])) throw new ClientError('invalid_response', 'WinRunner 未返回请求的两路日志。')
    return { ...response, stdout: logged.stdout, stderr: logged.stderr }
  } catch (error) {
    if (!(error instanceof ClientError)) throw error
    return { ...response, logs_error: errorDetails(error) }
  }
}

async function waitForTask(spec, seconds, signal, call, allowIdentityFallback) {
  const budget = AbortSignal.timeout(Math.ceil(seconds * 1000))
  const polling = signal ? AbortSignal.any([signal, budget]) : budget
  const interval = Math.min(pollInterval, seconds * 500)
  let last
  try {
    while (!polling.aborted) {
      let response = await requestRunner(spec, polling, call)
      if (allowIdentityFallback && !last && response.http_status === 400 && response.error?.code === 'invalid_request' &&
          response.error.message === 'Unexpected query parameter' && spec.path.includes('?include_input=1')) {
        // Older Runner versions do not expose the Action identity query.
        const path = spec.path.replace(/\?include_input=1(?:&|$)/, match => match.endsWith('&') ? '?' : '')
        spec = { ...spec, path }
        response = await requestRunner(spec, polling, call)
      }
      if (!response.ok) {
        if (typeof last?.action_id === 'string' && response.error?.code === 'invalid_offset') {
          try {
            const path = spec.path.split('?', 1)[0] + '?include_input=1'
            const current = await requestRunner({ ...spec, path, logEncoding: undefined }, polling, call)
            if (current.ok && typeof current.action_id === 'string' && current.action_id !== last.action_id) {
              throw new ClientError('task_changed', '等待期间项目的 Action 已被替换；未把新一轮当作原任务。')
            }
          } catch (error) {
            if (!(error instanceof ClientError) || signal?.aborted ||
                ['task_changed', 'runner_changed'].includes(error.code)) throw error
            // A failed diagnostic probe must not hide the original offset error.
          }
        }
        return response
      }
      const replaced = typeof last?.action_id === 'string' && response.action_id !== last.action_id
      if (replaced || (last?.commit && response.commit !== last.commit)) {
        throw new ClientError('task_changed', '等待期间项目的 Action 已被替换；未把新一轮当作原任务。')
      }
      if (!active.has(response.status)) return response
      last = response
      await delay(interval, undefined, { signal: polling })
    }
  } catch (error) {
    if (signal?.aborted) throw new ClientError('cancelled', '已取消本地等待；远端任务继续。')
    if (!budget.aborted || !(error.name === 'AbortError' || error.code === 'cancelled')) {
      if (last && transientWaitErrors.has(error.code)) return { ...last, wait_error: errorDetails(error) }
      throw error
    }
  }
  if (signal?.aborted) throw new ClientError('cancelled', '已取消本地等待；远端任务继续。')
  if (!last) throw new ClientError('wait_expired', '等待期间没有取得任务状态；可以再查询，远端任务未被停止。')
  return { ...last, wait_expired: true }
}

function errorDetails(error) {
  return {
    code: error.code,
    message: error.message,
    ...(error.httpStatus === undefined ? {} : { http_status: error.httpStatus }),
  }
}

function toolResult(group, args, response) {
  const { ok, outcome: _serverOutcome, ...body } = response
  if (args.op === 'start' && args.idempotency_key !== undefined) {
    body.idempotency_key = args.idempotency_key
    if (ok) body.note = '这是首次提交的状态快照（重放也一样）；查询当前状态或日志请用 get。'
    if (body.error?.code === 'idempotency_pending') body.note = '同键的首次提交尚未返回；仅在同一 Runner 进程中稍后用原参数重试。'
  }
  if (!ok) {
    if (group === 'command' && args.op === 'cancel' && body.http_status === 404 &&
        body.error?.code === 'not_found' && body.error.message === 'Route not found') {
      body.note = '此 Runner 不支持仅取消；请升级服务端。没有尝试 delete。'
    }
    if (body.http_status === 400 && body.error?.code === 'invalid_request' &&
        /^Unexpected query parameters?$/.test(body.error.message)) {
      if (group === 'command' && args.op === 'list' && args.status !== undefined) {
        body.note = '此 Runner 可能尚不支持按状态筛选；请升级服务端，或显式去掉 status 查全部。'
      } else if (streams.some(stream => args[`${stream}_tail`] !== undefined)) {
        body.note = '此 Runner 可能尚不支持日志尾读；请升级服务端，或先查字节数再按偏移读取。'
      }
    }
    if (group === 'action' && args.op === 'start' && (body.action ||
        ['invalid_request', 'worker_unavailable', 'worker_unavailable_reset_incomplete'].includes(body.error?.code))) {
      body.next_get = { op: 'get', name: args.name }
      body.note = '此次提交可能已经替换了 Action 槽；先查看当前状态，再考虑重试。'
    }
    if (group === 'command' && args.op === 'start' && body.command) {
      const target = taskTarget(group, args, body.command)
      if (target) body.next_get = { op: 'get', ...target }
    }
    return { outcome: 'request_error', ...body }
  }
  if (group === 'project') {
    if (args.op === 'files') {
      const { offset, entry_offset, entry_limit, ...params } = args
      if (body.kind === 'file' && Number.isSafeInteger(body.next_offset)) {
        body.next_get = { ...params, offset: body.next_offset }
      } else if (body.kind === 'directory' && Number.isSafeInteger(body.next_entry_offset)) {
        body.next_get = { op: args.op, name: args.name, ...(args.path === undefined ? {} : { path: args.path }),
          ...(entry_limit === undefined ? {} : { entry_limit }), entry_offset: body.next_entry_offset }
      }
    }
    return { outcome: 'succeeded', ...body }
  }
  if (group === 'command' && args.op === 'cancel') {
    const target = taskTarget(group, args, body)
    return { outcome: 'succeeded', ...body, next_get: { op: 'get', ...target, status_only: true } }
  }
  if (!['start', 'get', 'wait'].includes(args.op)) return { outcome: 'succeeded', ...body }

  const outcome = terminal.has(body.status) ? body.status : active.has(body.status) ? 'pending' : 'unknown'
  const target = taskTarget(group, args, body)
  taskHints(args, body, target, outcome)
  if (args.op === 'start' && args.idempotency_key !== undefined && target && terminal.has(body.status) &&
      !args.status_only && (body.stdout_bytes > 0 || body.stderr_bytes > 0)) body.next_get = { op: 'get', ...target }
  return { outcome, ...body }
}

function unpackArgs(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) invalid('参数必须是对象。')
  if (!Object.hasOwn(raw, 'params_json')) return raw // Compatibility for direct callers.
  select(raw, ['params_json'])
  if (typeof raw.params_json !== 'string') invalid('params_json 必须是 JSON 对象字符串。')
  let payload
  try { payload = JSON.parse(raw.params_json.trim() || '{}') } catch {
    invalid('params_json 不是有效 JSON；请传 JSON 对象字符串，例如 {"name":"demo"}。')
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload) || Object.hasOwn(payload, 'op')) {
    invalid('params_json 必须是 JSON 对象，op 只能在外层。')
  }
  return { ...payload, op: raw.op }
}

function packHints(result) {
  for (const key of ['next_get', 'next_wait']) {
    if (!result[key]) continue
    const { op, ...params } = result[key]
    result[key] = { op, params_json: JSON.stringify(params) }
  }
  return result
}

function definition(group, { description, ops, params }) {
  return {
    name: `winrunner_${group}`,
    description,
    parameters: {
      type: 'object', additionalProperties: false,
      properties: {
        op: choice('选择操作。', ops),
        params_json: { type: 'string', description: `填 JSON 对象字符串，仅含该操作参数，不重复写 op。${params}` },
      }, required: ['op', 'params_json'],
    },
    output: {
      schema: {
        type: 'object', additionalProperties: true, required: ['outcome'],
        properties: {
          outcome: choice('操作或任务结果；HTTP 状态只用于诊断。', ['succeeded', 'failed', 'cancelled', 'pending', 'request_error', 'unknown']),
          ...(group === 'project' ? { cpu: cpuOutput } : {
            started_at: { type: 'integer', description: '任务记录创建时的 Unix 毫秒时间戳。' },
            finished_at: { oneOf: [{ type: 'integer' }, { type: 'null' }], description: '任务结束时的 Unix 毫秒时间戳；未结束为 null。' },
          }),
        },
      },
      render(_args, value) { return [{ type: 'text', text: JSON.stringify(value, null, 2) }] },
    },
    timeoutMs: 130000,
    finalizeContent(exec, result) {
      // DSH can replace execute's return value with a canonical abort after dispatch.
      if (result.error?.info?.code === 'ABORTED' && ['start', 'create', 'delete', 'cancel'].includes(exec.args?.op)) {
        return [...result.content, { type: 'text', text: '本地工具调用已取消，远端写操作可能已经生效。先在同一台 Runner 查询状态，不要盲目重试。' }]
      }
    },
    isConcurrencySafe: args => readOnly.has(args?.op),
    async execute(raw, exec) {
      let args
      const format = raw && Object.hasOwn(raw, 'params_json') ? packHints : result => result
      try {
        args = unpackArgs(raw)
        enumValue(args.op, 'op', ops)
        const spec = builders[group](args)
        const signal = exec?.signal
        const call = {}
        let response = args.op === 'wait'
          ? await waitForTask(spec, waitSeconds(args), signal, call,
            group === 'action' && !args.include_input && !streams.some(stream => args[`${stream}_tail`] !== undefined))
          : await requestRunner(spec, signal, call)
        if (spec.fileFallback && response.http_status === 400 && response.error?.code === 'invalid_request' &&
            response.error.message === 'entry_offset and entry_limit are only for directories') {
          response = await requestRunner(spec.fileFallback, signal, call)
        }
        if (args.op === 'start' && spec.idempotencyKey === undefined) {
          response = await withStartLogs(group, args, response, signal, call)
        }
        return format(toolResult(group, args, response))
      } catch (error) {
        if (!(error instanceof ClientError)) throw error
        const { wait_seconds, ...resume } = args ?? {}
        return format({
          outcome: error.outcomeUnknown ? 'unknown' : 'request_error',
          error: { code: error.code, message: error.message },
          ...(error.httpStatus === undefined ? {} : { http_status: error.httpStatus }),
          ...(error.retryAfter === undefined ? {} : { retry_after_seconds: error.retryAfter }),
          ...(error.outcomeUnknown ? { outcome_unknown: true } : {}),
          ...(args?.op === 'start' && error.outcomeUnknown && args.idempotency_key !== undefined ? {
            idempotency_key: args.idempotency_key,
            note: '仅确认 Runner 未重启且地址未变后，才用原键和原参数重试；不要换键重试同一次提交。',
          } : {}),
          ...(args?.op === 'wait' && transientWaitErrors.has(error.code) ? { next_wait: resume } : {}),
        })
      }
    },
  }
}

export function apply(ctx) {
  ctx.effect(() => {
    const disposers = []
    const dispose = () => {
      for (const remove of disposers.splice(0).reverse()) remove()
    }
    try {
      for (const [name, group] of Object.entries(groups)) disposers.push(ctx.tools.register(definition(name, group)))
      return dispose
    } catch (error) {
      dispose()
      throw error
    }
  })
}
