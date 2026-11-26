import type { TurnStepChunk } from 'claude-code'
export const WINDOW_MS = 10_000
type Step = { start: number; first?: number; end?: number; estimated: number }
export function speed() {
  return { steps: [] as Step[], tokens: [] as { at: number; n: number; step: Step }[], active: undefined as Step | undefined }
}
export type Speed = ReturnType<typeof speed>
export function stepStarted(s: Speed, now: number) {
  s.active = { start: now, estimated: 0 }
  s.steps.push(s.active)
}
export function streamed(s: Speed, chunk: TurnStepChunk, now: number) {
  const step = s.active
  if (!step || chunk.kind === 'engine') return
  if (chunk.kind === 'stop') {
    const exact = chunk.usage?.output_tokens
    // Visible text is estimated live at four characters a token; the API's count rescales it.
    if (typeof exact === 'number' && step.estimated > 0) {
      const scale = exact / step.estimated
      for (const t of s.tokens) if (t.step === step) t.n *= scale
    }
    stepEnded(s, now)
    return
  }
  step.first ??= now
  const n = (chunk.kind === 'input' ? chunk.json : chunk.kind === 'tool' ? '' : chunk.text).length / 4
  step.estimated += n
  s.tokens.push({ at: now, n, step })
}
export function stepEnded(s: Speed, now: number) {
  if (s.active) s.active.end ??= now
  s.active = undefined
}
export function summary(s: Speed, now: number): string | undefined {
  const from = now - WINDOW_MS
  s.tokens = s.tokens.filter(t => t.at > from)
  s.steps = s.steps.filter(t => t === s.active || (t.end ?? now) > from)
  let busy = 0
  for (const t of s.steps) if (t.first !== undefined) busy += Math.max(0, (t.end ?? now) - Math.max(t.first, from))
  const tokens = s.tokens.reduce((sum, t) => sum + t.n, 0)
  const waits = s.steps.filter(t => t.first !== undefined && t.first > from).map(t => t.first! - t.start)
  const parts = []
  if (busy >= 250 && tokens > 0) parts.push(`${Math.round(tokens / (busy / 1000))} tok/s`)
  if (waits.length) parts.push(`TTFT ${(waits.reduce((a, b) => a + b, 0) / waits.length / 1000).toFixed(1)}s`)
  return parts.length ? `last 10s: ${parts.join(' · ')}` : undefined
}
