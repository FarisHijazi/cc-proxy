import { expect, mock, test } from 'claude-code/testing'
import type { On, SessionStartInput } from 'claude-code'

const start: SessionStartInput = { cwd: '/work', surface: 'terminal', isInteractive: true }
const sessionId = '11111111-1111-4111-8111-111111111111'
type Body = { claude_session_id: string; native_session_id: string; previous_claude_session_id?: string;
  events: { id: string; kind: string; data: unknown }[] }

function batch(sent: Body[], index = 0) {
  const body = sent[index]
  if (!body) throw new Error(`Expected event batch ${index}`)
  return body
}

function setup(on: On, response?: (body: Body, count: number) => number | Promise<number>, identity = () => sessionId,
  publisher = () => ({ pid: 1234, sessionId: identity(), bridgeSessionId: 'session_' + 'a'.repeat(32) })) {
  const clock = mock.clock(on)
  const sent: Body[] = []
  mock.env(on, {
    CC_PROXY_SESSION_EVENTS_URL: 'http://127.0.0.1:9000/v1/code/sessions/mod-events',
    CC_PROXY_SESSION_EVENTS_KEY_FILE: '/private/toy-key',
    CLAUDE_CONFIG_DIR: '/private/profile',
  })
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: identity() }))
  on('process.hookEnv', () => ({ value: { CLAUDE_PID: '1234' } }))
  on('fs.read', (_$, e) => ({ value: e.path === '/private/toy-key' ? 'toy-key-with-more-than-thirty-two-bytes' :
    JSON.stringify(publisher()) }))
  on('http.fetch', async (_$, e) => {
    const body = JSON.parse(String(e.init?.body)) as Body
    sent.push(body)
    const status = await (response?.(body, sent.length) ?? 200)
    return { value: { status, ok: status === 200, headers: {}, text: JSON.stringify({
      claude_session_id: body.claude_session_id, session_id: body.native_session_id }) } }
  })
  return { clock, sent }
}

test('observes a tool once without changing input, result, or permissions', async ($, on) => {
  const { clock, sent } = setup(on)
  const calls: string[] = []
  on('tool.call', { tool: 'Bash' }, (_$, e) => { calls.push(e.command); return { result: 'native result' } })
  await $.session.start(start)
  expect(await $.tool.call({ tool: 'Bash', command: 'printf toy' })).toEqual({ result: 'native result' })
  await clock.advance(500)
  expect(calls).toEqual(['printf toy'])
  expect(sent.length).toBe(1)
  expect(batch(sent).claude_session_id).toBe(sessionId)
  expect(batch(sent).native_session_id).toBe('cse_' + 'a'.repeat(32))
  expect(batch(sent).events.map(e => e.kind)).toEqual(['tool.call', 'tool.result'])
})

test('uncertain event delivery retries the frozen batch without rerunning the tool', async ($, on) => {
  const { clock, sent } = setup(on, (_body, count) => count === 1 ? 503 : 200)
  let calls = 0
  on('tool.call', () => { calls++; return { result: 'ok' } })
  await $.session.start(start)
  await $.tool.call({ tool: 'Bash', command: 'printf toy' })
  await clock.advance(1000)
  expect(calls).toBe(1)
  expect(sent.length).toBe(2)
  expect(sent[0]).toEqual(sent[1])
})

test('authorization failure stops publishing but preserves native execution', async ($, on) => {
  const { clock, sent } = setup(on, () => 401)
  let calls = 0
  on('tool.call', () => { calls++; return { result: 'ok' } })
  await $.session.start(start)
  await $.tool.call({ tool: 'Bash', command: 'printf first' })
  await clock.advance(1000)
  await $.tool.call({ tool: 'Bash', command: 'printf second' })
  await clock.advance(1000)
  expect(calls).toBe(2)
  expect(sent.length).toBe(1)
})

test('headless runs do not create a publisher or send session content', async ($, on) => {
  const { clock, sent } = setup(on)
  on('tool.call', () => ({ result: 'ok' }))
  await $.session.start({ ...start, surface: null, isInteractive: false })
  await $.tool.call({ tool: 'Bash', command: 'printf toy' })
  await clock.advance(1000)
  expect(sent).toEqual([])
})

test('oversized tool output produces an explicit gap', async ($, on) => {
  const { clock, sent } = setup(on)
  on('tool.call', () => ({ result: 'x'.repeat(30000) }))
  await $.session.start(start)
  const result = await $.tool.call({ tool: 'Bash', command: 'printf toy' })
  await clock.advance(500)
  expect(result.result).toBe('x'.repeat(30000))
  expect(batch(sent).events[1]).toMatchObject({ kind: 'gap', data: { reason: 'event_too_large', kind: 'tool.result' } })
})

test('a native denial is observed and kept as a denial', async ($, on) => {
  const { clock, sent } = setup(on)
  on('tool.call', () => ({ deny: 'Native permission required' }))
  await $.session.start(start)
  expect(await $.tool.call({ tool: 'Bash', command: 'printf toy' })).toEqual({ deny: 'Native permission required' })
  await clock.advance(500)
  expect(batch(sent).events[1]).toMatchObject({ kind: 'tool.result', data: { result: { deny: 'Native permission required' } } })
})

test('deep structured results become gaps without disabling later observations', async ($, on) => {
  const { clock, sent } = setup(on)
  let deep: unknown = 'native value'
  for (let n = 0; n < 35; n++) deep = { nested: deep }
  let calls = 0
  on('tool.call', () => ({ result: ++calls === 1 ? deep : 'ordinary result' }))
  await $.session.start(start)
  expect((await $.tool.call({ tool: 'Bash', command: 'printf first' })).result).toEqual(deep)
  await clock.advance(500)
  await $.tool.call({ tool: 'Bash', command: 'printf second' })
  await clock.advance(500)
  expect(batch(sent).events[1]).toMatchObject({ kind: 'gap', data: { reason: 'unsupported_event_data' } })
  expect(batch(sent, 1).events[1]).toMatchObject({ kind: 'tool.result', data: { result: { result: 'ordinary result' } } })
})

test('resume keeps observation active and never relabels the previous session', async ($, on) => {
  let identity = sessionId
  const { clock, sent } = setup(on, undefined, () => identity)
  on('tool.call', () => ({ result: 'ok' }))
  on('session.end', (_$, e) => ({ sessionId: e.sessionId }))
  await $.session.start(start)
  await $.tool.call({ tool: 'Bash', command: 'printf old' })
  await clock.advance(500)
  await $.session.end({ reason: 'resume', sessionId, resume: { id: sessionId } })
  identity = '22222222-2222-4222-8222-222222222222'
  await $.tool.call({ tool: 'Bash', command: 'printf new' })
  await clock.advance(1000)
  const newest = sent.filter(body => body.claude_session_id === identity)
  expect(newest.length).toBe(1)
  expect(batch(newest).events.some(event => event.kind === 'tool.call')).toBe(true)
  expect(batch(newest).previous_claude_session_id).toBe(sessionId)
  expect(JSON.stringify(newest)).not.toContain('printf old')
})

test('a changed result object cannot change a retried event batch', async ($, on) => {
  const { clock, sent } = setup(on, (_body, count) => count === 1 ? 503 : 200)
  const native = { text: 'original' }
  on('tool.call', () => ({ result: native }))
  await $.session.start(start)
  await $.tool.call({ tool: 'Bash', command: 'printf toy' })
  await clock.advance(250)
  native.text = 'changed after returning'
  await clock.advance(750)
  expect(sent.length).toBe(2)
  expect(sent[1]).toEqual(sent[0])
  expect(JSON.stringify(sent[1])).not.toContain('changed after returning')
})

test('stream observation preserves every native chunk and final result', async ($, on) => {
  const { clock, sent } = setup(on)
  let requests = 0
  on('turn.step', async function* (_$, e) {
    requests++
    yield { kind: 'text', index: 0, text: 'native chunk' }
    return { turnId: e.turnId, index: e.index, answer: 'native chunk', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  await $.session.start(start)
  const stream = $.turn.step({ turnId: 'turn-1', index: 0, model: 'haiku', messageCount: 1 })
  expect(await stream.next()).toMatchObject({ done: false, value: { kind: 'text', text: 'native chunk' } })
  expect(await stream.next()).toMatchObject({ done: true, value: { answer: 'native chunk' } })
  await clock.advance(500)
  expect(requests).toBe(1)
  expect(batch(sent).events[0]).toMatchObject({ kind: 'turn.step', data: { turnId: 'turn-1', chunk: { text: 'native chunk' } } })
})

test('closing observation closes the native generator once without restarting it', async ($, on) => {
  setup(on)
  let requests = 0, closed = 0
  on('turn.step', async function* (_$, e) {
    requests++
    try {
      yield { kind: 'text', index: 0, text: 'first' }
      yield { kind: 'text', index: 0, text: 'second' }
      return { turnId: e.turnId, index: e.index, answer: 'firstsecond', toolUses: [], stopReason: 'end_turn', usage: null }
    } finally { closed++ }
  })
  await $.session.start(start)
  const stream = $.turn.step({ turnId: 'turn-1', index: 0, model: 'haiku', messageCount: 1 })
  await stream.next()
  await stream.return({ turnId: 'turn-1', index: 0, answer: '', toolUses: [], stopReason: null, usage: null })
  expect([requests, closed]).toEqual([1, 1])
})

test('an unrelated process publisher cannot attach events to a native session', async ($, on) => {
  let pid = 9999
  const { clock, sent } = setup(on, undefined, undefined,
    () => ({ pid, sessionId, bridgeSessionId: 'session_' + 'a'.repeat(32) }))
  on('tool.call', () => ({ result: 'ok' }))
  await $.session.start(start)
  await $.tool.call({ tool: 'Bash', command: 'printf toy' })
  await clock.advance(500)
  expect(sent).toEqual([])
  pid = 1234
  await clock.advance(500)
  expect(sent.length).toBe(1)
})

test('a stale conversation publisher buffers events until the exact identity appears', async ($, on) => {
  let published = '22222222-2222-4222-8222-222222222222'
  const { clock, sent } = setup(on, undefined, undefined,
    () => ({ pid: 1234, sessionId: published, bridgeSessionId: 'session_' + 'a'.repeat(32) }))
  on('tool.call', () => ({ result: 'ok' }))
  await $.session.start(start)
  await $.tool.call({ tool: 'Bash', command: 'printf toy' })
  await clock.advance(500)
  expect(sent).toEqual([])
  published = sessionId
  await clock.advance(500)
  expect(sent.length).toBe(1)
})

test('an old in-flight rejection cannot disable a resumed publisher', async ($, on) => {
  let identity = sessionId
  let finish!: (status: number) => void
  const pending = new Promise<number>(resolve => { finish = resolve })
  const { clock, sent } = setup(on, (_body, count) => count === 1 ? pending : 200, () => identity)
  on('tool.call', () => ({ result: 'ok' }))
  await $.session.start(start)
  await $.tool.call({ tool: 'Bash', command: 'printf old' })
  await clock.advance(250)
  identity = '22222222-2222-4222-8222-222222222222'
  await $.tool.call({ tool: 'Bash', command: 'printf new' })
  await clock.advance(250)
  expect(sent.length).toBe(1)
  finish(409)
  await clock.advance(1000)
  expect(batch(sent, 1).claude_session_id).toBe(identity)
})

test('unpublished intermediate UUIDs do not replace the acknowledged binding', async ($, on) => {
  let identity = sessionId
  const { clock, sent } = setup(on, undefined, () => identity)
  on('tool.call', () => ({ result: 'ok' }))
  await $.session.start(start)
  await $.tool.call({ tool: 'Bash', command: 'printf first' })
  await clock.advance(500)
  identity = '22222222-2222-4222-8222-222222222222'
  await $.tool.call({ tool: 'Bash', command: 'printf intermediate' })
  identity = '33333333-3333-4333-8333-333333333333'
  await $.tool.call({ tool: 'Bash', command: 'printf last' })
  await clock.advance(500)
  expect(batch(sent, 1).previous_claude_session_id).toBe(sessionId)
})

test('an uncertain intermediate binding settles before the next UUID binds', async ($, on) => {
  let identity = sessionId
  const { clock, sent } = setup(on, (_body, count) => count === 2 ? 503 : 200, () => identity)
  on('tool.call', () => ({ result: 'ok' }))
  await $.session.start(start)
  await $.tool.call({ tool: 'Bash', command: 'printf first' })
  await clock.advance(500)
  identity = '22222222-2222-4222-8222-222222222222'
  await $.tool.call({ tool: 'Bash', command: 'printf uncertain' })
  await clock.advance(250)
  identity = '33333333-3333-4333-8333-333333333333'
  await $.tool.call({ tool: 'Bash', command: 'printf last' })
  await clock.advance(1000)
  expect(batch(sent, 2)).toEqual(batch(sent, 1))
  expect(batch(sent, 3).previous_claude_session_id).toBe('22222222-2222-4222-8222-222222222222')
})

test('a tool result cannot be relabeled after the conversation changes', async ($, on) => {
  let identity = sessionId
  let finish!: (value: { result: string }) => void
  let started!: () => void
  const entered = new Promise<void>(resolve => { started = resolve })
  const pending = new Promise<{ result: string }>(resolve => { finish = resolve })
  const { clock, sent } = setup(on, undefined, () => identity)
  on('tool.call', { tool: 'Bash' }, (_$, e) => {
    if (e.command === 'printf old') { started(); return pending }
    return { result: 'new result' }
  })
  await $.session.start(start)
  const old = $.tool.call({ tool: 'Bash', command: 'printf old' })
  await entered
  await clock.advance(500)
  identity = '22222222-2222-4222-8222-222222222222'
  await $.tool.call({ tool: 'Bash', command: 'printf new' })
  finish({ result: 'old result' })
  expect(await old).toEqual({ result: 'old result' })
  await clock.advance(500)
  const newest = sent.filter(body => body.claude_session_id === identity)
  expect(JSON.stringify(newest)).not.toContain('old result')
  expect(JSON.stringify(newest)).toContain('new result')
})
