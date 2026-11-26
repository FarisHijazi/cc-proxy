import type { HttpInit, HttpResponse, TurnStepInput } from 'claude-code'
export type Serving = { model: string; account: string; provider: string }
const backends = new Set(['codex', 'gemini', 'cursor', 'cursor-cli', 'kimi', 'grok', 'zai', 'local', 'agy'])
export function parseServing(value: string | undefined): Serving | undefined {
  if (!value || value.length > 512 || /[\u0000- \u007f]/.test(value)) return
  const slash = value.indexOf('/')
  const account = value.slice(0, slash), model = value.slice(slash + 1)
  if (slash < 1 || !/^[a-zA-Z0-9._:@+-]+$/.test(account) || !/^[a-zA-Z0-9._:[\]-]{1,128}$/.test(model)) return
  let hash = 2166136261
  for (const character of account) hash = Math.imul(hash ^ character.charCodeAt(0), 16777619)
  const label = backends.has(account) || account === 'console-api-key' ? account : `account-${(hash >>> 0).toString(16)}`
  return { model, account: label, provider: backends.has(account) ? account : 'Anthropic' }
}
export function tracker() {
  return { requested: 'unknown', generation: 0, send: 0,
    actual: undefined as Serving | undefined, last: undefined as Serving | undefined,
    active: undefined as TurnStepInput | undefined, agents: new Map<string, string>() }
}
export type Tracker = ReturnType<typeof tracker>
export function reset(state: Tracker, requested = state.requested, clearHistory = false) {
  state.generation++; state.send++
  state.requested = requested
  state.actual = undefined; state.active = undefined
  state.agents.clear()
  if (clearHistory) state.last = undefined
}
export function begin(state: Tracker, step: TurnStepInput) {
  if (step.agentId) { state.agents.set(step.agentId, step.model); return }
  state.generation++
  state.requested = step.model; state.active = step; state.actual = undefined
}
export function finish(state: Tracker, step: TurnStepInput) {
  if (step.agentId) state.agents.delete(step.agentId)
  else if (state.active === step) state.active = undefined
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
  state.send++; state.actual = undefined
  return { generation: state.generation, send: state.send }
}
export type Ticket = ReturnType<typeof sent>
export function answered(state: Tracker, ticket: Ticket, response?: HttpResponse): string | undefined {
  if (ticket.generation !== state.generation || ticket.send !== state.send) return
  state.actual = undefined
  if (!response?.ok) return
  const header = response.headers['x-claude-codex-served-by']
  state.actual = parseServing(header)
  if (!state.actual) return
  const changed = state.last && (state.last.account !== state.actual.account || !sameModel(state.last.model, state.actual))
  state.last = state.actual
  return changed ? `Serving changed: ${served(state, state.actual)}` : undefined
}
function sameModel(requested: string, actual: Serving) {
  const clean = (id: string) => id.replace(/\[1m\]$/i, '')
  let want = clean(requested), got = clean(actual.model)
  if (want === got) return true
  if (actual.provider === 'codex') {
    // -fast selects a service tier, not a different model. Only bare family selectors float.
    want = want.replace(/-fast$/, ''); got = got.replace(/-fast$/, '')
    const family = (id: string) => /^gpt-\d+(?:\.\d+)*-(astra|sol|luna|terra)$/.exec(id)?.[1]
    return want === got || family(want) === got || family(got) === want
  }
  if (actual.provider === 'Anthropic') {
    const family = (id: string) => /^claude-(opus|sonnet|haiku|fable)-\d+(?:-\d+)*$/.exec(id)?.[1]
    const undated = (id: string) => /^(claude-[a-z]+-\d+(?:-\d+)*)-\d{8}$/.exec(id)?.[1]
    return family(want) === got || family(got) === want || undated(want) === got || undated(got) === want
  }
  if (actual.provider === 'kimi') return want.replace(/^kimi-(?=k3(?:$|[.-]))/, '') === got.replace(/^kimi-(?=k3(?:$|[.-]))/, '')
  const prefix: Record<string, RegExp> = { agy: /^agy(?:-agent)?:/, gemini: /^(?:models\/|gemini:)/,
    cursor: /^cursor(?:-ask|-plan|-agent)?:/, 'cursor-cli': /^cursor-cli(?:-ask|-plan|-agent)?:/ }
  const route = prefix[actual.provider]
  return !!route && want.replace(route, '') === got.replace(route, '')
}
export function served(state: Tracker, actual: Serving) {
  const where = actual.provider === 'Anthropic' ? `Anthropic · account ${actual.account}` : actual.provider
  return sameModel(state.requested, actual) ? `${actual.model} · ${where}` :
    `⚠️ ${actual.model} · ${where} · requested ${state.requested}`
}
export function display(state: Tracker) {
  return state.actual ? `Serving ${served(state, state.actual)}` : undefined
}
