import { expect, mock, test } from 'claude-code/testing'
import { tracker, reset, begin, finish, isMainRequest, sent, answered, display, parseServing } from '../hooks/identity'
import type { HttpInit, HttpResponse } from 'claude-code'
const model = 'claude-opus-5-5'
const step = { turnId: 'turn-1', index: 0, model, messageCount: 59 }
const url = 'https://api.anthropic.com/v1/messages'
const request = (overrides = {}): HttpInit => ({ method: 'POST', body: JSON.stringify({ model, messages: [{ role: 'user', content: 'native request' }], tools: [{ name: 'Bash' }], ...overrides }) })
const response = (served?: string, status = 200): HttpResponse => ({ status, ok: status === 200, headers: served ? { 'x-claude-codex-served-by': served } : {}, text: '', isStreaming: true, sendIndex: 2 })

test('band is hidden until an actual answer and never appears in headless sessions', async ($, on) => {
  mock.env(on, {})
  on('session.model', () => ({ value: model }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  const band = { plugin: 'cc-proxy-session', component: 'AbovePrompt', surface: 'terminal', requestId: 'metadata', props: { hasSurvey: false, isCollapsed: false, isWorking: false, maxRows: 4, bodyColumns: 80, scroll: { offset: 0, bodyRows: 4 }, view: {} } } as const
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(band)
  expect(await ui.find({ type: 'Text' })).toBe(undefined)
  await ui.unmount()
  await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
  const headless = await $.ui.mount(band)
  expect(await headless.find({ type: 'Text' })).toBe(undefined)
  await headless.unmount()
})

test('hidden while pending; response/stream not consumed or modified', () => {
  const state = tracker(), init = request(), reply = response('codex/gpt-6.1-sol')
  expect(display(state)).toBe(undefined)
  begin(state, step)
  expect(isMainRequest(state, url, init)).toBe(true)
  const ticket = sent(state)
  expect(display(state)).toBe(undefined)
  expect(answered(state, ticket, reply)).toBe(undefined)
  finish(state, step)
  expect(display(state)).toBe('Serving ⚠️ gpt-6.1-sol · codex · requested claude-opus-5-5')
  expect(init).toEqual(request())
  expect(reply).toEqual(response('codex/gpt-6.1-sol'))
})

test('toasts only actual model/account transitions and never reveal email identities', () => {
  const state = tracker()
  begin(state, step)
  expect(answered(state, sent(state), response('codex/gpt-6.1-sol'))).toBe(undefined)
  expect(answered(state, sent(state), response('codex/gpt-6.1-sol'))).toBe(undefined)
  const toast = answered(state, sent(state), response('user@example.com/' + model))
  expect(toast).toBe(`Serving changed: ${model} · Anthropic · account ${parseServing('user@example.com/' + model)?.account}`)
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
  expect(display(state)).toBe(undefined)
})

test('missing, malformed and failed answers hide the band instead of guessing', () => {
  const state = tracker()
  begin(state, step)
  answered(state, sent(state), response('codex/gpt-6.1-sol'))
  answered(state, sent(state), response())
  expect(display(state)).toBe(undefined)
  for (const value of ['garbage', 'bad\nheader/forged', 'codex/', '/model', 'account/model/extra', 'x'.repeat(513)]) {
    expect(parseServing(value)).toBe(undefined)
    answered(state, sent(state), response('codex/gpt-6.1-sol'))
    answered(state, sent(state), response(value))
    expect(display(state)).toBe(undefined)
  }
  answered(state, sent(state), response('codex/gpt-6.1-sol'))
  answered(state, sent(state), response(undefined, 429))
  expect(display(state)).toBe(undefined)
  answered(state, sent(state))
  expect(display(state)).toBe(undefined)
})

test('late answers are ignored; the current answer wins', () => {
  const state = tracker()
  begin(state, step)
  const old = sent(state), current = sent(state)
  answered(state, old, response('codex/stale'))
  expect(display(state)).toBe(undefined)
  answered(state, current, response('kimi/kimi-k3.1-pro'))
  expect(display(state)).toBe('Serving ⚠️ kimi-k3.1-pro · kimi · requested claude-opus-5-5')
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

test('aliases, context hints, tiers and route prefixes are the requested model, not a fallback', () => {
  const cases: [string, string, string][] = [
    ['sol[1m]', 'codex/gpt-6.1-sol', 'Serving gpt-6.1-sol · codex'],
    ['gpt-6.1-sol-fast', 'codex/gpt-6.1-sol', 'Serving gpt-6.1-sol · codex'],
    ['opus', 'user@example.com/claude-opus-5-5', 'Serving claude-opus-5-5 · Anthropic · account '],
    ['claude-opus-5-5-20260101', 'user@example.com/claude-opus-5-5', 'Serving claude-opus-5-5 · Anthropic · account '],
    ['kimi-k3.1-pro', 'kimi/k3.1-pro', 'Serving k3.1-pro · kimi'],
    ['gemini:gemini-3-pro', 'gemini/gemini-3-pro', 'Serving gemini-3-pro · gemini'],
    ['cursor-cli-ask:gpt-6', 'cursor-cli/gpt-6', 'Serving gpt-6 · cursor-cli'],
  ]
  for (const [requested, served, shown] of cases) {
    const state = tracker()
    begin(state, { ...step, model: requested })
    answered(state, sent(state), response(served))
    expect(display(state)?.startsWith(shown)).toBe(true)
    expect(display(state)).not.toContain('⚠️')
  }
  const state = tracker()
  begin(state, { ...step, model: 'sol' })
  answered(state, sent(state), response('codex/gpt-6.1-luna'))
  expect(display(state)).toBe('Serving ⚠️ gpt-6.1-luna · codex · requested sol')
})

test('an alias resolving to its model is not a model change', () => {
  const state = tracker()
  begin(state, { ...step, model: 'sol' })
  answered(state, sent(state), response('codex/gpt-6.1-sol'))
  reset(state, 'gpt-6.1-sol')
  begin(state, { ...step, model: 'gpt-6.1-sol' })
  expect(answered(state, sent(state), response('codex/gpt-6.1-sol'))).toBe(undefined)
})

test('model switch invalidates in-flight answers; reload clears history', () => {
  const state = tracker()
  begin(state, step)
  answered(state, sent(state), response('codex/gpt-6.1-sol'))
  const ticket = sent(state)
  reset(state, 'glm-5.4')
  answered(state, ticket, response('codex/stale'))
  expect(display(state)).toBe(undefined)
  reset(state, 'unknown', true)
  begin(state, step)
  expect(answered(state, sent(state), response('zai/glm-5.4'))).toBe(undefined)
})

test('same model shows the name once; a different model gets a warning', () => {
  const state = tracker()
  begin(state, step)
  answered(state, sent(state), response('user@example.com/' + model))
  expect(display(state)).toMatch(new RegExp(`^Serving ${model} · Anthropic · account account-[0-9a-f]+$`))
  expect(display(state)?.split(model).length).toBe(2)
  answered(state, sent(state), response('zai/glm-5.4'))
  expect(display(state)).toBe(`Serving ⚠️ glm-5.4 · zai · requested ${model}`)
})
