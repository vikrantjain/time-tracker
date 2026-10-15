// Tests for hooks/register.tsx (run with `claude plugin test .`). The test's
// hooks sit beneath the module and stand in for report.py, tt-dispatch.sh
// and the session.

import { describe, expect, mock, test } from 'claude-code/testing'
import type { On, ProcessRunResult, RenderElement } from 'claude-code'

import type { TrackerStatus } from '../types'

const NOW_MS = 1_700_000_000_000

const RUNNING: TrackerStatus = { line: '⏱ 3h 12m today', paused: null }

function paused(untilTs: number | null): TrackerStatus {
  return {
    line: '⏸ paused until 13:05 · 2h 41m today',
    paused: { since: '12:20', until: untilTs ? '13:05' : null, untilTs, reason: 'lunch' },
  }
}

const BAND = {
  plugin: 'time-tracker',
  component: 'AbovePrompt',
  props: {
    hasSurvey: false,
    isWorking: false,
    maxRows: 10,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 10 },
    view: {},
  },
} as const

function ran(stdout: string, exitCode = 0): ProcessRunResult {
  return { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
}

// What report.py answers, and a record of what the module did.
function world(on: On, initial: TrackerStatus) {
  const w = {
    status: initial as TrackerStatus | 'broken',
    runs: [] as { argv: readonly string[]; env: Record<string, string> | undefined }[],
    lines: [] as (string | undefined)[],
    toasts: [] as string[],
  }
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.cwd', () => ({ value: '/proj/demo' }))
  on('prompt.submit', () => ({ drop: 'handled by tt' }))
  // The engine's own band: an empty Box the module's tree replaces.
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Box } = $.ui.resolve(e)
    return h(Box, { key: 'engine-band' }) as RenderElement
  })
  on('process.run', ($, e) => {
    w.runs.push({ argv: e.argv, env: e.init?.env })
    if (e.argv.at(-1) === 'resume') {
      w.status = RUNNING
      return { value: ran(JSON.stringify({ decision: 'block', reason: '▶ Tracking resumed.' })) }
    }
    if (w.status === 'broken') return { value: ran('', 1) }
    return { value: ran(JSON.stringify(w.status)) }
  })
  on('ui.status', ($, e) => {
    w.lines.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', ($, e) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  return w
}

const INTERACTIVE = { cwd: '/proj/demo', surface: 'terminal', isInteractive: true } as const

describe('status entry', () => {
  test('shows the brief line from report.py --status --json', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const w = world(on, RUNNING)
    await $.session.start(INTERACTIVE)
    await clock.settle()

    expect(w.lines.at(-1)).toBe('⏱ 3h 12m today')
    const argv = w.runs[0]?.argv ?? []
    expect(argv.slice(1)).toEqual([
      expect.stringMatching(/\/scripts\/report\.py$/),
      '--status', '--json', '--session', 'sess-1', '--project', '/proj/demo',
    ])
  })

  test('says it is unavailable when report.py fails', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const w = world(on, RUNNING)
    w.status = 'broken'
    await $.session.start(INTERACTIVE)
    await clock.settle()

    expect(w.lines.at(-1)).toBe('⏱ time-tracker: status unavailable')
  })

  test('refreshes every minute', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const w = world(on, RUNNING)
    await $.session.start(INTERACTIVE)
    await clock.settle()
    w.status = { line: '⏱ 3h 13m today', paused: null }
    await clock.advance(60_000)

    expect(w.lines.at(-1)).toBe('⏱ 3h 13m today')
  })

  test('a headless run spawns nothing', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const w = world(on, RUNNING)
    await $.session.start({ cwd: '/proj/demo', surface: null, isInteractive: false })
    await clock.advance(120_000)

    expect(w.runs).toEqual([])
  })
})

describe('paused band', () => {
  test('no band while tracking', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    world(on, RUNNING)
    await $.session.start(INTERACTIVE)
    await clock.settle()

    const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
    expect(await ui.find({ key: 'resume' })).toBeUndefined()
    await ui.unmount()
  })

  test('a tt pause typed at the prompt brings the band up', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const w = world(on, RUNNING)
    await $.session.start(INTERACTIVE)
    await clock.settle()

    w.status = paused(null)
    await $.prompt.submit({ text: 'tt pause lunch', wait: false, origin: { kind: 'composer' } })
    await clock.settle()

    const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
    expect(await ui.find({ type: 'Text', text: 'paused since 12:20 · lunch' })).toBeDefined()
    expect(await ui.find({ key: 'resume' })).toBeDefined()
    await ui.unmount()
  })

  for (const surface of ['terminal', 'desktop'] as const) {
    test(`Resume runs tt-dispatch.sh resume for this session (${surface})`, async ($, on) => {
      const clock = mock.clock(on, { now: NOW_MS })
      const w = world(on, paused(null))
      await $.session.start(INTERACTIVE)
      await clock.settle()

      const ui = await $.ui.mount({ ...BAND, surface })
      await ui.press({ key: 'resume' })
      await clock.settle()

      const resume = w.runs.find(r => r.argv.at(-1) === 'resume')
      expect(resume?.argv).toEqual([
        'bash', expect.stringMatching(/\/scripts\/tt-dispatch\.sh$/), 'resume',
      ])
      expect(resume?.env).toMatchObject({ TT_SESSION_ID: 'sess-1', TT_PROJECT: '/proj/demo' })
      expect(w.toasts).toContain('▶ Tracking resumed.')
      expect(w.lines.at(-1)).toBe('⏱ 3h 12m today')
      expect(await ui.find({ key: 'resume' })).toBeUndefined()
      await ui.unmount()
    })
  }

  test('a timed pause drops the band when it runs out', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const w = world(on, paused(NOW_MS / 1000 + 30))
    await $.session.start(INTERACTIVE)
    await clock.settle()

    const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
    expect(await ui.find({ type: 'Text', text: 'auto-resumes 13:05' })).toBeDefined()

    // report.py now reads the pause as expired; the expiry timer (30s + 1s)
    // fires before the minute ticker would.
    w.status = RUNNING
    await clock.advance(31_500)
    expect(await ui.find({ key: 'resume' })).toBeUndefined()
    await ui.unmount()
  })
})
