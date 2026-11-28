import { strict as assert } from 'node:assert'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import http from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { apply, inject } from '@local/dsh-winrunner-client'

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

test('only a post-dispatch cancellation of a write adds the reconciliation warning', async t => {
  const client = await fixture(t)
  const { finalizeContent } = client.tools.get('winrunner_command')
  const aborted = { error: { info: { code: 'ABORTED' } }, content: [{ type: 'text', text: 'Aborted' }] }
  assert.match(finalizeContent({ arguments: { op: 'start' } }, aborted)[1].text, /不要盲目重试/)
  assert.equal(finalizeContent({ arguments: { op: 'wait' } }, aborted), undefined)
  assert.equal(finalizeContent({ arguments: { op: 'start' } }, { ...aborted, error: { info: { code: 'ABORTED_BEFORE_DISPATCH' } } }), undefined)
})

test('health preserves all CPU windows, including zero and missing samples', async t => {
  const client = await fixture(t)
  const cpu = { logical_cores: 128, usage_percent: 0.57, usage_5s_percent: 0, usage_60s_percent: null }
  await client.configure(await server(t, (_req, res) => json(res, { cpu })))
  assert.deepEqual((await client.run('project', { op: 'health', params_json: '{}' })).cpu, cpu)
  const schema = client.tools.get('winrunner_project').output.schema.properties.cpu.properties
  for (const key of ['usage_percent', 'usage_5s_percent', 'usage_60s_percent']) {
    assert.match(schema[key].description, /百分比，或 null/)
  }
})

test('two-field tool schema works with strict transport and returns directly replayable hints', async t => {
  const client = await fixture(t)
  const seen = []
  const base = await server(t, (req, res, body) => {
    seen.push([req.method, req.url, body.length ? JSON.parse(body) : null])
    if (req.method === 'POST' && req.url === '/commands') {
      return json(res, { id: '0', status: 'running' }, 202)
    }
    if (req.url.startsWith('/commands/0?')) {
      return json(res, { id: '0', status: 'succeeded', stdout_bytes: 2, stderr_bytes: 0,
        stdout: { data_base64: 'eA==', next_offset: 1 }, stderr: { data_base64: '', next_offset: 0 } })
    }
    json(res, {})
  })
  await client.configure(base)
  for (const tool of client.tools.values()) {
    assert.deepEqual(Object.keys(tool.parameters.properties), ['op', 'params_json'])
    assert.equal(tool.parameters.properties.params_json.type, 'string')
  }
  assert.equal((await client.run('project', { op: 'health', params_json: '' })).outcome, 'succeeded')
  assert.equal((await client.run('command', { op: 'list', params_json: '{}' })).outcome, 'succeeded')
  for (const raw of [
    { op: 'health', params_json: '{' },
    { op: 'health', params_json: 'null' },
    { op: 'health', params_json: '[]' },
    { op: 'health', params_json: '{"op":"delete"}' },
    { op: 'health', params_json: '{"name":"demo"}' },
    { op: 'health', params_json: '{}', name: 'demo' },
    { op: 'delete', params_json: '' },
  ]) assert.equal((await client.run('project', raw)).error.code, 'invalid_input')
  const command = "Write-Output '中文'\nWrite-Output 'C:\\temp'"
  const start = await client.run('command', { op: 'start', params_json: JSON.stringify({ mode: 'ps', command }) })
  assert.deepEqual(start.next_wait, { op: 'wait', params_json: '{"id":"0"}' })
  const done = await client.run('command', start.next_wait)
  assert.equal(done.outcome, 'succeeded')
  assert.deepEqual(done.next_get, { op: 'get', params_json: '{"id":"0","stdout_offset":1}' })
  await client.run('command', done.next_get)
  assert.deepEqual(seen, [
    ['GET', '/health', null], ['GET', '/commands', null],
    ['POST', '/commands', { mode: 'ps', command, timeout: 0 }],
    ['GET', '/commands/0?stdout_offset=0&stderr_offset=0&limit=2048', null],
    ['GET', '/commands/0?stdout_offset=1&limit=4096', null],
  ])
})

test('keeps project command scope through start, preview, wait, pagination, list and delete', async t => {
  const client = await fixture(t)
  const seen = []
  const collection = '/projects/Demo/commands'
  const item = `${collection}/0`
  const preview = `${item}?stdout_offset=0&stderr_offset=0&limit=2048`
  const page = `${item}?stdout_offset=1&limit=4096`
  const finished = { id: '0', mode: 'ps', status: 'succeeded', stdout_bytes: 2, stderr_bytes: 0 }
  await client.configure(await server(t, (req, res, body) => {
    seen.push([req.method, req.url, body.length ? JSON.parse(body) : null, req.headers['idempotency-key']])
    if (req.method === 'POST' && req.url === collection) {
      if (req.headers['idempotency-key']) return json(res, { ...finished, status: 'running' }, 202)
      return json(res, finished)
    }
    if (req.method === 'GET' && req.url === preview) {
      return json(res, { ...finished, stdout: { data_base64: 'YQ==', next_offset: 1 }, stderr: { data_base64: '', next_offset: 0 } })
    }
    if (req.method === 'GET' && req.url === page) {
      return json(res, { ...finished, stdout: { data_base64: 'Yg==', next_offset: 2 } })
    }
    if (req.method === 'GET' && req.url === collection) return json(res, { commands: [finished] })
    if (req.method === 'DELETE' && req.url === item) { res.writeHead(204); res.end(); return }
    return json(res, { error: { code: 'not_found', message: 'Not found' } }, 404)
  }))
  const run = (op, params) => client.run('command', { op, params_json: JSON.stringify(params) })
  const name = 'Demo'
  const command = 'Write-Output ab'
  const args = { name, mode: 'ps', command }
  const short = await run('start', { ...args, wait_seconds: 5 })
  assert.equal(short.outcome, 'succeeded')
  assert.equal(short.stdout.text, 'a')
  assert.deepEqual(short.next_get, { op: 'get', params_json: '{"name":"Demo","id":"0","stdout_offset":1}' })
  assert.equal((await client.run('command', short.next_get)).stdout.text, 'b')

  const pending = await run('start', { ...args, idempotency_key: 'one' })
  assert.equal(pending.outcome, 'pending')
  assert.deepEqual(pending.next_wait, { op: 'wait', params_json: '{"name":"Demo","id":"0"}' })
  assert.equal((await client.run('command', pending.next_wait)).outcome, 'succeeded')
  assert.deepEqual((await run('list', { name })).commands, [finished])
  assert.equal((await run('delete', { name, id: '0' })).http_status, 204)
  assert.equal((await run('get', { name: 'Missing', id: '0' })).http_status, 404)
  for (const invalidName of ['', null, '../Other', 'NUL']) {
    assert.equal((await run('list', { name: invalidName })).error.code, 'invalid_input')
  }
  assert.deepEqual(seen, [
    ['POST', collection, { mode: 'ps', command, timeout: 5 }, undefined],
    ['GET', preview, null, undefined],
    ['GET', page, null, undefined],
    ['POST', collection, { mode: 'ps', command, timeout: 0 }, 'one'],
    ['GET', preview, null, undefined],
    ['GET', collection, null, undefined],
    ['DELETE', item, null, undefined],
    ['GET', '/projects/Missing/commands/0?stdout_offset=0&stderr_offset=0&limit=2048', null, undefined],
  ])
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
      json(res, { error: { code: 'invalid_request', message: 'Invalid input' }, action: { phase: 'reset', error: { code: 'reset_blocked' } } }, 400)
      return
    }
    if (req.method === 'POST' && req.url === '/commands') { json(res, { id: 'id1', status: 'running' }, 202); return }
    if (req.method === 'POST' && req.url === '/projects/demo') { json(res, { name: 'demo' }, 201); return }
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
  assert.deepEqual(commandStart.next_wait, { op: 'wait', id: 'id1' })
  assert(!Object.hasOwn(commandStart, 'next_get'))
  await client.run('command', { op: 'list' })
  const command = await client.run('command', { op: 'get', id: 'id1', stdout_offset: 0, encoding: 'base64' })
  assert.equal(command.stdout.data_base64, 'w6k=')
  assert(!Object.hasOwn(command, 'stderr'))
  assert.equal((await client.run('command', { op: 'delete', id: 'id1' })).http_status, 204)
  assert.deepEqual(seen.map(([method, path]) => `${method} ${path}`), [
    'GET /projects', 'POST /projects/demo', 'GET /projects/demo', 'DELETE /projects/demo',
    'POST /projects/demo/actions', 'GET /projects/demo/actions?include_input=1&stdout_offset=0&stderr_offset=0&limit=2048',
    'DELETE /projects/demo/actions', 'POST /commands', 'GET /commands', 'GET /commands/id1?stdout_offset=0&limit=4096', 'DELETE /commands/id1',
  ])
  assert.deepEqual(seen[1][2], { repo_url: 'https://example.invalid/repo.git' })
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
  const preview = await client.run('command', { ...args, limit: 4 })
  assert.equal(preview.outcome, 'pending')
  assert.deepEqual(preview.stdout, { text: 'abcd', next_offset: 4, remaining: 'available' })
  assert.deepEqual(preview.stderr, { text: '', next_offset: 0, remaining: 'caught_up' })
  assert.deepEqual(preview.next_get, { op: 'get', id: 'id1', stdout_offset: 4, stderr_offset: 0, limit: 4 })
  assert(!Object.hasOwn(preview, 'next_wait'))
  const only = await client.run('command', { ...args, status_only: true })
  assert.equal(only.outcome, 'pending')
  assert(!Object.hasOwn(only, 'stdout'))
  assert.equal((await client.run('command', { ...args, status_only: true, stdout_offset: 0 })).outcome, 'request_error')
  status = 'cancelled'
  const tail = await client.run('command', { ...args, stdout_offset: preview.stdout.next_offset, encoding: 'base64' })
  assert.equal(tail.outcome, 'cancelled')
  assert.deepEqual(tail.stdout, { data_base64: 'ZWZnaA==', next_offset: 8, remaining: 'complete' })
  assert(!Object.hasOwn(tail, 'stderr'))
  assert.deepEqual(queries, [{ stdout_offset: '0', stderr_offset: '0', limit: '4' }, {}, { stdout_offset: '4', limit: '4096' }])
})

test('start returns terminal logs and never replaces its result with a failed or mismatched follow-up', async t => {
  const client = await fixture(t)
  const seen = []
  let followup = 'matching'
  const action = { action_id: 'original', status: 'failed', phase: 'run', commit: 'abc1234', exit_code: 7, stdout_bytes: 2, stderr_bytes: 0 }
  const base = await server(t, (req, res) => {
    seen.push(req.method)
    if (req.method === 'POST') { json(res, action); return }
    assert.equal(new URL(req.url, 'http://localhost').searchParams.get('include_input'), '1')
    if (followup === 'missing') { json(res, { error: { code: 'not_found', message: 'Not found' } }, 404); return }
    json(res, { ...action, action_id: followup === 'changed' ? 'replacement' : action.action_id,
      stdout: { data_base64: 'aGk=', next_offset: 2 }, stderr: { data_base64: '', next_offset: 0 } })
  })
  await client.configure(base)
  const args = { op: 'start', name: 'demo', commit: 'abc1234', interpreter: 'python', script: 'script.py', wait_seconds: 5 }
  const result = await client.run('action', args)
  assert.equal(result.outcome, 'failed')
  assert.equal(result.exit_code, 7)
  assert.deepEqual(result.stdout, { text: 'hi', next_offset: 2, remaining: 'complete' })
  assert(!Object.hasOwn(result, 'next_get'))
  followup = 'changed'
  const mismatch = await client.run('action', args)
  assert.equal(mismatch.commit, action.commit)
  assert.equal(mismatch.logs_error.code, 'task_changed')
  assert(!Object.hasOwn(mismatch, 'stdout'))
  assert(!Object.hasOwn(mismatch, 'next_get'))
  followup = 'missing'
  const missing = await client.run('action', args)
  assert.equal(missing.outcome, 'failed')
  assert.equal(missing.logs_error.http_status, 404)
  const only = await client.run('action', { ...args, status_only: true })
  assert.equal(only.outcome, 'failed')
  assert(!Object.hasOwn(only, 'logs_error'))
  assert.deepEqual(seen, ['POST', 'GET', 'POST', 'GET', 'POST', 'GET', 'POST'])
})

test('accepts fractional start wait times without altering the server timeout', async t => {
  const client = await fixture(t)
  const seen = []
  await client.configure(await server(t, (req, res, body) => {
    seen.push([req.method, JSON.parse(body).timeout])
    json(res, { id: '0', status: 'succeeded' })
  }))
  const result = await client.run('command', { op: 'start', mode: 'cmd', command: 'echo ok', wait_seconds: 0.0625 })
  assert.equal(result.outcome, 'succeeded')
  assert.deepEqual(seen, [['POST', 0.0625]])
})

test('wait polls only with GET and stops on completion or its own deadline', async t => {
  const client = await fixture(t)
  let hits = 0
  let complete = true
  const methods = []
  const base = await server(t, (req, res) => {
    methods.push(req.method)
    const status = complete && ++hits > 1 ? 'succeeded' : 'running'
    json(res, { id: '0', status, stdout_bytes: 1, stderr_bytes: 0,
      stdout: { data_base64: 'eA==', next_offset: 1 }, stderr: { data_base64: '', next_offset: 0 } })
  })
  await client.configure(base)
  const args = { op: 'wait', id: '0', wait_seconds: 1.5 }
  const done = await client.run('command', args)
  assert.equal(done.outcome, 'succeeded')
  assert.equal(done.stdout.text, 'x')
  assert(!Object.hasOwn(done, 'next_wait'))
  complete = false
  const expired = await client.run('command', { ...args, wait_seconds: 1 })
  assert.equal(expired.outcome, 'pending')
  assert.equal(expired.wait_expired, true)
  assert.deepEqual(expired.next_wait, { op: 'wait', id: '0', stdout_offset: 1, stderr_offset: 0 })
  assert.equal((await client.run('command', { ...args, wait_seconds: 0 })).error.code, 'invalid_input')
  assert(methods.length >= 3 && methods.every(method => method === 'GET'))
})

test('Action wait distinguishes separate runs of the same commit', async t => {
  const client = await fixture(t)
  let sameAction = false
  let hits = 0
  await client.configure(await server(t, (req, res) => {
    const url = new URL(req.url, 'http://localhost')
    assert.equal(url.pathname, '/projects/demo/actions')
    assert.equal(url.searchParams.get('include_input'), '1')
    const first = ++hits === 1
    json(res, { status: first ? 'running' : 'succeeded', action_id: first || sameAction ? 'original' : 'replacement', commit: sameAction && first ? null : 'abcdef' })
  }))
  const args = { op: 'wait', name: 'demo', status_only: true, wait_seconds: 2 }
  const replaced = await client.run('action', args)
  assert.equal(replaced.error.code, 'task_changed')
  assert.equal(replaced.next_wait, undefined)
  sameAction = true
  hits = 0
  assert.equal((await client.run('action', args)).outcome, 'succeeded')
})

test('Action continuation hints bind the first read to the original round', async t => {
  const client = await fixture(t)
  const original = 'a'.repeat(32)
  const replacement = 'b'.repeat(32)
  let current = original
  let reads = 0
  await client.configure(await server(t, (req, res) => {
    const url = new URL(req.url, 'http://localhost')
    assert.equal(url.pathname, '/projects/demo/actions')
    if (req.method === 'POST') return json(res, { action_id: original, status: 'preparing' }, 202)
    reads++
    assert.equal(url.searchParams.get('include_input'), '1')
    const offset = Number(url.searchParams.get('stdout_offset') ?? 0)
    json(res, { action_id: current, status: 'succeeded', commit: 'abcdef', stdout_bytes: 6, stderr_bytes: 0,
      stdout: { data_base64: Buffer.from(offset === 0 ? 'abcd' : 'ef').toString('base64'), next_offset: offset === 0 ? 4 : 6 },
      stderr: { data_base64: '', next_offset: 0 } })
  }))
  const start = await client.run('action', { op: 'start', params_json: JSON.stringify({
    name: 'demo', commit: 'abcdef', interpreter: 'python', script: 'task.py', idempotency_key: 'same' }) })
  assert.deepEqual(JSON.parse(start.next_wait.params_json), { name: 'demo', action_id: original })
  current = replacement
  const replaced = await client.run('action', start.next_wait)
  assert.equal(replaced.error.code, 'task_changed')
  assert.equal(replaced.stdout, undefined)
  assert.equal(replaced.next_get, undefined)
  assert.equal(reads, 1)
  current = original
  const firstPage = await client.run('action', { ...start.next_wait, params_json: JSON.stringify({
    ...JSON.parse(start.next_wait.params_json), limit: 4 }) })
  assert.deepEqual(JSON.parse(firstPage.next_get.params_json), { name: 'demo', action_id: original, stdout_offset: 4, limit: 4 })
  assert.equal((await client.run('action', firstPage.next_get)).stdout.text, 'ef')
  current = replacement
  const page = await client.run('action', firstPage.next_get)
  assert.equal(page.error.code, 'task_changed')
  assert.equal(page.stdout, undefined)
  current = undefined
  assert.equal((await client.run('action', firstPage.next_get)).error.code, 'action_identity_unavailable')
})

test('a rejected command with an allocated ID provides a scoped lookup', async t => {
  const client = await fixture(t)
  await client.configure(await server(t, (req, res) => {
    assert.equal(req.url, '/projects/demo/commands')
    json(res, { error: { code: 'invalid_request', message: 'Invalid command' }, command: { id: 'a', status: 'failed' } }, 400)
  }))
  const result = await client.run('command', { op: 'start', name: 'demo', mode: 'cmd', command: 'echo ok' })
  assert.equal(result.outcome, 'request_error')
  assert.deepEqual(result.next_get, { op: 'get', name: 'demo', id: 'a' })
})

test('Action wait diagnoses offset errors without reading a replacement log', async t => {
  const client = await fixture(t)
  let hits = 0
  let replacement = true
  await client.configure(await server(t, (req, res) => {
    const params = new URL(req.url, 'http://localhost').searchParams
    assert.equal(req.method, 'GET')
    assert.equal(params.get('include_input'), '1')
    switch (++hits) {
      case 1:
        assert.equal(params.get('stdout_offset'), '4')
        return json(res, { status: 'running', action_id: 'original' })
      case 2:
        assert.equal(params.get('stdout_offset'), '4')
        return json(res, { error: { code: 'invalid_offset', message: 'Offset exceeds log length' } }, 400)
      case 3:
        assert.deepEqual([...params.keys()], ['include_input'])
        return json(res, { status: 'running', action_id: replacement ? 'new' : 'original' })
      default: assert.fail('unexpected request')
    }
  }))
  const args = { op: 'wait', name: 'demo', stdout_offset: 4, wait_seconds: 3 }
  const replaced = await client.run('action', args)
  assert.equal(replaced.error.code, 'task_changed')
  assert.equal(replaced.next_wait, undefined)
  assert.equal(hits, 3)
  hits = 0
  replacement = false
  assert.equal((await client.run('action', args)).error.code, 'invalid_offset')
  assert.equal(hits, 3)
})

test('Action wait falls back to read-only polling on an older Runner', async t => {
  const client = await fixture(t)
  const seen = []
  await client.configure(await server(t, (req, res) => {
    seen.push([req.method, req.url])
    if (req.url.includes('include_input')) {
      return json(res, { error: { code: 'invalid_request', message: 'Unexpected query parameter' } }, 400)
    }
    json(res, { status: 'succeeded', commit: 'abcdef' })
  }))
  assert.equal((await client.run('action', { op: 'wait', name: 'demo', status_only: true })).outcome, 'succeeded')
  assert.deepEqual(seen, [['GET', '/projects/demo/actions?include_input=1'], ['GET', '/projects/demo/actions']])
})

test('wait bounds an unresponsive request and can be cancelled without mutating the task', async t => {
  const client = await fixture(t)
  const methods = []
  const base = await server(t, (req, _res) => { methods.push(req.method) })
  await client.configure(base)
  const args = { op: 'wait', params_json: '{"id":"0","wait_seconds":1}' }
  const expired = await client.run('command', args)
  assert.equal(expired.error.code, 'wait_expired')
  assert.deepEqual(expired.next_wait, { op: 'wait', params_json: '{"id":"0"}' })
  const cancelled = await client.run('command', expired.next_wait, AbortSignal.timeout(100))
  assert.equal(cancelled.error.code, 'cancelled')
  assert(!Object.hasOwn(cancelled, 'next_wait'))
  assert(methods.length >= 1 && methods.every(method => method === 'GET'))
})

test('wait stops before querying another endpoint if configuration changes mid-call', async t => {
  const client = await fixture(t)
  let otherHits = 0
  const other = await server(t, (_req, res) => { otherHits++; json(res, { status: 'succeeded' }) })
  const base = await server(t, (_req, res) => {
    void client.configure(other).then(() => json(res, { status: 'running' }))
  })
  await client.configure(base)
  const result = await client.run('command', { op: 'wait', id: '0', wait_seconds: 2, status_only: true })
  assert.equal(result.error.code, 'runner_changed')
  assert.equal(otherHits, 0)
})

test('filters both command collections on the server and preserves task timestamps', async t => {
  const client = await fixture(t)
  const seen = []
  const record = { id: '0', status: 'running', started_at: 1700000000000, finished_at: null }
  await client.configure(await server(t, (req, res) => {
    seen.push(req.url)
    json(res, { commands: [record] })
  }))
  for (const status of ['active', 'preparing', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled']) {
    const result = await client.run('command', { op: 'list', params_json: JSON.stringify({ name: 'Demo', status }) })
    assert.deepEqual(result.commands, [record])
    assert.equal(seen.at(-1), `/projects/Demo/commands?status=${status}`)
  }
  await client.run('command', { op: 'list', status: 'active' })
  assert.equal(seen.at(-1), '/commands?status=active')
  const count = seen.length
  assert.equal((await client.run('command', { op: 'list', status: 'pending' })).error.code, 'invalid_input')
  assert.equal(seen.length, count)
})

test('reads tails and offsets by stream with reusable continuation and opt-in input', async t => {
  const client = await fixture(t)
  const seen = []
  const stdout = Buffer.from('x中!')
  await client.configure(await server(t, (req, res) => {
    seen.push(req.url)
    const { pathname, searchParams: query } = new URL(req.url, 'http://localhost')
    const action = pathname.endsWith('/actions')
    json(res, {
      ...(action ? { action_id: 'a'.repeat(32) } : { id: '0' }),
      status: action ? 'succeeded' : 'running',
      started_at: 1700000000000, finished_at: action ? 1700000000050 : null,
      stdout_bytes: stdout.length, stderr_bytes: 5,
      ...(query.has('include_input') && !action ? { command: 'echo private' } : {}),
      ...(query.has('stdout_tail') ? { stdout: { data_base64: stdout.subarray(-Number(query.get('stdout_tail'))).toString('base64'), next_offset: stdout.length } } : {}),
      ...(query.has('stderr_offset') ? { stderr: { data_base64: 'eA==', next_offset: 2 } } : {}),
    })
  }))
  const result = await client.run('command', { op: 'get', name: 'Demo', id: '0', stdout_tail: 4, stderr_offset: 1, include_input: true })
  assert.equal(seen.at(-1), '/projects/Demo/commands/0?include_input=1&stdout_tail=4&stderr_offset=1&limit=4096')
  assert.deepEqual(result.stdout, { text: '中!', next_offset: 5, remaining: 'caught_up' })
  assert.equal(result.started_at, 1700000000000)
  assert.equal(result.finished_at, null)
  assert.equal(result.command, 'echo private')
  assert.deepEqual(result.next_get, { op: 'get', name: 'Demo', id: '0', stdout_offset: 5, stderr_offset: 2 })
  const action = await client.run('action', { op: 'wait', name: 'Demo', include_input: true, stdout_tail: 1, encoding: 'base64' })
  assert.equal(seen.at(-1), '/projects/Demo/actions?include_input=1&stdout_tail=1')
  assert.equal(action.action_id, 'a'.repeat(32))
  assert.equal(action.finished_at, 1700000000050)
  assert.deepEqual(action.stdout, { data_base64: 'IQ==', next_offset: 5, remaining: 'complete' })
  const input = await client.run('command', { op: 'get', id: '0', status_only: true, include_input: true })
  assert.equal(seen.at(-1), '/commands/0?include_input=1')
  assert.equal(input.command, 'echo private')
  const count = seen.length
  for (const options of [
    { stdout_offset: 0, stdout_tail: 4 }, { stdout_tail: 4, limit: 4096 },
    { status_only: true, stderr_tail: 4 }, { stdout_tail: 8193 },
    { stdout_tail: 1 }, { include_input: 1 },
  ]) assert.equal((await client.run('command', { op: 'get', id: '0', ...options })).error.code, 'invalid_input')
  assert.equal(seen.length, count)
})

test('command cancel is a bodyless scoped POST with status lookup, not DELETE', async t => {
  const client = await fixture(t)
  const seen = []
  await client.configure(await server(t, (req, res, body) => {
    seen.push([req.method, req.url, body.length])
    if (req.method === 'GET') return json(res, { status: 'succeeded' })
    res.writeHead(204)
    res.end()
  }))
  const tool = client.tools.get('winrunner_command')
  assert(tool.parameters.properties.op.enum.includes('cancel'))
  assert.equal(tool.isConcurrencySafe({ op: 'cancel' }), false)
  const call = { op: 'cancel', params_json: '{"name":"Demo","id":"0"}' }
  for (let i = 0; i < 2; i++) {
    const result = await client.run('command', call)
    assert.equal(result.http_status, 204)
    assert.equal(result.status, undefined)
    assert.deepEqual(result.next_get, { op: 'get', params_json: '{"name":"Demo","id":"0","status_only":true}' })
    assert.equal((await client.run('command', result.next_get)).status, 'succeeded')
  }
  await client.run('command', { op: 'cancel', id: '0' })
  await client.run('command', { op: 'delete', id: '0' })
  assert.deepEqual(seen, [
    ['POST', '/projects/Demo/commands/0/cancel', 0], ['GET', '/projects/Demo/commands/0', 0],
    ['POST', '/projects/Demo/commands/0/cancel', 0], ['GET', '/projects/Demo/commands/0', 0],
    ['POST', '/commands/0/cancel', 0], ['DELETE', '/commands/0', 0],
  ])
})

test('unsupported new APIs on old runners do not trigger fallback requests', async t => {
  const client = await fixture(t)
  const seen = []
  await client.configure(await server(t, (req, res) => {
    seen.push([req.method, req.url])
    if (req.url.endsWith('/cancel')) return json(res, { error: { code: 'not_found', message: 'Route not found' } }, 404)
    json(res, { error: { code: 'invalid_request', message: req.url.includes('status=') ? 'Unexpected query parameters' : 'Unexpected query parameter' } }, 400)
  }))
  assert.match((await client.run('command', { op: 'cancel', id: '0' })).note, /没有尝试 delete/)
  assert.match((await client.run('command', { op: 'list', status: 'active' })).note, /按状态筛选/)
  assert.match((await client.run('action', { op: 'wait', name: 'Demo', stdout_tail: 4 })).note, /日志尾读/)
  assert.equal((await client.run('command', { op: 'wait', id: '0', status_only: true, include_input: true })).http_status, 400)
  assert.deepEqual(seen, [
    ['POST', '/commands/0/cancel'], ['GET', '/commands?status=active'],
    ['GET', '/projects/Demo/actions?include_input=1&stdout_tail=4'], ['GET', '/commands/0?include_input=1'],
  ])
})

test('surfaces the Runner authentication ban and Retry-After without retrying', async t => {
  const client = await fixture(t)
  const seen = []
  await client.configure(await server(t, (req, res) => {
    seen.push([req.method, req.url])
    json(res, { error: { code: 'auth_ip_banned', message: 'Authentication temporarily blocked' } }, 429, { 'retry-after': '7' })
  }))
  for (const [op, args] of [['list', { status: 'active' }], ['cancel', { id: '0' }]]) {
    const result = await client.run('command', { op, ...args })
    assert.deepEqual([result.outcome, result.error.code, result.http_status, result.retry_after_seconds], ['request_error', 'auth_ip_banned', 429, 7])
  }
  assert.deepEqual(seen, [['GET', '/commands?status=active'], ['POST', '/commands/0/cancel']])
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
  assert.equal((await client.run('command', { ...args, limit: 1 })).error.code, 'invalid_input')
  const tiny = (await client.run('command', { ...args, stdout_offset: first.next_offset, limit: 1, encoding: 'base64' })).stdout
  assert.equal(tiny.next_offset, first.next_offset + 1)
})

test('keeps a split UTF-8 character at live log EOF until it completes', async t => {
  const client = await fixture(t)
  const bytes = Buffer.from('中')
  let size = 1
  let status = 'running'
  await client.configure(await server(t, (req, res) => {
    const offset = Number(new URL(req.url, 'http://localhost').searchParams.get('stdout_offset'))
    const page = bytes.subarray(offset, size)
    json(res, { id: '0', status, stdout_bytes: size, stdout: { next_offset: size, data_base64: page.toString('base64') } })
  }))
  const args = { op: 'get', id: '0', stdout_offset: 0 }
  const first = await client.run('command', args)
  assert.deepEqual(first.stdout, { text: '', next_offset: 0, remaining: 'caught_up' })
  assert.deepEqual(first.next_wait, { op: 'wait', id: '0', stdout_offset: 0 })
  status = 'succeeded'
  const incomplete = await client.run('command', args)
  assert.deepEqual(incomplete.stdout, { text: '\ufffd', next_offset: 1, remaining: 'complete' })
  size = bytes.length
  const finished = await client.run('command', first.next_wait)
  assert.deepEqual(finished.stdout, { text: '中', next_offset: size, remaining: 'complete' })
})

test('handles empty files, bounded byte ranges, and small directory pages', async t => {
  const client = await fixture(t)
  const bytes = Buffer.from([0, 1, 2, 3, 0x80, 0xa0, 0xff])
  const chinese = Buffer.from('abc中文!')
  const entries = Array.from({ length: 101 }, (_, i) => ({ name: `${i}.txt`, type: 'file', size_bytes: 3 }))
  const base = await server(t, (req, res) => {
    const path = new URL(req.url, 'http://localhost').searchParams.get('path')
    const content = path === 'utf8' ? chinese : bytes
    if (!path) { json(res, { path: '', entries }); return }
    if (path === 'empty') { res.writeHead(200, { 'content-type': 'application/octet-stream', 'content-length': 0 }); res.end(); return }
    const match = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range ?? '')
    if (match) {
      const start = Number(match[1])
      const end = Math.min(content.length - 1, Number(match[2]))
      const block = content.subarray(start, end + 1)
      res.writeHead(206, { 'content-type': 'application/octet-stream', 'content-length': block.length, 'content-range': `bytes ${start}-${end}/${content.length}` })
      res.end(block)
    } else {
      res.writeHead(200, { 'content-type': 'application/octet-stream', 'content-length': content.length }); res.end(content)
    }
  })
  await client.configure(base)
  const args = { op: 'files', name: 'demo' }
  const root = await client.run('project', args)
  assert.equal(root.entries.length, 100)
  assert.equal(root.next_entry_offset, 100)
  assert.equal((await client.run('project', root.next_get)).entries[0].name, '100.txt')
  const u = await client.run('project', { op: 'files', params_json: '{"name":"demo","path":"utf8","length":4}' })
  assert.equal(u.text, 'abc')
  assert.equal(u.next_offset, 3)
  assert.deepEqual(u.next_get, { op: 'files', params_json: '{"name":"demo","path":"utf8","length":4,"offset":3}' })
  const v = await client.run('project', u.next_get)
  const w = await client.run('project', v.next_get)
  assert.equal(u.text + v.text + w.text, chinese.toString())
  assert.equal(w.next_get, undefined)
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

test('uses server directory cursors and falls back to plain reads only for an auto-detected file', async t => {
  const client = await fixture(t)
  const queries = []
  const base = await server(t, (req, res) => {
    const q = Object.fromEntries(new URL(req.url, 'http://localhost').searchParams)
    queries.push(q)
    if (q.path === 'file.txt') {
      if (q.entry_limit) return json(res, { error: { code: 'invalid_request', message: 'entry_offset and entry_limit are only for directories' } }, 400)
      res.writeHead(200, { 'content-type': 'application/octet-stream', 'content-length': 1 }); res.end('x'); return
    }
    json(res, { path: '', entries: [{ name: q.entry_offset }], next_entry_offset: q.entry_offset === '0' ? 1 : null })
  })
  await client.configure(base)
  const first = await client.run('project', { op: 'files', params_json: '{"name":"demo","entry_limit":2}' })
  assert.deepEqual(first.next_get, { op: 'files', params_json: '{"name":"demo","entry_limit":2,"entry_offset":1}' })
  const last = await client.run('project', first.next_get)
  assert.equal(last.entries[0].name, '1')
  assert.equal(last.next_get, undefined)
  assert.equal((await client.run('project', { op: 'files', name: 'demo', path: 'file.txt' })).text, 'x')
  assert.equal((await client.run('project', { op: 'files', name: 'demo', path: 'file.txt', entry_limit: 1 })).http_status, 400)
  assert.equal((await client.run('project', { op: 'files', name: 'demo', entry_limit: 1, length: 9 })).error.code, 'invalid_input')
  assert.equal((await client.run('project', { op: 'files', name: 'demo', length: 2 })).error.code, 'invalid_input')
  assert.equal((await client.run('project', { op: 'files', name: 'demo', path: 'now-a-directory', offset: 8 })).error.code, 'not_a_file')
  assert.deepEqual(queries, [{ entry_offset: '0', entry_limit: '2' }, { entry_offset: '1', entry_limit: '2' },
    { path: 'file.txt', entry_offset: '0', entry_limit: '100' }, { path: 'file.txt' },
    { path: 'file.txt', entry_offset: '0', entry_limit: '1' }, { path: 'now-a-directory' }])
})

test('passes explicit idempotency keys without automatic retry or merging snapshot logs', async t => {
  const client = await fixture(t)
  const seen = []
  const base = await server(t, (req, res, bytes) => {
    seen.push([req.method, req.url, req.headers['idempotency-key'], JSON.parse(bytes)])
    if (req.headers['idempotency-key'] === 'lost') { req.socket.destroy(); return }
    json(res, { id: '0', status: 'succeeded', stdout_bytes: 4 })
  })
  await client.configure(base)
  const args = { op: 'start', mode: 'ps', command: 'echo ok', wait_seconds: 5, idempotency_key: 'abc-._~' }
  const first = await client.run('command', args)
  assert.equal(first.idempotency_key, args.idempotency_key)
  assert.deepEqual(first.next_get, { op: 'get', id: '0' })
  assert.equal(first.stdout, undefined)
  const lost = await client.run('command', { ...args, idempotency_key: 'lost' })
  assert.equal(lost.outcome, 'unknown')
  assert.equal(lost.idempotency_key, 'lost')
  assert.equal((await client.run('command', { ...args, idempotency_key: 'x\n' })).error.code, 'invalid_input')
  assert.equal((await client.run('action', { op: 'start', name: 'demo', commit: 'abc', interpreter: 'python', script: 'no.py', idempotency_key: 'action' })).outcome, 'succeeded')
  assert.deepEqual(seen, [
    ['POST', '/commands', 'abc-._~', { mode: 'ps', command: 'echo ok', timeout: 5 }],
    ['POST', '/commands', 'lost', { mode: 'ps', command: 'echo ok', timeout: 5 }],
    ['POST', '/projects/demo/actions', 'action', { commit: 'abc', interpreter: 'python', script: 'no.py', args: [], timeout: 0 }],
  ])
})

test('ambiguous JSON 5xx write results are unknown, but explicit Runner rejections are not', async t => {
  const client = await fixture(t)
  const seen = []
  await client.configure(await server(t, (req, res) => {
    seen.push(req.method)
    if (req.method === 'POST') {
      assert.equal(req.headers['idempotency-key'], 'same')
      return json(res, { error: { code: 'internal_error', message: 'Internal runner error' } }, 500)
    }
    if (req.method === 'DELETE') {
      res.setHeader('Retry-After', '2')
      return json(res, { error: { code: 'bad_gateway', message: 'Gateway error' } }, 502)
    }
    json(res, { error: { code: 'internal_error', message: 'Internal runner error' } }, 500)
  }))
  const start = await client.run('command', { op: 'start', mode: 'cmd', command: 'echo ok', idempotency_key: 'same' })
  assert.equal(start.outcome, 'unknown')
  assert.equal(start.outcome_unknown, true)
  assert.equal(start.idempotency_key, 'same')
  assert.equal(start.http_status, 500)
  assert.match(start.note, /原键和原参数/)
  const removed = await client.run('command', { op: 'delete', id: '0' })
  assert.equal(removed.outcome, 'unknown')
  assert.equal(removed.retry_after_seconds, 2)
  assert.equal((await client.run('command', { op: 'get', id: '0' })).outcome, 'request_error')
  assert.deepEqual(seen, ['POST', 'DELETE', 'GET'])
})

test('never follows redirects or retries mutations, and respects an already cancelled run', async t => {
  const client = await fixture(t)
  let hits = 0
  let redirectedHits = 0
  const target = await server(t, (_req, res) => { redirectedHits++; json(res, {}) })
  const base = await server(t, (req, res) => {
    hits++
    if (req.method === 'POST' && req.url === '/projects/demo') { res.writeHead(502, { 'content-type': 'text/html', 'retry-after': '5' }); res.end('<h1>proxy only</h1>'); return }
    if (req.method === 'POST' && req.url === '/projects/demo-json') { json(res, { message: 'proxy only' }, 502, { 'retry-after': '5' }); return }
    if (req.method === 'GET') { res.writeHead(302, { location: `${target}/unexpected` }); res.end(); return }
    json(res, { error: { code: 'server_busy', message: 'Too many HTTP connections' } }, 503, { 'retry-after': '1' })
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
  const jsonProxy = await client.run('project', { op: 'create', name: 'demo-json', repo_url: 'https://example.invalid/repo.git' })
  assert.deepEqual([jsonProxy.outcome, jsonProxy.http_status, jsonProxy.retry_after_seconds], ['unknown', 502, 5])
  assert.equal(jsonProxy.error.code, 'invalid_response')
  assert(!JSON.stringify(jsonProxy).includes('proxy only'))
  const busy = await client.run('command', { op: 'start', mode: 'cmd', command: 'echo ok' })
  assert.equal(busy.http_status, 503)
  assert.equal(busy.retry_after_seconds, 1)
  assert.equal((await client.run('project', { op: 'health' }, AbortSignal.abort())).error.code, 'cancelled')
  assert.equal(hits, 4)
  assert.equal(redirectedHits, 0)
})
