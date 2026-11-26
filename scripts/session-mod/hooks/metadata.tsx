import type { Hook, On, Timer, TurnStepChunk, TurnStepInput, TurnStepResult } from 'claude-code'
import { tracker, reset, begin, finish, isMainRequest, sent, answered, completed, display } from './identity'
import { speed, stepStarted, streamed, stepEnded, summary } from './speed'
type API = Parameters<Hook<'session.start'>>[0]
const state = tracker()
let rates = speed()
let interactive = false
let ticker: Timer | undefined
let shownRates: string | undefined

export function registerMetadata(on: On) {
  on('classic.PostModelSwitch', ($, e, next) => {
    reset(state, e.to_model)
    $.ui.invalidate('ui.render')
    return next(e)
  })
  on('http.fetch', async ($, e, next) => {
    const ticket = interactive && next.origin.plugin === 'engine' && isMainRequest(state, e.url, e.init) ? sent(state) : undefined
    if (ticket) $.ui.invalidate('ui.render')
    let result
    try { result = await next(e) }
    catch (error) {
      if (ticket) { answered(state, ticket); $.ui.invalidate('ui.render') }
      throw error
    }
    if (ticket) {
      const toast = answered(state, ticket, result.value)
      if (toast) $.ui.toast(toast)
      $.ui.invalidate('ui.render')
    }
    return result
  })
  on('ui.render', { component: 'AbovePrompt' }, ($, e, next) => {
    if (!interactive || e.props.hasSurvey) return next(e)
    shownRates = summary(rates, performance.now())
    const text = [display(state), shownRates].filter(Boolean).join(' · ')
    if (!text) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return <Box><Text dimColor>{text}</Text></Box>
  })
}
export async function startMetadata($: API, isInteractive: boolean) {
  interactive = isInteractive
  reset(state, 'unknown', true)
  rates = speed(); shownRates = undefined
  ticker?.cancel(); ticker = undefined
  if (interactive) {
    ticker = $.clock.every(1000, () => {
      if (summary(rates, performance.now()) !== shownRates) $.ui.invalidate('ui.render')
    })
    try { state.requested = await $.session.model() } catch { /* Native events still work without model discovery. */ }
  }
}
export function endMetadata($: API) {
  reset(state, 'unknown', true); rates = speed()
  ticker?.cancel(); ticker = undefined
  $.ui.invalidate('ui.render')
}
export function beginMetadata($: API, e: TurnStepInput) {
  if (!interactive) return
  begin(state, e)
  if (!e.agentId) stepStarted(rates, performance.now())
  $.ui.invalidate('ui.render')
}
export function completeMetadata($: API, e: TurnStepInput, result: TurnStepResult) {
  if (!interactive) return
  const toast = completed(state, e, result)
  if (toast) $.ui.toast(toast)
  $.ui.invalidate('ui.render')
}
export function finishMetadata($: API, e: TurnStepInput) {
  if (!interactive) return
  finish(state, e)
  if (!e.agentId) stepEnded(rates, performance.now())
  $.ui.invalidate('ui.render')
}
export function streamMetadata(e: TurnStepInput, chunk: TurnStepChunk) {
  if (interactive && !e.agentId) streamed(rates, chunk, performance.now())
}
