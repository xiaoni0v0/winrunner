import { setTimeout as delay } from 'node:timers/promises'
import { ClientError, limits, requestRunner } from './client.js'

export const inject = ['tools']
const modes = ['cmd', 'ps']
const encodings = ['utf8', 'base64']
const projectName = /^[a-z\d][a-z\d_-]{0,63}$/i
const commandId = /^[a-z\d_-]{1,128}$/i
const hex = /^[a-f\d]{1,64}$/i
const reserved = /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/i
const string = description => ({ type: 'string', description })
const integer = description => ({ type: 'integer', description })
const choice = (description, choices) => ({ type: 'string', description, enum: choices })
const name = string('项目注册名。')
const id = string('start 返回的命令 ID。')
const wait = { type: 'number', description: 'start：等待 0–60 秒，默认 0；wait：等待 1–60 秒，默认 15。结束就返回，等满也不会停止任务。' }
const taskQuery = {
  status_only: { type: 'boolean', description: 'start、get、wait：只返回任务状态，不请求日志。默认 false。' },
  stdout_offset: integer('get、wait：用 stdout.next_offset 续读。两个偏移都省略则预览两路；只传一路则只读那一路。'),
  stderr_offset: integer('get、wait：用 stderr.next_offset 续读；只传这一路就只读 stderr。'),
  limit: integer(`get、wait：每路最多读取的字节数；默认预览 2048，指定偏移默认 4096，上限 ${limits.log}。`),
  encoding: choice('get、wait：默认 utf8；需要无损时用 base64。', encodings),
}
const groups = {
  project: {
    description: '管理 WinRunner 项目、读取项目目录或文件、查询主机健康。delete 会停止项目并删除其文件和缓存。',
    ops: ['health', 'list', 'get', 'create', 'delete', 'files'],
    properties: {
      name: string('get、create、delete、files 必填：项目注册名。'),
      repo_url: string('create 必填：无凭据、查询或片段的 HTTPS Git 最终地址。'),
      path: string('files：项目内相对路径；省略列根目录。'),
      offset: integer('files 读文件的字节偏移；用上次 next_offset 继续。'),
      length: integer(`files 每页文件字节数，默认 4096，上限 ${limits.file}。`),
      entry_offset: integer('files 列目录的条目偏移；用上次 next_entry_offset 继续。'),
      encoding: choice('files 文件编码，默认 utf8；二进制或无损读取用 base64。', encodings),
    },
  },
  action: {
    description: '提交、轮询或取消 WinRunner 项目的 Git Action。长任务前用 winrunner_project 的 health 查一次。start 覆盖上轮工作区和日志，忙时返回 409；pending 时用 wait，不要重复 start。',
    ops: ['start', 'get', 'wait', 'cancel'],
    properties: {
      name,
      commit: string('start 必填：远端可达的十六进制提交哈希或唯一前缀；优先传完整哈希。不接受分支或标签名。'),
      interpreter: string('start 必填：服务器解释器白名单中的别名，如 python 或 node。'),
      script: string('start 必填：检出目录内的脚本相对路径。'),
      args: { type: 'array', items: { type: 'string' }, description: 'start 可选：逐项传给脚本，不拼成 shell 命令，也不用给参数额外加引号。' },
      wait_seconds: wait,
      ...taskQuery,
    },
  },
  command: {
    description: '提交、列出、轮询或删除 WinRunner 临时 Windows 命令。长任务前用 winrunner_project 的 health 查一次。pending 时按 id 等待；delete 终止并丢弃记录和日志。',
    ops: ['start', 'get', 'wait', 'list', 'delete'],
    properties: {
      id,
      mode: choice('start 必填：优先用 ps（PowerShell）；仅在需要 CMD 语法时用 cmd。', modes),
      command: string('start 必填：直接给命令原文，不用再套一对引号或自行 Base64；ps 支持多行，cmd 仍遵守自身的引号和变量展开规则。'),
      wait_seconds: wait,
      ...taskQuery,
    },
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

function getTask(path, args) {
  validateStatusOnly(args)
  if (args.status_only) {
    if (['stdout_offset', 'stderr_offset', 'limit', 'encoding'].some(key => args[key] !== undefined)) {
      invalid('status_only 为 true 时不要传日志参数。')
    }
    return request('GET', path)
  }
  const params = new URLSearchParams()
  for (const key of ['stdout_offset', 'stderr_offset']) {
    if (args[key] !== undefined) params.set(key, nonnegative(args[key], key, 0, Number.MAX_SAFE_INTEGER))
  }
  const defaultLimit = params.size ? 4096 : 2048
  if (!params.size) {
    params.set('stdout_offset', 0)
    params.set('stderr_offset', 0)
  }
  params.set('limit', nonnegative(args.limit === undefined ? defaultLimit : args.limit, 'limit', 1, limits.log))
  const logEncoding = enumValue(args.encoding === undefined ? 'utf8' : args.encoding, 'encoding', encodings)
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
    return request('POST', '/projects', { body: { name: args.name, repo_url: repoUrl }, deadline: 120000 })
  }
  if (op === 'files') {
    select(args, ['name', 'path', 'offset', 'length', 'entry_offset', 'encoding'])
    const relative = args.path === undefined ? '' : text(args.path, 'path', true)
    const params = new URLSearchParams()
    if (relative) params.set('path', relative)
    const offset = nonnegative(args.offset === undefined ? 0 : args.offset, 'offset', 0, Number.MAX_SAFE_INTEGER - limits.file)
    const length = nonnegative(args.length === undefined ? 4096 : args.length, 'length', 1, limits.file)
    const entryOffset = nonnegative(args.entry_offset === undefined ? 0 : args.entry_offset, 'entry_offset', 0, Number.MAX_SAFE_INTEGER - limits.entries)
    const encoding = enumValue(args.encoding === undefined ? 'utf8' : args.encoding, 'encoding', encodings)
    return request('GET', queryPath(`${path}/files`, params), { file: { offset, length, entryOffset, encoding } })
  }
  select(args, ['name'])
  return op === 'delete' ? request('DELETE', path, { deadline: 30000 }) : request('GET', path)
}

function forAction(args) {
  const path = `${projectPath(args.name)}/actions`
  if (args.op === 'start') {
    select(args, ['name', 'commit', 'interpreter', 'script', 'args', 'wait_seconds', 'status_only'])
    validateStatusOnly(args)
    const commit = match(args.commit, hex, 'commit')
    const interpreter = text(args.interpreter, 'interpreter')
    const script = text(args.script, 'script')
    const argv = args.args === undefined ? [] : args.args
    if (!Array.isArray(argv) || !argv.every(clean)) invalid('args 必须是字符串数组。')
    const timeout = waitSeconds(args)
    return request('POST', path, { body: { commit, interpreter, script, args: argv, timeout }, deadline: (timeout + 20) * 1000 })
  }
  if (args.op === 'get' || args.op === 'wait') {
    select(args, ['name', ...Object.keys(taskQuery), ...(args.op === 'wait' ? ['wait_seconds'] : [])])
    return getTask(path, args)
  }
  select(args, ['name'])
  return request('DELETE', path, { deadline: 30000 })
}

function forCommand(args) {
  if (args.op === 'list') {
    select(args, [])
    return request('GET', '/commands')
  }
  if (args.op === 'start') {
    select(args, ['mode', 'command', 'wait_seconds', 'status_only'])
    validateStatusOnly(args)
    const mode = enumValue(args.mode, 'mode', modes)
    const command = text(args.command, 'command')
    const timeout = waitSeconds(args)
    return request('POST', '/commands', { body: { mode, command, timeout }, deadline: (timeout + 20) * 1000 })
  }
  const path = `/commands/${encodeURIComponent(match(args.id, commandId, 'id'))}`
  if (args.op === 'get' || args.op === 'wait') {
    select(args, ['id', ...Object.keys(taskQuery), ...(args.op === 'wait' ? ['wait_seconds'] : [])])
    return getTask(path, args)
  }
  select(args, ['id'])
  return request('DELETE', path, { deadline: 30000 })
}

const builders = { project: forProject, action: forAction, command: forCommand }
const readOnly = new Set(['health', 'list', 'get', 'wait', 'files'])
const terminal = new Set(['succeeded', 'failed', 'cancelled'])
const active = new Set(['preparing', 'running', 'cancelling'])
const streams = ['stdout', 'stderr']
const pollInterval = 1000
const transientWaitErrors = new Set(['request_timeout', 'transport_error', 'wait_expired'])

function taskTarget(group, args, body) {
  if (group === 'action') return { name: args.name }
  const command = args.id ?? body.id
  return typeof command === 'string' && commandId.test(command) ? { id: command } : null
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
    if (args.op === 'wait' && args.wait_seconds !== undefined) body.next_wait.wait_seconds = args.wait_seconds
  }
}

async function withStartLogs(group, args, response, signal, call) {
  if (!response.ok || args.status_only || !terminal.has(response.status) ||
      !(response.stdout_bytes > 0 || response.stderr_bytes > 0)) return response
  const target = taskTarget(group, args, response)
  if (!target) return { ...response, logs_error: { code: 'missing_task_id', message: '未收到命令 ID，无法查询日志。' } }
  try {
    const preview = builders[group]({ op: 'get', ...target })
    const logged = await requestRunner({ ...preview, deadline: 4000 }, signal, call)
    if (!logged.ok) {
      return { ...response, logs_error: { ...logged.error, http_status: logged.http_status } }
    }
    const fields = ['status', 'phase', 'commit', 'id', 'exit_code', 'output_source', 'stdout_bytes', 'stderr_bytes']
    if (fields.some(key => logged[key] !== response[key]) ||
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

async function waitForTask(spec, seconds, signal, call) {
  const budget = AbortSignal.timeout(Math.ceil(seconds * 1000))
  const polling = signal ? AbortSignal.any([signal, budget]) : budget
  const interval = Math.min(pollInterval, seconds * 500)
  let last
  try {
    while (!polling.aborted) {
      const response = await requestRunner(spec, polling, call)
      if (!response.ok) return response
      if (last?.commit && response.commit !== last.commit) {
        throw new ClientError('task_changed', '等待期间项目的当前提交变了；未把新一轮当作原任务。')
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
  if (!ok) {
    if (group === 'action' && args.op === 'start' && (body.action ||
        ['invalid_request', 'worker_unavailable', 'worker_unavailable_reset_incomplete'].includes(body.error?.code))) {
      body.next_get = { op: 'get', name: args.name }
      body.note = '此次提交可能已经替换了 Action 槽；先查看当前状态，再考虑重试。'
    }
    return { outcome: 'request_error', ...body }
  }
  if (group === 'project' || !['start', 'get', 'wait'].includes(args.op)) return { outcome: 'succeeded', ...body }

  const outcome = terminal.has(body.status) ? body.status : active.has(body.status) ? 'pending' : 'unknown'
  taskHints(args, body, taskTarget(group, args, body), outcome)
  return { outcome, ...body }
}

function definition(group, { description, ops, properties }) {
  return {
    name: `winrunner_${group}`,
    description,
    parameters: {
      type: 'object', additionalProperties: false,
      properties: { op: choice('选择操作。', ops), ...properties }, required: ['op'],
    },
    output: {
      schema: {
        type: 'object', additionalProperties: true, required: ['outcome'],
        properties: { outcome: choice('操作或任务结果；HTTP 状态只用于诊断。', ['succeeded', 'failed', 'cancelled', 'pending', 'request_error', 'unknown']) },
      },
      render(_args, value) { return [{ type: 'text', text: JSON.stringify(value, null, 2) }] },
    },
    timeoutMs: 130000,
    isConcurrencySafe: args => readOnly.has(args?.op),
    async execute(args, exec) {
      try {
        if (!args || typeof args !== 'object' || Array.isArray(args)) invalid('参数必须是对象。')
        enumValue(args.op, 'op', ops)
        const spec = builders[group](args)
        const signal = exec?.signal
        const call = {}
        let response = args.op === 'wait'
          ? await waitForTask(spec, waitSeconds(args), signal, call)
          : await requestRunner(spec, signal, call)
        if (args.op === 'start') response = await withStartLogs(group, args, response, signal, call)
        return toolResult(group, args, response)
      } catch (error) {
        if (!(error instanceof ClientError)) throw error
        return {
          outcome: error.outcomeUnknown ? 'unknown' : 'request_error',
          error: { code: error.code, message: error.message },
          ...(error.httpStatus === undefined ? {} : { http_status: error.httpStatus }),
          ...(error.retryAfter === undefined ? {} : { retry_after_seconds: error.retryAfter }),
          ...(error.outcomeUnknown ? { outcome_unknown: true } : {}),
          ...(args?.op === 'wait' && transientWaitErrors.has(error.code) ? { next_wait: args } : {}),
        }
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
