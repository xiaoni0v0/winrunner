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
const wait = { type: 'number', description: 'start：等远端回报结果的秒数（0–60，默认 0）。等满只返回当前状态，不会停止任务；用 get 继续查。' }
const taskQuery = {
  status_only: { type: 'boolean', description: 'get：只查状态。默认 false；不指定偏移时从头预览两路日志。' },
  stdout_offset: integer('get：从此字节续读 stdout，使用上次 stdout.next_offset。指定任一偏移后，只读显式指定的流。'),
  stderr_offset: integer('get：从此字节续读 stderr，使用上次 stderr.next_offset。'),
  limit: integer(`get：每路最多读取的字节数；默认预览 2048，指定偏移时默认 4096，上限 ${limits.log}。`),
  encoding: choice('get：日志默认 utf8；非 UTF-8 或需要无损时用 base64。', encodings),
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
    description: '提交、轮询或取消 WinRunner 项目的 Git Action。start 占槽后覆盖上一轮工作区和日志，忙时返回 409。pending 时用 get，不要重复 start。',
    ops: ['start', 'get', 'cancel'],
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
    description: '提交、列出、轮询或删除 WinRunner 临时 Windows 命令。命令以远端 Runner 账号运行；pending 时按 id 查询；delete 终止并丢弃记录和日志。',
    ops: ['start', 'get', 'list', 'delete'],
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

function postWait(args) {
  const seconds = args.wait_seconds === undefined ? 0 : args.wait_seconds
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0 || seconds > 60) {
    invalid('wait_seconds 必须是 0–60 之间的秒数。')
  }
  return seconds
}

function getTask(path, args) {
  if (args.status_only !== undefined && typeof args.status_only !== 'boolean') invalid('status_only 必须是布尔值。')
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
    select(args, ['name', 'commit', 'interpreter', 'script', 'args', 'wait_seconds'])
    const commit = match(args.commit, hex, 'commit')
    const interpreter = text(args.interpreter, 'interpreter')
    const script = text(args.script, 'script')
    const argv = args.args === undefined ? [] : args.args
    if (!Array.isArray(argv) || !argv.every(clean)) invalid('args 必须是字符串数组。')
    const timeout = postWait(args)
    return request('POST', path, { body: { commit, interpreter, script, args: argv, timeout }, deadline: (timeout + 20) * 1000 })
  }
  if (args.op === 'get') {
    select(args, ['name', ...Object.keys(taskQuery)])
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
    select(args, ['mode', 'command', 'wait_seconds'])
    const mode = enumValue(args.mode, 'mode', modes)
    const command = text(args.command, 'command')
    const timeout = postWait(args)
    return request('POST', '/commands', { body: { mode, command, timeout }, deadline: (timeout + 20) * 1000 })
  }
  const path = `/commands/${encodeURIComponent(match(args.id, commandId, 'id'))}`
  if (args.op === 'get') {
    select(args, ['id', ...Object.keys(taskQuery)])
    return getTask(path, args)
  }
  select(args, ['id'])
  return request('DELETE', path, { deadline: 30000 })
}

const builders = { project: forProject, action: forAction, command: forCommand }
const readOnly = new Set(['health', 'list', 'get', 'files'])
const terminal = new Set(['succeeded', 'failed', 'cancelled'])
const active = new Set(['preparing', 'running', 'cancelling'])

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
  if (group === 'project' || !['start', 'get'].includes(args.op)) return { outcome: 'succeeded', ...body }

  const outcome = terminal.has(body.status) ? body.status : active.has(body.status) ? 'pending' : 'unknown'
  if (args.op === 'start' && (outcome === 'pending' || outcome === 'unknown' || body.stdout_bytes > 0 || body.stderr_bytes > 0)) {
    if (group === 'action') body.next_get = { op: 'get', name: args.name }
    else if (body.id) body.next_get = { op: 'get', id: body.id }
  }
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
        const response = await requestRunner(builders[group](args), exec?.signal)
        return toolResult(group, args, response)
      } catch (error) {
        if (!(error instanceof ClientError)) throw error
        return {
          outcome: error.outcomeUnknown ? 'unknown' : 'request_error',
          error: { code: error.code, message: error.message },
          ...(error.httpStatus === undefined ? {} : { http_status: error.httpStatus }),
          ...(error.retryAfter === undefined ? {} : { retry_after_seconds: error.retryAfter }),
          ...(error.outcomeUnknown ? { outcome_unknown: true } : {}),
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
