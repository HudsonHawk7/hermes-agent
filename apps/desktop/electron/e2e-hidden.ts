/**
 * Desktop e2e hidden-window mode (HERMES_DESKTOP_E2E_HIDDEN).
 *
 * Agent Playwright runs must not flash CONNECTING windows on the operator's
 * screen or leave leaked electron.exe trees behind. When the flag is on
 * (fixtures default it to 1; set 0 only when a human wants to watch):
 *  - every BrowserWindow is born show:false + paintWhenInitiallyHidden
 *  - background throttling stays off so screenshots/clicks still work
 *  - show()/showInactive() are no-ops so ready-to-show cannot raise a popup
 *  - uncaughtException / unhandledRejection log to stderr and exit 1 so
 *    Electron never raises its native error dialog (hidden windows can't hide it)
 *
 * Import this module FIRST in main.ts so the crash handlers are registered
 * before link-metadata (or any other module) evaluates.
 */

export type E2eHiddenEnv = Record<string, string | undefined>

/** True when fixtures/agents requested hidden e2e windows. Explicit 0/false/off/no wins. */
export function isDesktopE2eHidden(env: E2eHiddenEnv = process.env): boolean {
  const raw = (env.HERMES_DESKTOP_E2E_HIDDEN ?? '').trim().toLowerCase()

  if (raw === '0' || raw === 'false' || raw === 'off' || raw === 'no') {
    return false
  }

  return raw === '1' || raw === 'true' || raw === 'on' || raw === 'yes'
}

type CrashExitTarget = {
  on: (event: 'uncaughtException' | 'unhandledRejection', listener: (value: unknown) => void) => unknown
  exit: (code?: number) => never | void
}

type CrashExitIo = {
  error: (...args: unknown[]) => void
}

function formatCrashReason(reason: unknown): string {
  if (reason instanceof Error) {
    return reason.stack || reason.message || reason.name || 'Error'
  }

  if (typeof reason === 'string') {
    return reason
  }

  try {
    return JSON.stringify(reason) ?? String(reason)
  } catch {
    return String(reason)
  }
}

/**
 * Under HERMES_DESKTOP_E2E_HIDDEN, never let Electron's default dialog fire.
 * Log the fault to stderr and exit 1 so Playwright sees a failed process instead
 * of a modal stuck on the operator's desk.
 */
export function installE2eCrashExit(
  env: E2eHiddenEnv = process.env,
  target: CrashExitTarget = process,
  io: CrashExitIo = console,
): boolean {
  if (!isDesktopE2eHidden(env)) {
    return false
  }

  const exitOn = (label: string) => (reason: unknown) => {
    try {
      io.error(`[hermes-desktop-e2e] ${label}: ${formatCrashReason(reason)}`)
    } catch {
      // stderr may already be gone during teardown
    }

    try {
      target.exit(1)
    } catch {
      // process.exit can throw under some test doubles
    }
  }

  target.on('uncaughtException', exitOn('uncaughtException'))
  target.on('unhandledRejection', exitOn('unhandledRejection'))

  return true
}

export type BrowserWindowLikeOptions = {
  show?: boolean
  paintWhenInitiallyHidden?: boolean
  webPreferences?: Record<string, unknown>
  [key: string]: unknown
}

/** Merge constructor options so a hidden e2e window still paints for Playwright. */
export function applyE2eHiddenWindowOptions<T extends BrowserWindowLikeOptions>(
  options: T,
  env: E2eHiddenEnv = process.env
): T {
  if (!isDesktopE2eHidden(env)) {
    return options
  }

  return {
    ...options,
    show: false,
    paintWhenInitiallyHidden: true,
    webPreferences: {
      ...(options.webPreferences ?? {}),
      backgroundThrottling: false
    }
  }
}

type AppLike = {
  on: (event: 'browser-window-created', listener: (event: unknown, win: E2eHiddenWindow) => void) => unknown
}

export type E2eHiddenWindow = {
  show: (...args: unknown[]) => unknown
  showInactive: (...args: unknown[]) => unknown
  hide: () => unknown
  isDestroyed?: () => boolean
  isVisible?: () => boolean
  once?: (event: string, listener: () => void) => unknown
  webContents?: { setBackgroundThrottling?: (allowed: boolean) => void }
}

/**
 * After construction, freeze the window hidden and keep the renderer unthrottled.
 * paintWhenInitiallyHidden must still be set at construction (see applyE2eHiddenWindowOptions).
 */
export function installE2eHiddenWindowGuard(appApi: AppLike, env: E2eHiddenEnv = process.env): void {
  if (!isDesktopE2eHidden(env)) {
    return
  }

  appApi.on('browser-window-created', (_event, win) => {
    const keepHidden = () => {
      if (win.isDestroyed?.()) {
        return
      }

      // Intentionally empty: ready-to-show / focus paths must not raise a popup.
    }

    win.show = keepHidden
    win.showInactive = keepHidden

    try {
      win.webContents?.setBackgroundThrottling?.(false)
    } catch {
      // webContents may not be ready yet on some platforms
    }

    win.once?.('ready-to-show', () => {
      try {
        if (!win.isDestroyed?.() && win.isVisible?.()) {
          win.hide()
        }
      } catch {
        // best-effort
      }
    })
  })
}
