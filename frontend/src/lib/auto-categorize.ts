import type { AutoCategorizeResult } from '@/types'

/** What the start endpoint returns: a finished answer, or a task to poll. */
export type AutoCategorizeStart =
  | AutoCategorizeResult
  | { status: 'queued'; task_id: string }

/** What the status endpoint returns while the worker is still going. */
export type AutoCategorizePoll = AutoCategorizeResult | { status: 'running' }

export interface AutoCategorizeClient {
  start: () => Promise<AutoCategorizeStart>
  poll: (taskId: string) => Promise<AutoCategorizePoll>
}

export interface RunOptions {
  intervalMs?: number
  /** Stop waiting after this long. The worker's own hard limit is 210s. */
  deadlineMs?: number
  sleep?: (ms: number) => Promise<void>
  now?: () => number
}

const EMPTY_RESULT = {
  considered: 0,
  categorized: 0,
  skipped_low_confidence: 0,
  detail: '',
  by_category: {},
}

/**
 * Start an auto-categorization and wait for its answer.
 *
 * The request no longer holds a connection open for the model — that was
 * cut at nginx's 60s default while real model calls took up to 104s. It
 * now returns at once, either with a final answer (nothing to do, agents
 * off, no connection) or with a task id, which this polls until the worker
 * finishes.
 *
 * Running out of patience is not a failure: the worker keeps going and its
 * categories still land. That comes back as `still_running`, so the UI can
 * say "check back shortly" rather than claim an error.
 */
export async function runAutoCategorize(
  client: AutoCategorizeClient,
  {
    intervalMs = 3000,
    deadlineMs = 240_000,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    now = () => Date.now(),
  }: RunOptions = {},
): Promise<AutoCategorizeResult> {
  const started = await client.start()
  if (started.status !== 'queued') return started

  const taskId = started.task_id
  const giveUpAt = now() + deadlineMs
  while (now() < giveUpAt) {
    await sleep(intervalMs)
    const polled = await client.poll(taskId)
    if (polled.status !== 'running') return polled
  }
  return { status: 'still_running', ...EMPTY_RESULT }
}
