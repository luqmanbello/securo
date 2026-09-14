import { describe, expect, it, vi } from 'vitest'
import type { AutoCategorizeResult } from '@/types'
import {
  runAutoCategorize,
  type AutoCategorizeClient,
  type AutoCategorizePoll,
  type AutoCategorizeStart,
} from '@/lib/auto-categorize'

const done = (overrides: Partial<AutoCategorizeResult> = {}): AutoCategorizeResult => ({
  status: 'ok',
  considered: 2,
  categorized: 1,
  skipped_low_confidence: 1,
  detail: '',
  by_category: { Groceries: 1 },
  ...overrides,
})

/** A fake clock that only moves when the runner sleeps. */
function fakeClock() {
  let t = 0
  return {
    now: () => t,
    sleep: vi.fn(async (ms: number) => {
      t += ms
    }),
  }
}

function client(start: AutoCategorizeStart, polls: AutoCategorizePoll[]): AutoCategorizeClient & {
  poll: ReturnType<typeof vi.fn>
} {
  const queue = [...polls]
  return {
    start: vi.fn(async () => start),
    poll: vi.fn(async () => queue.shift() ?? { status: 'running' as const }),
  }
}

describe('runAutoCategorize', () => {
  it('returns an instant answer without polling', async () => {
    const clock = fakeClock()
    const c = client(done({ status: 'no_candidates', categorized: 0 }), [])

    const result = await runAutoCategorize(c, clock)

    expect(result.status).toBe('no_candidates')
    expect(c.poll).not.toHaveBeenCalled()
    expect(clock.sleep).not.toHaveBeenCalled()
  })

  it('polls a queued task until the worker finishes', async () => {
    const clock = fakeClock()
    const c = client({ status: 'queued', task_id: 'abc' }, [
      { status: 'running' },
      { status: 'running' },
      done({ categorized: 3 }),
    ])

    const result = await runAutoCategorize(c, { ...clock, intervalMs: 3000 })

    expect(result.categorized).toBe(3)
    expect(c.poll).toHaveBeenCalledTimes(3)
    expect(c.poll).toHaveBeenCalledWith('abc')
  })

  it('waits between polls rather than hammering the API', async () => {
    const clock = fakeClock()
    const c = client({ status: 'queued', task_id: 'abc' }, [{ status: 'running' }, done()])

    await runAutoCategorize(c, { ...clock, intervalMs: 3000 })

    expect(clock.sleep).toHaveBeenCalledTimes(2)
    expect(clock.sleep).toHaveBeenCalledWith(3000)
  })

  it('reports a slow run as still running, not as an error', async () => {
    // The worker carries on after we stop waiting, so its categories still
    // land. Calling that an error would be untrue.
    const clock = fakeClock()
    const c = client({ status: 'queued', task_id: 'slow' }, [])

    const result = await runAutoCategorize(c, { ...clock, intervalMs: 3000, deadlineMs: 9000 })

    expect(result.status).toBe('still_running')
    expect(result.categorized).toBe(0)
    expect(c.poll).toHaveBeenCalledTimes(3)
  })

  it('passes a worker error straight through for the UI to explain', async () => {
    const clock = fakeClock()
    const c = client({ status: 'queued', task_id: 'x' }, [done({ status: 'error', categorized: 0 })])

    const result = await runAutoCategorize(c, clock)

    expect(result.status).toBe('error')
  })

  it('lets a failed poll request surface as a rejection', async () => {
    const clock = fakeClock()
    const c: AutoCategorizeClient = {
      start: async () => ({ status: 'queued', task_id: 'x' }),
      poll: async () => {
        throw new Error('network down')
      },
    }

    await expect(runAutoCategorize(c, clock)).rejects.toThrow('network down')
  })
})
