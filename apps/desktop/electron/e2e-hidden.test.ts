import { afterEach, describe, expect, test, vi } from 'vitest'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('installE2eCrashExit', () => {
  test('no-ops when hidden mode is off', async () => {
    const { installE2eCrashExit } = await import('./e2e-hidden')
    const on = vi.fn()
    const exit = vi.fn()
    expect(installE2eCrashExit({ HERMES_DESKTOP_E2E_HIDDEN: '0' }, { on, exit })).toBe(false)
    expect(on).not.toHaveBeenCalled()
    expect(exit).not.toHaveBeenCalled()
  })

  test('logs to stderr and exits 1 on uncaughtException', async () => {
    const { installE2eCrashExit } = await import('./e2e-hidden')
    const handlers = new Map<string, (value: unknown) => void>()

    const on = vi.fn((event: string, listener: (value: unknown) => void) => {
      handlers.set(event, listener)
    })

    const exit = vi.fn()
    const error = vi.fn()

    expect(
      installE2eCrashExit(
        { HERMES_DESKTOP_E2E_HIDDEN: '1' },
        { on, exit },
        { error },
      ),
    ).toBe(true)

    expect(handlers.has('uncaughtException')).toBe(true)
    expect(handlers.has('unhandledRejection')).toBe(true)

    handlers.get('uncaughtException')!(new Error('boom-userData'))
    expect(error).toHaveBeenCalledOnce()
    expect(String(error.mock.calls[0]?.[0] ?? '')).toContain('uncaughtException')
    expect(String(error.mock.calls[0]?.[0] ?? '')).toContain('boom-userData')
    expect(exit).toHaveBeenCalledWith(1)
  })

  test('logs and exits 1 on unhandledRejection', async () => {
    const { installE2eCrashExit } = await import('./e2e-hidden')
    const handlers = new Map<string, (value: unknown) => void>()

    const on = vi.fn((event: string, listener: (value: unknown) => void) => {
      handlers.set(event, listener)
    })

    const exit = vi.fn()
    const error = vi.fn()

    installE2eCrashExit(
      { HERMES_DESKTOP_E2E_HIDDEN: '1' },
      { on, exit },
      { error },
    )

    handlers.get('unhandledRejection')!('reject-reason')
    expect(String(error.mock.calls[0]?.[0] ?? '')).toContain('unhandledRejection')
    expect(String(error.mock.calls[0]?.[0] ?? '')).toContain('reject-reason')
    expect(exit).toHaveBeenCalledWith(1)
  })
})

describe('isDesktopE2eHidden', () => {
  test('defaults off when unset (production path)', async () => {
    const { isDesktopE2eHidden } = await import('./e2e-hidden')
    expect(isDesktopE2eHidden({})).toBe(false)
    expect(isDesktopE2eHidden({ HERMES_DESKTOP_E2E_HIDDEN: '' })).toBe(false)
  })

  test('accepts truthy agent values', async () => {
    const { isDesktopE2eHidden } = await import('./e2e-hidden')

    for (const value of ['1', 'true', 'TRUE', 'on', 'yes']) {
      expect(isDesktopE2eHidden({ HERMES_DESKTOP_E2E_HIDDEN: value }), value).toBe(true)
    }
  })

  test('explicit off wins so a human can watch', async () => {
    const { isDesktopE2eHidden } = await import('./e2e-hidden')

    for (const value of ['0', 'false', 'off', 'no', 'OFF']) {
      expect(isDesktopE2eHidden({ HERMES_DESKTOP_E2E_HIDDEN: value }), value).toBe(false)
    }
  })
})

describe('applyE2eHiddenWindowOptions', () => {
  test('is a no-op when hidden mode is off', async () => {
    const { applyE2eHiddenWindowOptions } = await import('./e2e-hidden')
    const options = { show: true, width: 800, webPreferences: { sandbox: true } }
    expect(applyE2eHiddenWindowOptions(options, {})).toEqual(options)
  })

  test('forces show:false, paintWhenInitiallyHidden, and unthrottled prefs', async () => {
    const { applyE2eHiddenWindowOptions } = await import('./e2e-hidden')

    const options = {
      show: true,
      width: 800,
      webPreferences: { sandbox: true, preload: '/x' }
    }

    const next = applyE2eHiddenWindowOptions(options, { HERMES_DESKTOP_E2E_HIDDEN: '1' })

    expect(next.show).toBe(false)
    expect(next.paintWhenInitiallyHidden).toBe(true)
    expect(next.width).toBe(800)
    expect(next.webPreferences).toEqual({
      sandbox: true,
      preload: '/x',
      backgroundThrottling: false
    })
  })
})

describe('installE2eHiddenWindowGuard', () => {
  test('no-ops when flag is off', async () => {
    const { installE2eHiddenWindowGuard } = await import('./e2e-hidden')
    let attached = false
    installE2eHiddenWindowGuard(
      {
        on: () => {
          attached = true
        }
      },
      { HERMES_DESKTOP_E2E_HIDDEN: '0' }
    )
    expect(attached).toBe(false)
  })

  test('blocks show and showInactive under the flag', async () => {
    const { installE2eHiddenWindowGuard } = await import('./e2e-hidden')

    let listener: ((event: unknown, win: {
      show: (...args: unknown[]) => unknown
      showInactive: (...args: unknown[]) => unknown
      hide: () => unknown
      isDestroyed?: () => boolean
      isVisible?: () => boolean
      once?: (event: string, listener: () => void) => unknown
      webContents?: { setBackgroundThrottling?: (allowed: boolean) => void }
    }) => void) | null = null

    const shown: string[] = []

    installE2eHiddenWindowGuard(
      {
        on: (_event, fn) => {
          listener = fn
        }
      },
      { HERMES_DESKTOP_E2E_HIDDEN: '1' }
    )

    expect(listener).toBeTruthy()

    let throttling: boolean | null = null

    const win = {
      show: () => shown.push('show'),
      showInactive: () => shown.push('showInactive'),
      hide: () => shown.push('hide'),
      isDestroyed: () => false,
      isVisible: () => true,
      once: (event: string, fn: () => void) => {
        if (event === 'ready-to-show') {
          fn()
        }
      },
      webContents: {
        setBackgroundThrottling: (value: boolean) => {
          throttling = value
        }
      }
    }

    listener!(null, win)
    win.show()
    win.showInactive()

    expect(shown).toEqual(['hide'])
    expect(throttling).toBe(false)
  })
})
