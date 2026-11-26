import type { TurnStepChunk } from 'claude-code'
export const WINDOW_MS = 10_000
type Step = { start: number; first?: number; end?: number; exact?: number }
export function speed() {
  return { steps: [] as Step[], tokens: [] as { at: number; n: number; step: Step }[], active: undefined as Step | undefined }
}
export type Speed = ReturnType<typeof speed>
export function stepStarted(s: Speed, now: number) {
  s.active = { start: now }
  s.steps.push(s.active)
}
export function streamed(s: Speed, chunk: TurnStepChunk, now: number) {
  const step = s.active
  if (!step || chunk.kind === 'engine') return
  step.first ??= now
  if (chunk.kind === 'stop') {
    // Hidden thinking streams no text, so a finished step spreads the API's count over its whole generation.
    if (typeof chunk.usage?.output_tokens === 'number') step.exact = chunk.usage.output_tokens
    stepEnded(s, now)
    return
  }
  const text = chunk.kind === 'input' ? chunk.json : chunk.kind === 'tool' ? '' : chunk.text
  if (text) s.tokens.push({ at: now, n: text.length / 4, step })
}
export function stepEnded(s: Speed, now: number) {
  if (s.active) s.active.end ??= now
  s.active = undefined
}
export function summary(s: Speed, now: number): string | undefined {
  const from = now - WINDOW_MS
  s.tokens = s.tokens.filter(t => t.at > from)
  s.steps = s.steps.filter(t => t === s.active || (t.end ?? now) > from)
  let busy = 0, tokens = 0
  for (const t of s.steps) {
    if (t.first === undefined) continue
    const end = t.end ?? now, overlap = Math.max(0, end - Math.max(t.first, from))
    busy += overlap
    tokens += t.exact !== undefined && end > t.first ? t.exact * overlap / (end - t.first) :
      s.tokens.reduce((sum, x) => sum + (x.step === t ? x.n : 0), 0)
  }
  const waits = s.steps.filter(t => t.first !== undefined && t.first > from).map(t => t.first! - t.start)
  const parts = []
  if (busy >= 250 && tokens > 0) parts.push(`${Math.round(tokens / (busy / 1000))} tok/s`)
  if (waits.length) parts.push(`TTFT ${(waits.reduce((a, b) => a + b, 0) / waits.length / 1000).toFixed(1)}s`)
  return parts.length ? `last 10s: ${parts.join(' · ')}` : undefined
}
