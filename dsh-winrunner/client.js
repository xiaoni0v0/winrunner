import { open } from 'node:fs/promises'
import http from 'node:http'
import https from 'node:https'
import { homedir } from 'node:os'
import { isAbsolute, join } from 'node:path'

export const limits = { log: 8192, file: 16384, entries: 100 }
const CONFIG_LIMIT = 16 * 1024
const JSON_LIMIT = 512 * 1024
const INPUT_LIMIT = 1024 * 1024
const INVALID_TOKEN_CHAR = /[^!-~]/
// These Runner responses have a defined outcome, even when a task record exists.
const knownWriteFailures = new Set([
  '500 invalid_config', '500 validation_failed', '500 worker_unavailable',
  '500 worker_unavailable_reset_incomplete', '502 git_clone_failed',
  '503 idempotency_capacity', '503 server_busy', '503 shutting_down',
])

export class ClientError extends Error {
  constructor(code, message) {
    super(message)
    this.code = code
  }
}

async function loadConfig() {
  const path = process.env.WINRUNNER_CLIENT_CONFIG ?? join(homedir(), '.winrunner', 'client.json')
  if (!isAbsolute(path)) throw new ClientError('invalid_config', 'WINRUNNER_CLIENT_CONFIG 必须是绝对路径。')
  let file
  let settings
  try {
    file = await open(path, 'r')
    if (!(await file.stat()).isFile()) throw new ClientError('invalid_config', 'WinRunner 配置必须是普通文件。')
    const bytes = Buffer.alloc(CONFIG_LIMIT + 1)
    let used = 0
    while (used < bytes.length) {
      const { bytesRead } = await file.read(bytes, used, bytes.length - used, used)
      if (!bytesRead) break
      used += bytesRead
    }
    if (used > CONFIG_LIMIT) throw new ClientError('invalid_config', 'WinRunner 配置超过 16 KiB。')
    settings = JSON.parse(bytes.subarray(0, used).toString('utf8').replace(/^\uFEFF/, ''))
  } catch (error) {
    if (error instanceof ClientError) throw error
    if (error.code === 'ENOENT') {
      throw new ClientError('config_missing', '请先在 ~/.winrunner/client.json（或 WINRUNNER_CLIENT_CONFIG 指向的文件）填写 baseUrl 和 token。')
    }
    throw new ClientError('invalid_config', '无法读取 WinRunner 配置，请检查权限和 JSON 格式。')
  } finally {
    await file?.close()
  }
  const { baseUrl, port, token, allowInsecureHttp = false } = settings ?? {}
  let origin
  try {
    if (typeof baseUrl !== 'string' || /[?#\\\s]/.test(baseUrl) || !/^https?:\/\/[^/]+\/?$/i.test(baseUrl)) throw new Error()
    origin = new URL(baseUrl)
    if (origin.username || origin.password || baseUrl.includes('@')) throw new Error()
  } catch {
    throw new ClientError('invalid_config', 'baseUrl 必须是绝对 HTTP(S) 源地址，只包含主机和可选端口。')
  }
  // URL.port hides an explicit protocol-default port, so compare the original spelling too.
  const embeddedPort = /:(\d+)\/?$/.exec(baseUrl)?.[1]
  if ((embeddedPort !== undefined && Number(embeddedPort) === 0) ||
      (port != null && (!Number.isInteger(port) || port < 1 || port > 65535))) {
    throw new ClientError('invalid_config', '端口必须是 1–65535 的整数。')
  }
  if (port != null) {
    if (embeddedPort !== undefined && Number(embeddedPort) !== port) {
      throw new ClientError('invalid_config', 'baseUrl 中的端口与 port 不一致，请只填一处或保持一致。')
    }
    origin.port = String(port)
  }
  if (typeof token !== 'string' || !token || INVALID_TOKEN_CHAR.test(token) || typeof allowInsecureHttp !== 'boolean') {
    throw new ClientError('invalid_config', '配置需要非空、无空格的 ASCII token；allowInsecureHttp 必须为布尔值。')
  }
  const loopback = ['127.0.0.1', '[::1]'].includes(origin.hostname)
  if (origin.protocol === 'http:' && !loopback && !allowInsecureHttp) {
    throw new ClientError('insecure_http', '非回环明文 HTTP 需要你在本机配置显式设置 allowInsecureHttp: true；优先使用 HTTPS。')
  }
  return { origin, token }
}

function integerHeader(value) {
  if (typeof value !== 'string' || !/^\d+$/.test(value)) return undefined
  const num = Number(value)
  return Number.isSafeInteger(num) ? num : undefined
}

async function readBody(response, limit, prefix = false) {
  if (!limit && prefix) {
    response.destroy()
    return Buffer.alloc(0)
  }
  const chunks = []
  let size = 0
  for await (const chunk of response) {
    if (prefix) {
      const take = Math.min(chunk.length, limit - size)
      size += take
      chunks.push(chunk.subarray(0, take))
      if (size === limit) break
    } else {
      size += chunk.length
      if (size > limit) throw new ClientError('response_too_large', 'WinRunner 返回的 JSON 超出客户端大小上限。')
      chunks.push(chunk)
    }
  }
  return Buffer.concat(chunks, size)
}

export function encoded(bytes, encoding) {
  return encoding === 'base64' ? { data_base64: bytes.toString('base64') } : { text: bytes.toString('utf8') }
}

async function fileResult(response, file) {
  const status = response.statusCode
  const contentLength = integerHeader(response.headers['content-length'])
  let total
  let bytesToRead
  if (status === 206) {
    const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers['content-range'] ?? '')
    const start = integerHeader(match?.[1])
    const end = integerHeader(match?.[2])
    total = integerHeader(match?.[3])
    if (start !== file.offset || end === undefined || total === undefined || end < start || end >= total ||
        end - start + 1 !== contentLength || contentLength > file.length) {
      throw new ClientError('invalid_response', 'WinRunner 返回了与请求不一致的文件字节范围。')
    }
    bytesToRead = contentLength
  } else if (status === 200 && file.offset === 0 && contentLength !== undefined) {
    total = contentLength
    bytesToRead = Math.min(total, file.length)
  } else {
    throw new ClientError('invalid_response', 'WinRunner 的文件响应缺少长度或没有按偏移返回。')
  }
  const bytes = await readBody(response, bytesToRead, bytesToRead < contentLength)
  if (bytes.length !== bytesToRead) throw new ClientError('incomplete_response', 'WinRunner 文件响应未完整送达。')
  const page = file.encoding === 'utf8' && file.offset + bytes.length < total ? utf8Page(bytes) : bytes
  const next = file.offset + page.length
  return { kind: 'file', offset: file.offset, size_bytes: total, next_offset: next < total ? next : null,
    encoding: file.encoding, ...encoded(page, file.encoding) }
}

function responseJson(bytes) {
  try {
    const value = JSON.parse(bytes.toString('utf8'))
    if (value && typeof value === 'object' && !Array.isArray(value)) return value
  } catch { /* report without echoing a potentially sensitive non-API body */ }
  throw new ClientError('invalid_response', 'WinRunner 没有返回有效的 JSON 对象。')
}

// Leave only a valid, unfinished UTF-8 scalar for the next read. An entire
// live log chunk can be just that fragment; in that case keep its offset.
function utf8Page(bytes) {
  let lead = bytes.length - 1
  while (lead >= 0 && bytes.length - lead < 4 && (bytes[lead] & 0xc0) === 0x80) lead--
  const byte = bytes[lead]
  const width = byte >= 0xc2 && byte <= 0xdf ? 2 : byte >= 0xe0 && byte <= 0xef ? 3 : byte >= 0xf0 && byte <= 0xf4 ? 4 : 0
  if (lead < 0 || width <= bytes.length - lead) return bytes
  try { new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(lead), { stream: true }) } catch { return bytes }
  return bytes.subarray(0, lead)
}

function projectResponse(body, spec) {
  if (spec.file && Array.isArray(body.entries)) {
    if (spec.file.explicitFile) throw new ClientError('not_a_file', '该路径现在是目录，不能继续按文件字节读取。')
    const { entryOffset: offset, entryLimit } = spec.file
    if (Object.hasOwn(body, 'next_entry_offset')) {
      const next = body.next_entry_offset
      if (body.entries.length > entryLimit || (next !== null &&
          (!Number.isSafeInteger(next) || !body.entries.length || next !== offset + body.entries.length))) {
        throw new ClientError('invalid_response', 'WinRunner 返回的目录游标与本页不一致。')
      }
      return { ...body, kind: 'directory' }
    }
    const entries = body.entries.slice(offset, offset + entryLimit)
    return { kind: 'directory', path: body.path, entries, total_entries: body.entries.length,
      next_entry_offset: offset + entries.length < body.entries.length ? offset + entries.length : null }
  }
  if (spec.logEncoding) {
    const terminal = ['succeeded', 'failed', 'cancelled'].includes(body.status)
    for (const stream of ['stdout', 'stderr']) {
      const chunk = body[stream]
      if (chunk && typeof chunk.data_base64 === 'string') {
        const total = body[`${stream}_bytes`]
        const raw = Buffer.from(chunk.data_base64, 'base64')
        const storedAhead = chunk.next_offset < total
        const page = spec.logEncoding === 'utf8' && (storedAhead || !terminal) ? utf8Page(raw) : raw
        const nextOffset = chunk.next_offset - (raw.length - page.length)
        body[stream] = {
          next_offset: nextOffset,
          remaining: storedAhead ? 'available' : terminal ? 'complete' : 'caught_up',
          ...encoded(page, spec.logEncoding),
        }
      }
    }
  }
  return body
}

export async function requestRunner(spec, signal, call = {}) {
  if (signal?.aborted) throw new ClientError('cancelled', '本地请求已取消，尚未向 WinRunner 发起请求。')
  const { origin, token } = await loadConfig() // Read configuration for every HTTP request, including polls.
  if (call.origin !== undefined && call.origin !== origin.origin) {
    throw new ClientError('runner_changed', '等待期间 WinRunner 地址已改变；未向新地址查询旧任务。')
  }
  call.origin = origin.origin
  const stop = AbortSignal.timeout(spec.deadline ?? 20000)
  const active = signal ? AbortSignal.any([stop, signal]) : stop
  const url = new URL(spec.path, origin)
  const headers = { Authorization: `Bearer ${token}`, 'Accept-Encoding': 'identity', Connection: 'close' }
  if (spec.idempotencyKey !== undefined) headers['Idempotency-Key'] = spec.idempotencyKey
  const data = spec.body === undefined ? undefined : Buffer.from(JSON.stringify(spec.body), 'utf8')
  if (data) {
    if (data.length > INPUT_LIMIT) throw new ClientError('invalid_input', '请求体超过 WinRunner 的 1 MiB 上限。')
    headers['Content-Type'] = 'application/json; charset=utf-8'
    headers['Content-Length'] = String(data.length)
  }
  if (spec.file?.offset > 0) headers.Range = `bytes=${spec.file.offset}-${spec.file.offset + spec.file.length - 1}`

  let response
  try {
    response = await new Promise((resolve, reject) => {
      const transport = origin.protocol === 'https:' ? https : http
      const request = transport.request(url, { method: spec.method, headers, agent: false, signal: active }, resolve)
      request.once('error', reject)
      request.end(data)
    })
    const status = response.statusCode
    const ok = status >= 200 && status < 300
    if (status >= 300 && status < 400) {
      throw new ClientError('unexpected_redirect', 'WinRunner 返回了重定向；没有跟随，也没有把令牌发送给重定向目标。')
    }
    const contentType = response.headers['content-type']?.split(';', 1)[0].trim().toLowerCase()
    const body = status === 204 ? {} : ok && spec.file && contentType === 'application/octet-stream'
      ? await fileResult(response, spec.file)
      : projectResponse(responseJson(await readBody(response, JSON_LIMIT)), spec)
    if (!ok && (typeof body.error?.code !== 'string' || typeof body.error?.message !== 'string')) {
      throw new ClientError('invalid_response', 'WinRunner 没有返回符合接口约定的错误对象。')
    }
    const retryAfter = integerHeader(response.headers['retry-after'])
    const outcomeUnknown = spec.method !== 'GET' && status >= 500 && status <= 599 &&
      !knownWriteFailures.has(`${status} ${body.error?.code}`)
    return { ...body, ok, http_status: status, ...(outcomeUnknown ? { outcome_unknown: true } : {}),
      ...(retryAfter === undefined ? {} : { retry_after_seconds: retryAfter }) }
  } catch (error) {
    const failure = signal?.aborted
      ? new ClientError('cancelled', '本地请求已取消；已经提交的远端任务未必停止。')
      : stop.aborted
        ? new ClientError('request_timeout', '客户端等候超时。')
        : error instanceof ClientError ? error
          : new ClientError('transport_error', 'WinRunner 连接失败或响应中途断开。')
    if (response) {
      failure.httpStatus = response.statusCode
      failure.retryAfter = integerHeader(response.headers['retry-after'])
    }
    if (spec.method !== 'GET') {
      failure.outcomeUnknown = true
      failure.message += ' 不能确认本次写操作的结果；先核对同一台 Runner 的状态，不要自动重试。'
    }
    throw failure
  } finally {
    if (response && !response.complete) response.destroy()
  }
}
