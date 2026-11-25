import type { Hook, On, TurnStepInput } from 'claude-code'
import { tracker, reset, begin, finish, isMainRequest, sent, answered, display } from './identity'
type API = Parameters<Hook<'session.start'>>[0]
const state = tracker()
let interactive = false

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
    const { Box, Text } = $.ui.resolve(e)
    return <Box><Text dimColor>{display(state)}</Text></Box>
  })
}
export async function startMetadata($: API, isInteractive: boolean) {
  interactive = isInteractive
  reset(state, 'unknown', true)
  if (interactive) {
    try { state.requested = await $.session.model() } catch { /* Native events still work without model discovery. */ }
  }
}
export function endMetadata($: API) { reset(state, 'unknown', true); $.ui.invalidate('ui.render') }
export function beginMetadata($: API, e: TurnStepInput) {
  if (interactive) { begin(state, e); $.ui.invalidate('ui.render') }
}
export function finishMetadata($: API, e: TurnStepInput) {
  if (interactive) { finish(state, e); $.ui.invalidate('ui.render') }
}
