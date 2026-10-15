// State the hooks module keeps for the session: the last answer of
// `report.py --status --json`. report.py formats the clock times.

export type TrackerPause = {
  since: string
  until: string | null
  untilTs: number | null
  reason: string | null
}

export type TrackerStatus = {
  line: string
  paused: TrackerPause | null
}

declare module 'claude-code' {
  interface PluginState {
    'time-tracker': { status: TrackerStatus | null }
  }
}
