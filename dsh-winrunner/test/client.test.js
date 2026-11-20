import { strict as assert } from 'node:assert'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import http from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { apply, inject } from '../index.js'

async function server(t, handler) {
  const instance = http.createServer(async (req, res) => {
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    handler(req, res, Buffer.concat(chunks))
  })
  await new Promise((resolve, reject) => {
    instance.once('error', reject)
    instance.listen(0, '127.0.0.1', resolve)
  })
  t.after(() => new Promise(resolve => instance.close(resolve)))
  return `http://127.0.0.1:${instance.address().port}`
}

const json = (res, data, status = 200, headers = {}) => {
  const bytes = Buffer.from(JSON.stringify(data))
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': bytes.length, ...headers })
  res.end(bytes)
}

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-winrunner-test-'))
  const file = join(directory, 'client.json')
  const previous = process.env.WINRUNNER_CLIENT_CONFIG
  process.env.WINRUNNER_CLIENT_CONFIG = file
  t.after(async () => {
    if (previous === undefined) delete process.env.WINRUNNER_CLIENT_CONFIG
    else process.env.WINRUNNER_CLIENT_CONFIG = previous
    await rm(directory, { recursive: true, force: true })
  })
  const tools = new Map()
  let dispose
  apply({ effect(setup) { dispose = setup() }, tools: {
    register(tool) { tools.set(tool.name, tool); return () => tools.delete(tool.name) },
  } })
  return {
    tools, dispose, configure: (baseUrl, token = 'test-token', settings) => writeFile(file, JSON.stringify({ baseUrl, token, ...settings })),
    run: (name, args, signal) => tools.get(`winrunner_${name}`).execute(args, { signal }),
  }
}

test('registers just three tools; config is required, reread every request and not a tool argument', async t => {
  const client = await fixture(t)
  assert.deepEqual(inject, ['tools'])
  assert.deepEqual([...client.tools.keys()], ['winrunner_project', 'winrunner_action', 'winrunner_command'])
  let hit = 0
  const first = await server(t, (req, res) => { hit++; json(res, { server: 1, auth_ok: req.headers.authorization === 'Bearer fixture-one' }) })
  const second = await server(t, (req, res) => { hit++; json(res, { server: 2, auth_ok: req.headers.authorization === 'Bearer fixture-two' }) })
  assert.equal((await client.run('project', { op: 'health' })).error.code, 'config_missing')
  await client.configure(first, 'fixture-one')
  assert.deepEqual(await client.run('project', { op: 'health' }), { server: 1, auth_ok: true, outcome: 'succeeded', http_status: 200 })
  await client.configure('http://127.0.0.1', 'fixture-one', { port: Number(new URL(first).port) })
  assert.deepEqual(await client.run('project', { op: 'health' }), { server: 1, auth_ok: true, outcome: 'succeeded', http_status: 200 })
  await client.configure('http://127.0.0.1', 'fixture-two', { port: Number(new URL(second).port) })
  assert.deepEqual(await client.run('project', { op: 'health' }), { server: 2, auth_ok: true, outcome: 'succeeded', http_status: 200 })
  await client.configure(first, 'fixture-one', { port: Number(new URL(second).port) })
  assert.equal((await client.run('project', { op: 'health' })).error.code, 'invalid_config')
  await client.configure('http://127.0.0.1:80', 'fixture-one', { port: 81 })
  assert.equal((await client.run('project', { op: 'health' })).error.code, 'invalid_config')
  await client.configure('http://127.0.0.1', 'fixture-one', { port: 65536 })
  assert.equal((await client.run('project', { op: 'health' })).error.code, 'invalid_config')
  assert.equal((await client.run('project', { op: 'health', token: 'not-a-tool-option' })).error.code, 'invalid_input')
  await client.configure(second, 'bad\n')
  assert.equal((await client.run('project', { op: 'health' })).error.code, 'invalid_config')
  await client.configure('http://example.invalid')
  assert.equal((await client.run('project', { op: 'health' })).error.code, 'insecure_http')
  assert.equal(hit, 3)
  client.dispose()
  assert.equal(client.tools.size, 0)
})

test('maps project, action and command endpoints, preserves task and HTTP error details', async t => {
  const client = await fixture(t)
  const seen = []
  const base = await server(t, (req, res, body) => {
    seen.push([req.method, req.url, body.length ? JSON.parse(body) : null, req.headers['content-length'], body.length])
    if (req.method === 'DELETE') { res.writeHead(204); res.end(); return }
    if (req.url.includes('stdout_offset')) {
      const q = new URL(req.url, 'http://localhost').searchParams
      const streams = {
        ...(q.has('stdout_offset') ? { stdout: { data_base64: 'w6k=', next_offset: 2 } } : {}),
        ...(q.has('stderr_offset') ? { stderr: { data_base64: '/w==', next_offset: 1 } } : {}),
      }
      json(res, { status: 'failed', stdout_bytes: 2, stderr_bytes: 1, ...streams }); return
    }
    if (req.method === 'POST' && req.url.endsWith('/actions')) {
      json(res, { error: { code: 'invalid_request' }, action: { phase: 'reset', error: { code: 'reset_blocked' } } }, 400)
      return
    }
    if (req.method === 'POST' && req.url === '/commands') { json(res, { id: 'id1', status: 'running' }, 202); return }
    if (req.method === 'POST' && req.url === '/projects') { json(res, { name: 'demo' }, 201); return }
    json(res, { ok: false }) // the client owns `ok`, rather than trusting a server field.
  })
  await client.configure(base)
  assert.equal((await client.run('project', { op: 'list' })).outcome, 'succeeded')
  assert.equal((await client.run('project', { op: 'create', name: 'demo', repo_url: 'https://example.invalid/repo.git' })).http_status, 201)
  await client.run('project', { op: 'get', name: 'demo' })
  assert.equal((await client.run('project', { op: 'delete', name: 'demo' })).http_status, 204)
  const start = await client.run('action', { op: 'start', name: 'demo', commit: 'abc1234', interpreter: 'python', script: '任务.py' })
  assert.equal(start.outcome, 'request_error')
  assert.equal(start.http_status, 400)
  assert.equal(start.error.code, 'invalid_request')
  assert.deepEqual(start.action, { phase: 'reset', error: { code: 'reset_blocked' } })
  assert.deepEqual(start.next_get, { op: 'get', name: 'demo' })
  const task = await client.run('action', { op: 'get', name: 'demo' })
  assert.deepEqual(task.stdout, { text: 'é', next_offset: 2, remaining: 'complete' })
  assert.equal(task.stderr.next_offset, 1)
  assert.equal(task.outcome, 'failed')
  assert.equal(task.http_status, 200)
  assert(!Object.hasOwn(task, 'ok'))
  assert.equal((await client.run('action', { op: 'cancel', name: 'demo' })).http_status, 204)
  const commandStart = await client.run('command', { op: 'start', mode: 'ps', command: "Write-Output 'hello'\n", wait_seconds: 2.5 })
  assert.equal(commandStart.id, 'id1')
  assert.equal(commandStart.outcome, 'pending')
  assert.deepEqual(commandStart.next_get, { op: 'get', id: 'id1' })
  await client.run('command', { op: 'list' })
  const command = await client.run('command', { op: 'get', id: 'id1', stdout_offset: 0, encoding: 'base64' })
  assert.equal(command.stdout.data_base64, 'w6k=')
  assert(!Object.hasOwn(command, 'stderr'))
  assert.equal((await client.run('command', { op: 'delete', id: 'id1' })).http_status, 204)
  assert.deepEqual(seen.map(([method, path]) => `${method} ${path}`), [
    'GET /projects', 'POST /projects', 'GET /projects/demo', 'DELETE /projects/demo',
    'POST /projects/demo/actions', 'GET /projects/demo/actions?stdout_offset=0&stderr_offset=0&limit=2048',
    'DELETE /projects/demo/actions', 'POST /commands', 'GET /commands', 'GET /commands/id1?stdout_offset=0&limit=4096', 'DELETE /commands/id1',
  ])
  assert.deepEqual(seen[4][2], { commit: 'abc1234', interpreter: 'python', script: '任务.py', args: [], timeout: 0 })
  assert.equal(Number(seen[4][3]), seen[4][4])
  assert.equal(seen[7][2].timeout, 2.5)
  assert.equal((await client.run('action', { op: 'start', name: 'demo', commit: 'main', interpreter: 'python', script: '任务.py' })).error.code, 'invalid_input')
  assert.equal((await client.run('command', { op: 'start', mode: 'ps', command: 'echo ok', wait_seconds: 61 })).error.code, 'invalid_input')
  assert.equal((await client.run('command', { op: 'get', id: '../another' })).error.code, 'invalid_input')
  assert.equal(seen.length, 11)
})

test('previews both logs, distinguishes live end from completion, and supports status-only polling', async t => {
  const client = await fixture(t)
  const queries = []
  const logs = { stdout: Buffer.from('abcdefgh'), stderr: Buffer.alloc(0) }
  let status = 'running'
  const base = await server(t, (req, res) => {
    const params = new URL(req.url, 'http://localhost').searchParams
    queries.push(Object.fromEntries(params))
    const snapshot = { status, stdout_bytes: logs.stdout.length, stderr_bytes: logs.stderr.length }
    for (const [stream, bytes] of Object.entries(logs)) {
      if (!params.has(`${stream}_offset`)) continue
      const offset = Number(params.get(`${stream}_offset`))
      const page = bytes.subarray(offset, offset + Number(params.get('limit')))
      snapshot[stream] = { next_offset: offset + page.length, data_base64: page.toString('base64') }
    }
    json(res, snapshot)
  })
  await client.configure(base)
  const args = { op: 'get', id: 'id1' }
  const preview = await client.run('command', { ...args, limit: 3 })
  assert.equal(preview.outcome, 'pending')
  assert.deepEqual(preview.stdout, { text: 'abc', next_offset: 3, remaining: 'available' })
  assert.deepEqual(preview.stderr, { text: '', next_offset: 0, remaining: 'caught_up' })
  const only = await client.run('command', { ...args, status_only: true })
  assert.equal(only.outcome, 'pending')
  assert(!Object.hasOwn(only, 'stdout'))
  assert.equal((await client.run('command', { ...args, status_only: true, stdout_offset: 0 })).outcome, 'request_error')
  status = 'cancelled'
  const tail = await client.run('command', { ...args, stdout_offset: preview.stdout.next_offset, encoding: 'base64' })
  assert.equal(tail.outcome, 'cancelled')
  assert.deepEqual(tail.stdout, { data_base64: 'ZGVmZ2g=', next_offset: 8, remaining: 'complete' })
  assert(!Object.hasOwn(tail, 'stderr'))
  assert.deepEqual(queries, [{ stdout_offset: '0', stderr_offset: '0', limit: '3' }, {}, { stdout_offset: '3', limit: '4096' }])
})

test('uses a UTF-8 character boundary as the next log offset', async t => {
  const client = await fixture(t)
  const bytes = Buffer.from(`${'x'.repeat(2047)}中!`)
  const base = await server(t, (req, res) => {
    const query = new URL(req.url, 'http://localhost').searchParams
    const offset = Number(query.get('stdout_offset'))
    const page = bytes.subarray(offset, offset + Number(query.get('limit')))
    json(res, { status: 'succeeded', stdout_bytes: bytes.length,
      stdout: { next_offset: offset + page.length, data_base64: page.toString('base64') } })
  })
  await client.configure(base)
  const args = { op: 'get', id: 'id1' }
  const first = (await client.run('command', args)).stdout
  assert.equal(first.next_offset, 2047)
  assert.equal(first.remaining, 'available')
  const last = (await client.run('command', { ...args, stdout_offset: first.next_offset })).stdout
  assert.equal(first.text + last.text, bytes.toString())
  assert.equal(last.remaining, 'complete')
  const tiny = (await client.run('command', { ...args, stdout_offset: first.next_offset, limit: 1 })).stdout
  assert.equal(tiny.next_offset, first.next_offset + 1)
})

test('handles empty files, bounded byte ranges, and small directory pages', async t => {
  const client = await fixture(t)
  const bytes = Buffer.from([0, 1, 2, 3, 0x80, 0xa0, 0xff])
  const entries = Array.from({ length: 101 }, (_, i) => ({ name: `${i}.txt`, type: 'file', size_bytes: 3 }))
  const base = await server(t, (req, res) => {
    const path = new URL(req.url, 'http://localhost').searchParams.get('path')
    if (!path) { json(res, { path: '', entries }); return }
    if (path === 'empty') { res.writeHead(200, { 'content-type': 'application/octet-stream', 'content-length': 0 }); res.end(); return }
    const match = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range ?? '')
    if (match) {
      const start = Number(match[1])
      const end = Math.min(bytes.length - 1, Number(match[2]))
      const block = bytes.subarray(start, end + 1)
      res.writeHead(206, { 'content-type': 'application/octet-stream', 'content-length': block.length, 'content-range': `bytes ${start}-${end}/${bytes.length}` })
      res.end(block)
    } else {
      res.writeHead(200, { 'content-type': 'application/octet-stream', 'content-length': bytes.length }); res.end(bytes)
    }
  })
  await client.configure(base)
  const args = { op: 'files', name: 'demo' }
  const root = await client.run('project', args)
  assert.equal(root.entries.length, 100)
  assert.equal(root.next_entry_offset, 100)
  assert.equal((await client.run('project', { ...args, entry_offset: 100 })).entries[0].name, '100.txt')
  const empty = await client.run('project', { ...args, path: 'empty' })
  assert.equal(empty.size_bytes, 0)
  assert.equal(empty.text, '')
  assert.equal(empty.next_offset, null)
  const file = { ...args, path: 'with spaces?#.bin', length: 4, encoding: 'base64' }
  const first = await client.run('project', file)
  assert.equal(first.next_offset, 4)
  const second = await client.run('project', { ...file, offset: first.next_offset })
  assert.equal(second.http_status, 206)
  assert.equal(second.next_offset, null)
  assert.deepEqual(Buffer.concat([Buffer.from(first.data_base64, 'base64'), Buffer.from(second.data_base64, 'base64')]), bytes)
})

test('never follows redirects or retries mutations, and respects an already cancelled run', async t => {
  const client = await fixture(t)
  let hits = 0
  let redirectedHits = 0
  const target = await server(t, (_req, res) => { redirectedHits++; json(res, {}) })
  const base = await server(t, (req, res) => {
    hits++
    if (req.url === '/projects') { res.writeHead(502, { 'content-type': 'text/html', 'retry-after': '5' }); res.end('<h1>proxy only</h1>'); return }
    if (req.method === 'GET') { res.writeHead(302, { location: `${target}/unexpected` }); res.end(); return }
    json(res, { error: { code: 'server_busy' } }, 503, { 'retry-after': '1' })
  })
  await client.configure(base)
  const redirect = await client.run('project', { op: 'health' })
  assert.equal(redirect.error.code, 'unexpected_redirect')
  assert.equal(redirect.http_status, 302)
  const proxy = await client.run('project', { op: 'create', name: 'demo', repo_url: 'https://example.invalid/repo.git' })
  assert.equal(proxy.http_status, 502)
  assert.equal(proxy.retry_after_seconds, 5)
  assert.equal(proxy.error.code, 'invalid_response')
  assert.equal(proxy.outcome_unknown, true)
  assert(!JSON.stringify(proxy).includes('proxy only'))
  const busy = await client.run('command', { op: 'start', mode: 'cmd', command: 'echo ok' })
  assert.equal(busy.http_status, 503)
  assert.equal(busy.retry_after_seconds, 1)
  assert.equal((await client.run('project', { op: 'health' }, AbortSignal.abort())).error.code, 'cancelled')
  assert.equal(hits, 3)
  assert.equal(redirectedHits, 0)
})
