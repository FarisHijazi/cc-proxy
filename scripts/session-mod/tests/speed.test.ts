import { expect, test } from 'claude-code/testing'
import { speed, stepStarted, streamed, stepEnded, summary } from '../hooks/speed'
const text = (n: number) => ({ kind: 'text', index: 0, text: 'x'.repeat(n * 4) }) as never
const stop = (output_tokens: number) => ({ kind: 'stop', stopReason: 'end_turn', usage: { output_tokens } }) as never

test('reports output speed and time to first token over the last 10 seconds', () => {
  const s = speed()
  stepStarted(s, 0)
  expect(summary(s, 500)).toBe(undefined)
  streamed(s, { kind: 'engine', ref: 1 }, 400)
  streamed(s, text(10), 2000)
  streamed(s, text(40), 3000)
  expect(summary(s, 3000)).toBe('last 10s: 50 tok/s · TTFT 2.0s')
  streamed(s, stop(100), 4000)
  stepEnded(s, 4000)
  expect(summary(s, 4000)).toBe('last 10s: 50 tok/s · TTFT 2.0s')
  expect(summary(s, 9000)).toBe('last 10s: 50 tok/s · TTFT 2.0s')
})

test('tool time between steps is not counted and old data expires', () => {
  const s = speed()
  stepStarted(s, 0); streamed(s, text(10), 1000); streamed(s, stop(60), 2000)
  stepStarted(s, 5000); streamed(s, text(10), 5500); streamed(s, stop(40), 6000)
  expect(summary(s, 6000)).toBe('last 10s: 67 tok/s · TTFT 0.8s')
  expect(summary(s, 13000)).toBe('last 10s: 80 tok/s · TTFT 0.5s')
  expect(summary(s, 17000)).toBe(undefined)
})

test('a cancelled step stops counting generation time', () => {
  const s = speed()
  stepStarted(s, 0); streamed(s, text(20), 1000); streamed(s, text(20), 2000)
  stepEnded(s, 2000)
  expect(summary(s, 8000)).toBe('last 10s: 40 tok/s · TTFT 1.0s')
})
