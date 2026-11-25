import { expect, mock, test } from 'claude-code/testing'
import { tracker, reset, begin, finish, isMainRequest, sent, answered, display, parseServing } from '../hooks/identity'
import type { HttpInit, HttpResponse } from 'claude-code'
const model = 'claude-opus-5-5'
const step = { turnId: 'turn-1', index: 0, model, messageCount: 59 }
const url = 'https://api.anthropic.com/v1/messages'
const request = (overrides = {}): HttpInit => ({ method: 'POST', body: JSON.stringify({ model, messages: [{ role: 'user', content: 'native request' }], tools: [{ name: 'Bash' }], ...overrides }) })
const response = (served?: string, status = 200): HttpResponse => ({ status, ok: status === 200, headers: served ? { 'x-claude-codex-served-by': served } : {}, text: '', isStreaming: true, sendIndex: 2 })

test('native band renders without event transport and never appears in headless sessions', async ($, on) => {
  mock.env(on, {})
  on('session.model', () => ({ value: model }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  const band = { plugin: 'cc-proxy-session', component: 'AbovePrompt', surface: 'terminal', requestId: 'metadata', props: { hasSurvey: false, isCollapsed: false, isWorking: false, maxRows: 4, bodyColumns: 80, scroll: { offset: 0, bodyRows: 4 }, view: {} } } as const
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(band)
  expect((await ui.find({ type: 'Text' }))?.text).toBe(`Requested ${model} → Serving unknown`)
  await ui.unmount()
  await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
  const headless = await $.ui.mount(band)
  expect(await headless.find({ type: 'Text' })).toBe(undefined)
  await headless.unmount()
})

test('unknown before actual header; response/stream not consumed or modified', () => {
  const state = tracker(), init = request(), reply = response('codex/gpt-6.1-sol')
  expect(display(state)).toContain('Serving unknown')
  begin(state, step)
  expect(display(state)).toContain('pending')
  expect(isMainRequest(state, url, init)).toBe(true)
  expect(answered(state, sent(state), reply)).toBe(undefined)
  finish(state, step)
  expect(display(state)).toContain('Serving codex / gpt-6.1-sol')
  expect(display(state)).toContain('fallback')
  expect(init).toEqual(request())
  expect(reply).toEqual(response('codex/gpt-6.1-sol'))
})

test('toasts only actual model/account transitions and never reveal email identities', () => {
  const state = tracker()
  begin(state, step)
  expect(answered(state, sent(state), response('codex/gpt-6.1-sol'))).toBe(undefined)
  expect(answered(state, sent(state), response('codex/gpt-6.1-sol'))).toBe(undefined)
  const toast = answered(state, sent(state), response('user@example.com/' + model))
  expect(toast).toContain('Serving changed: Anthropic')
  expect(toast).not.toContain('user@example.com')
  expect(display(state)).toContain('account account-')
  expect(answered(state, sent(state), response('backup@example.com/' + model))).toContain('Serving changed:')
})

test('title, count, no-tools, malformed and subagent calls cannot establish main attribution', () => {
  const state = tracker()
  expect(isMainRequest(state, url, request())).toBe(false)
  begin(state, step)
  expect(isMainRequest(state, url, request({ model: 'claude-haiku-4-5' }))).toBe(false)
  expect(isMainRequest(state, url, request({ tools: [] }))).toBe(false)
  expect(isMainRequest(state, url, request({ tools: undefined }))).toBe(false)
  expect(isMainRequest(state, url + '/count_tokens', request())).toBe(false)
  expect(isMainRequest(state, url, { ...request(), headers: { 'X-Claude-Code-Agent-Id': '[redacted]' } })).toBe(false)
  expect(isMainRequest(state, url, { ...request(), headers: { 'x-claude-code-parent-agent-id': '[redacted]' } })).toBe(false)
  expect(isMainRequest(state, url, { method: 'POST', body: '{' })).toBe(false)
  finish(state, step)
  begin(state, { ...step, agentId: 'child' })
  expect(isMainRequest(state, url, request())).toBe(false)
  expect(display(state)).toContain('Serving unknown')
})

test('missing/malformed metadata is unknown with explicitly historical last confirmation', () => {
  const state = tracker()
  begin(state, step)
  answered(state, sent(state), response('codex/gpt-6.1-sol'))
  answered(state, sent(state), response('garbage'))
  expect(display(state)).toContain('Serving unknown')
  expect(display(state)).toContain('previous response')
  for (const value of ['bad\nheader/forged', 'codex/', '/model', 'account/model/extra', 'x'.repeat(513)]) {
    expect(parseServing(value)).toBe(undefined)
    answered(state, sent(state), response(value))
    expect(display(state)).toContain('Serving unknown')
  }
})

test('retry HTTP/network failures clear current; successful retry wins; late sends ignored', () => {
  const state = tracker()
  begin(state, step)
  answered(state, sent(state), response('codex/gpt-6.1-sol'))
  answered(state, sent(state), response(undefined, 429))
  expect(display(state)).toContain('HTTP 429')
  expect(display(state)).toContain('Serving unknown')
  const old = sent(state), current = sent(state)
  answered(state, old, response('codex/stale'))
  expect(display(state)).toContain('Serving unknown')
  answered(state, current, response('kimi/kimi-k3.1-pro'))
  expect(display(state)).toContain('Serving kimi / kimi-k3.1-pro')
  answered(state, sent(state))
  expect(display(state)).toContain('request failed')
})

test('simultaneous same-model agent/main is ambiguous even without lineage headers', () => {
  const state = tracker(), agent = { ...step, agentId: 'child' }
  begin(state, step)
  begin(state, agent)
  expect(isMainRequest(state, url, request())).toBe(false)
  finish(state, agent)
  expect(isMainRequest(state, url, request())).toBe(true)
  finish(state, step)
  expect(isMainRequest(state, url, request())).toBe(false)
})

test('opaque requested aliases are routed, not falsely declared fallbacks', () => {
  const state = tracker()
  begin(state, { ...step, model: 'sol[1m]' })
  answered(state, sent(state), response('codex/gpt-6.1-sol'))
  expect(display(state)).toContain('routed')
  expect(display(state)).not.toContain('fallback')
})

test('model switch invalidates in-flight answers but preserves labeled last actual answer', () => {
  const state = tracker()
  begin(state, step)
  answered(state, sent(state), response('codex/gpt-6.1-sol'))
  const ticket = sent(state)
  expect(display(state)).toContain('pending')
  expect(display(state)).toContain('previous response')
  reset(state, 'glm-5.4')
  answered(state, ticket, response('codex/stale'))
  expect(display(state)).toContain('Requested glm-5.4 → Serving unknown')
  expect(display(state)).not.toContain('stale')
  expect(display(state)).toContain('previous response')
  reset(state, 'unknown', true)
  expect(display(state)).not.toContain('previous response')
})

test('ok response without served-by header is the session own login, not unknown or fallback', () => {
  const state = tracker()
  begin(state, step)
  expect(answered(state, sent(state), response())).toBe(undefined)
  expect(display(state)).toBe(`Requested ${model} → Serving Anthropic / ${model} · own login (not routed by cc-proxy)`)
  expect(display(state)).not.toContain('fallback')
  expect(answered(state, sent(state), response('garbage'))).toBe(undefined)
  expect(display(state)).toContain('Serving unknown')
  expect(answered(state, sent(state), response())).toBe(undefined)
  expect(answered(state, sent(state), response('codex/gpt-6.1-sol'))).toContain('Serving changed: codex / gpt-6.1-sol')
  expect(display(state)).toContain('Serving codex / gpt-6.1-sol · account codex · fallback')
  expect(answered(state, sent(state), response())).toBe(`Serving changed: Anthropic / ${model} · own login`)
  answered(state, sent(state), response(undefined, 500))
  expect(display(state)).toContain('Serving unknown')
  expect(display(state)).toContain('HTTP 500')
})
