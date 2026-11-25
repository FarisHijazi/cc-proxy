import type { HttpInit, HttpResponse, TurnStepInput } from 'claude-code'
export type Serving = { model: string; account: string; provider: string; identity: string; direct?: true }
const backends = new Set(['codex', 'gemini', 'cursor', 'cursor-cli', 'kimi', 'grok', 'zai', 'local', 'agy'])
export function parseServing(value: string | undefined): Serving | undefined {
  if (!value || value.length > 512 || /[\u0000- \u007f]/.test(value)) return
  const slash = value.indexOf('/')
  const account = value.slice(0, slash), model = value.slice(slash + 1)
  if (slash < 1 || !/^[a-zA-Z0-9._:@+-]+$/.test(account) || !/^[a-zA-Z0-9._:[\]-]{1,128}$/.test(model)) return
  let hash = 2166136261
  for (const character of account) hash = Math.imul(hash ^ character.charCodeAt(0), 16777619)
  const label = backends.has(account) || account === 'console-api-key' ? account : `account-${(hash >>> 0).toString(16)}`
  return { model, account: label, provider: backends.has(account) ? account : 'Anthropic', identity: `${label}/${model}` }
}
export function tracker() {
  return { requested: 'unknown', generation: 0, send: 0, pending: false, failure: '',
    actual: undefined as Serving | undefined, last: undefined as Serving | undefined,
    active: undefined as TurnStepInput | undefined, agents: new Map<string, string>() }
}
export type Tracker = ReturnType<typeof tracker>
export function reset(state: Tracker, requested = state.requested, clearHistory = false) {
  state.generation++; state.send++
  state.requested = requested
  state.actual = undefined; state.pending = false; state.failure = ''; state.active = undefined
  state.agents.clear()
  if (clearHistory) state.last = undefined
}
export function begin(state: Tracker, step: TurnStepInput) {
  if (step.agentId) { state.agents.set(step.agentId, step.model); return }
  state.generation++
  state.requested = step.model; state.active = step; state.actual = undefined; state.pending = true; state.failure = ''
}
export function finish(state: Tracker, step: TurnStepInput) {
  if (step.agentId) state.agents.delete(step.agentId)
  else if (state.active === step) { state.active = undefined; state.pending = false }
}
export function isMainRequest(state: Tracker, url: string, init?: HttpInit): boolean {
  try {
    if (!state.active || new URL(url).pathname !== '/v1/messages' || init?.method?.toUpperCase() !== 'POST' || typeof init.body !== 'string') return false
    const headers = Object.fromEntries(Object.entries(init.headers ?? {}).map(([key, value]) => [key.toLowerCase(), value]))
    const envelope = JSON.parse(init.body)
    // Tool-less background calls have no public turn ID. Ambiguity stays unknown.
    return !headers['x-claude-code-agent-id'] && !headers['x-claude-code-parent-agent-id'] &&
      envelope.model === state.active.model && Array.isArray(envelope.tools) && envelope.tools.length > 0 &&
      ![...state.agents.values()].includes(state.active.model)
  } catch { return false }
}
export function sent(state: Tracker) {
  state.send++; state.actual = undefined; state.pending = true; state.failure = ''
  return { generation: state.generation, send: state.send }
}
export type Ticket = ReturnType<typeof sent>
export function answered(state: Tracker, ticket: Ticket, response?: HttpResponse): string | undefined {
  if (ticket.generation !== state.generation || ticket.send !== state.send) return
  state.pending = false
  state.actual = undefined
  if (!response?.ok) { state.failure = response ? `HTTP ${response.status}` : 'request failed'; return }
  const header = response.headers['x-claude-codex-served-by']
  state.actual = header === undefined ? direct(state) : parseServing(header)
  if (!state.actual) return
  const changed = state.last && state.last.identity !== state.actual.identity
  state.last = state.actual
  return changed ? `Serving changed: ${state.actual.provider} / ${state.actual.model} · ${state.actual.account}${fallback(state)}` : undefined
}
function direct(state: Tracker): Serving {
  const model = state.requested.replace(/\[1m\]$/, '')
  return { model, account: 'own login', provider: 'Anthropic', identity: `direct/${model}`, direct: true }
}
function fallback(state: Tracker) {
  const requested = state.requested.replace(/\[1m\]$/, '')
  if (!state.actual || state.actual.direct || state.actual.model === requested) return ''
  if (!requested.includes('-') && !requested.includes(':')) return ' · routed'
  return ' · fallback'
}
export function display(state: Tracker) {
  const actual = state.actual
  const served = !actual ? 'unknown' : actual.direct
    ? `${actual.provider} / ${actual.model} · own login (not routed by cc-proxy)`
    : `${actual.provider} / ${actual.model} · account ${actual.account}`
  const phase = state.pending ? ' · pending' : state.failure ? ` · ${state.failure}` : ''
  const history = !actual && state.last ? ` · last: ${state.last.provider} / ${state.last.model} · ${state.last.account} (previous response)` : ''
  return `Requested ${state.requested} → Serving ${served}${fallback(state)}${phase}${history}`
}
