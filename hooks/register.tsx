// Time Tracker — hooks module: today's time as a status entry, and a band
// above the prompt while this session is paused.
//
// A read-only view over the store. State comes from `report.py --status
// --json`, so the rules for what opens and closes a pause stay in report.py.
// The one write, the band's Resume button, runs tt-dispatch.sh exactly as a
// typed `tt resume` does. Capture stays in the command hooks beside this
// module: a module can only replace a file whole, never append to it.

import { atom, read, update } from 'claude-code'
import type { Register, Timer } from 'claude-code'

import type { TrackerStatus } from '../types'

const status = atom({ plugin: 'time-tracker', key: 'status' } as const, null)

// Today's total moves in minutes, so a minute between refreshes is enough.
const REFRESH_MS = 60_000

function parseStatus(exitCode: number, stdout: string): TrackerStatus | null {
  if (exitCode !== 0) return null
  try {
    return JSON.parse(stdout) as TrackerStatus
  } catch {
    return null
  }
}

export const register: Register = on => {
  // Set by session.start in an interactive session: schedules a refresh
  // without making the caller wait on python.
  let refreshSoon: (() => void) | undefined
  let ticker: Timer | undefined
  let expiry: Timer | undefined

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    // Headless runs draw nothing, so they spawn no refreshes.
    if (!e.isInteractive) return started

    const refresh = async (): Promise<void> => {
      const [sid, cwd] = await Promise.all([$.session.id(), $.session.cwd()])
      let current: TrackerStatus | null = null
      try {
        const ran = await $.process.run([
          'python3', `${$.plugin.root}/scripts/report.py`,
          '--status', '--json', '--session', sid, '--project', cwd,
        ])
        current = parseStatus(ran.exitCode, ran.stdout)
      } catch {
        // python3 missing, or the run timed out: shown as unavailable below.
      }
      // Say so rather than clear the line: a missing python3 breaks reports too.
      $.ui.status(current ? current.line : '⏱ time-tracker: status unavailable')
      await update($, status, () => current)

      // Drop the band when a timed pause runs out, not up to a minute later.
      expiry?.cancel()
      expiry = undefined
      const untilTs = current?.paused?.untilTs
      if (untilTs) {
        const wait = untilTs * 1000 - (await $.clock.now()) + 1000
        expiry = $.clock.after(Math.max(wait, 0), () => refreshSoon?.())
      }
    }

    refreshSoon = () => {
      $.clock.after(0, () => {
        refresh().catch(() => {})
      })
    }
    ticker?.cancel()
    ticker = $.clock.every(REFRESH_MS, () => refreshSoon?.())
    refreshSoon()
    return started
  })

  // Any prompt can change the state: `tt pause`/`tt resume` (dropped by the
  // command hook beneath) or a real prompt, which ends a pause.
  on('prompt.submit', async ($, e, next) => {
    const submitted = await next(e)
    refreshSoon?.()
    return submitted
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const pause = (await read($, status))?.paused
    if (!pause || e.props.hasSurvey) return next(e)

    const onResume = async (): Promise<void> => {
      const [sid, cwd] = await Promise.all([$.session.id(), $.session.cwd()])
      const ran = await $.process.run(
        ['bash', `${$.plugin.root}/scripts/tt-dispatch.sh`, 'resume'],
        {
          env: {
            CLAUDE_PLUGIN_ROOT: $.plugin.root,
            TT_SESSION_ID: sid,
            TT_PROJECT: cwd,
            TT_SOURCE: '',
            TT_REASON: '',
          },
        },
      )
      // The dispatcher answers with a hook block; its reason is the message.
      let said: string | undefined
      try {
        said = (JSON.parse(ran.stdout) as { reason?: string }).reason
      } catch {
        said = undefined
      }
      $.ui.toast(said ?? 'time-tracker: resume failed. Type tt resume instead.')
      refreshSoon?.()
    }

    const { Box, Button, Text } = $.ui.resolve(e)
    const until = pause.until ? ` · auto-resumes ${pause.until}` : ''
    const why = pause.reason ? ` · ${pause.reason}` : ''

    return (
      <Box>
        <Text color="warning">
          ⏸ Time tracking paused since {pause.since}
          {until}
          {why}{' '}
        </Text>
        <Button key="resume" label="Resume" onPress={onResume} />
      </Box>
    )
  })
}
