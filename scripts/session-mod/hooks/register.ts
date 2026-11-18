import type { Hook, Register, Timer } from 'claude-code'

type API = Parameters<Hook<'session.start'>>[0]
type Event = { kind: string; data: unknown }
type Batch = {
  version: 1; claude_session_id: string; native_session_id: string;
  previous_claude_session_id?: string; events: (Event & { id: string })[]
}
type Publisher = {
  sessionId: string; producer: string; sequence: number; queue: Event[];
  bytes: number; lost: number; batch?: Batch; busy: boolean; previousId?: string
}

const MAX_EVENT = 24 * 1024
const MAX_QUEUE = 128 * 1024
const MAX_BATCH = 16
let endpoint = ''
let keyFile = ''
let publisherFile = ''
let claudePid = ''
let publisher: Publisher | undefined
let timer: Timer | undefined
let status = 'disabled'
let accepted = 0
let flushing = false
let bound: { nativeId: string; sessionId: string } | undefined
let pending: Publisher | undefined

function uuid() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.floor(Math.random() * 16)
    return (c === 'x' ? r : (r & 3) | 8).toString(16)
  })
}

function byteSize(value: unknown) {
  // JSON escaped ASCII is one byte; four bytes per UTF-16 unit is an upper bound.
  return JSON.stringify(value).length * 4
}

function serializable(value: unknown, depth = 0): boolean {
  if (depth > 24) return false
  if (value === null || value === undefined || typeof value === 'boolean') return true
  if (typeof value === 'number') return Number.isFinite(value)
  if (typeof value === 'string') return !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value)
  if (typeof value !== 'object') return false
  return Object.values(value).every(child => serializable(child, depth + 1))
}

async function owner($: API) {
  const sessionId = await $.session.id()
  if (publisher?.sessionId !== sessionId) {
    const lost = publisher ? publisher.queue.length : 0
    publisher = { sessionId, producer: uuid(), sequence: 0, queue: [], bytes: 0, lost, busy: false,
      ...(publisher ? { previousId: publisher.sessionId } : {}) }
  }
  return publisher
}

function enqueue(p: Publisher, kind: string, data: unknown) {
  if (!endpoint) return
  if (publisher !== p) { if (publisher) publisher.lost++; return }
  let event: Event = { kind, data }
  try {
    if (!serializable(data)) event = { kind: 'gap', data: { reason: 'unsupported_event_data', kind } }
    else if (byteSize(event) > MAX_EVENT) event = { kind: 'gap', data: { reason: 'event_too_large', kind } }
  } catch {
    event = { kind: 'gap', data: { reason: 'unsupported_event_data', kind } }
  }
  event = JSON.parse(JSON.stringify(event))
  const size = byteSize(event)
  if (p.bytes + size > MAX_QUEUE || p.queue.length >= 256) {
    p.lost += 1
    return
  }
  if (p.lost && p.bytes + size + 512 < MAX_QUEUE) {
    const gap = { kind: 'gap', data: { reason: 'publisher_overflow_or_session_change', count: p.lost } }
    p.queue.push(gap)
    p.bytes += byteSize(gap)
    p.lost = 0
  }
  p.queue.push(event)
  p.bytes += size
}

async function observe($: API, kind: string, data: unknown, pinned?: Publisher) {
  try {
    if (endpoint) {
      const p = pinned ?? await owner($)
      if (publisher !== p) { if (publisher) publisher.lost++; return }
      enqueue(p, kind, data)
      return p
    }
  } catch {
    status = 'event observation failed; native action unchanged'
  }
}

async function flush($: API) {
  if (publisher?.lost && !publisher.queue.length) {
    const count = publisher.lost
    publisher.lost = 0
    enqueue(publisher, 'gap', { reason: 'publisher_dropped_events', count })
  }
  // Settle an uncertain binding batch before a later conversation can bind.
  // This retries observations only, never a native tool or user command.
  const p = pending ?? publisher
  if (!endpoint || !p || p.busy || flushing || (!p.batch && !p.queue.length)) return
  flushing = true
  p.busy = true
  try {
    if (!p.batch) {
      // The native wire ID differs from the transcript UUID. Only the engine's
      // own per-process publisher can join them; never search by cwd or title.
      const identity = JSON.parse(await $.fs.read(publisherFile, { length: 16384 }))
      if (publisher !== p) return
      if (String(identity.pid) !== claudePid || identity.sessionId !== p.sessionId ||
          !/^session_[0-9a-f]{32}$/.test(identity.bridgeSessionId ?? '')) {
        status = 'waiting for native session identity'
        return
      }
      const events = p.queue.splice(0, MAX_BATCH)
      p.bytes -= events.reduce((n, e) => n + byteSize(e), 0)
      const nativeId = `cse_${identity.bridgeSessionId.slice(8)}`
      const previousId = bound?.nativeId === nativeId ? bound.sessionId : p.previousId
      p.batch = {
        version: 1, claude_session_id: p.sessionId,
        native_session_id: nativeId,
        ...(previousId ? { previous_claude_session_id: previousId } : {}),
        events: events.map(e => ({ ...e, id: `${p.producer}:${++p.sequence}` })),
      }
      pending = p
    }
    const secret = (await $.fs.read(keyFile, { length: 1025 })).trim()
    if (!/^[\x21-\x7e]{32,1024}$/.test(secret)) {
      status = 'invalid event credential'; endpoint = ''; return
    }
    const result = await $.http.fetch(endpoint, {
      method: 'POST', headers: { 'content-type': 'application/json', 'x-hq-native-key': secret },
      body: JSON.stringify(p.batch),
    })
    if (result.ok) {
      const receipt = JSON.parse(result.text)
      if (receipt.claude_session_id !== p.sessionId || receipt.session_id !== p.batch.native_session_id) {
        status = 'invalid event receipt'; endpoint = ''
        return
      }
      bound = { nativeId: p.batch.native_session_id, sessionId: p.sessionId }
      if (publisher === p) accepted += p.batch.events.length
      p.batch = undefined
      pending = undefined
      if (publisher === p) status = 'connected'
    } else if (publisher !== p && result.status >= 400 && result.status < 500 && result.status !== 429) {
      if (publisher) publisher.lost += p.batch.events.length
      p.batch = undefined
      pending = undefined
      if (result.status === 401 || result.status === 403) {
        status = `event publishing stopped (HTTP ${result.status})`; endpoint = ''
      }
      return
    } else if (result.status === 404 || result.status >= 500 || result.status === 429) {
      status = result.status === 404 ? 'waiting for native session identity' : 'gateway unavailable; buffered'
    } else {
      status = `event publishing stopped (HTTP ${result.status})`
      endpoint = ''
    }
  } catch {
    if (publisher === p) status = 'event delivery uncertain; retrying the same event IDs'
  } finally {
    p.busy = false
    flushing = false
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    timer?.cancel()
    endpoint = ''
    publisherFile = ''
    publisher = undefined
    pending = undefined
    accepted = 0
    bound = undefined
    status = e.isInteractive ? 'disabled' : 'interactive sessions only'
    try {
      const configured = await $.env.get('CC_PROXY_SESSION_EVENTS_URL')
      keyFile = await $.env.get('CC_PROXY_SESSION_EVENTS_KEY_FILE') ?? ''
      if (e.isInteractive && configured && keyFile) {
        const url = new URL(configured)
        const secure = url.protocol === 'https:' ||
          (url.protocol === 'http:' && ['127.0.0.1', '[::1]', 'localhost'].includes(url.hostname))
        if (!secure || url.username || url.password || url.search || url.hash ||
            url.pathname !== '/v1/code/sessions/mod-events' || !keyFile.startsWith('/')) {
          status = 'invalid event configuration'
        } else {
          const config = await $.env.get('CLAUDE_CONFIG_DIR')
          claudePid = (await $.process.hookEnv()).CLAUDE_PID ?? ''
          if (!config?.startsWith('/') || !/^[1-9][0-9]*$/.test(claudePid)) {
            status = 'missing native process identity'
          } else {
            publisherFile = `${config.replace(/\/$/, '')}/sessions/${claudePid}.json`
            endpoint = url.href
            status = 'waiting for native session identity'
            await owner($)
            timer = $.clock.every(250, () => { void flush($) })
          }
        }
      }
      await $.command.register({ name: 'cc-session-status', description: 'Show the existing fleet event connection', immediate: true })
    } catch {
      endpoint = ''
      status = 'event configuration unavailable; native session unchanged'
    }
    return next(e)
  })
  on('command.run', { command: 'cc-session-status' }, async () => ({
    text: `Fleet events: ${status}; accepted ${accepted}; buffered ${(publisher?.queue.length ?? 0) + (publisher?.batch?.events.length ?? 0)}`,
  }))
  on('prompt.submit', async ($, e, next) => {
    await observe($, 'prompt.submit', e)
    return next(e)
  })
  on('turn.start', async ($, e, next) => {
    await observe($, 'turn.start', e)
    return next(e)
  })
  on('turn.step', async function* ($, e, next) {
    let p: Publisher | undefined
    try { if (endpoint) p = await owner($) } catch { status = 'event observation unavailable' }
    const stream = next(e)
    try {
      while (true) {
        const part = await stream.next()
        if (part.done) return part.value
        if (p && part.value.kind !== 'engine') {
          enqueue(p, 'turn.step', { turnId: e.turnId, index: e.index, agentId: e.agentId, chunk: part.value })
        }
        yield part.value
      }
    } finally {
      // Returning closes the engine iterator on cancellation; never replay a model request.
      await stream.return(undefined as never)
    }
  })
  on('tool.call', async ($, e, next) => {
    const p = await observe($, 'tool.call', e)
    const result = await next(e)
    if (p) await observe($, 'tool.result', { tool: e.tool, tool_use_id: e.tool_use_id, agentId: e.agentId, result }, p)
    return result
  })
  on('session.append', async ($, e, next) => {
    let p: Publisher | undefined
    try { if (endpoint) p = await owner($) } catch { status = 'event observation unavailable' }
    const result = await next(e)
    if (p) await observe($, 'session.append', e, p)
    return result
  })
  on('turn.complete', async ($, e, next) => {
    await observe($, 'turn.complete', e)
    void flush($)
    return next(e)
  })
  on('session.end', async ($, e, next) => {
    await observe($, 'session.end', e)
    if (e.reason !== 'clear' && e.reason !== 'resume') timer?.cancel()
    void flush($)
    return next(e)
  })
}
