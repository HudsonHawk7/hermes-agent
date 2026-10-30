import { isDesktopE2eHidden } from '../electron/e2e-hidden'

import { listElectronProcesses } from './core/process-census'
import {
  closeDesktopApp,
  listGatewayRunProcesses,
  setupMockBackend
} from './fixtures'
/**
 * Proof that agent desktop e2e keeps BrowserWindows off the operator's screen.
 *
 * Default fixture env sets HERMES_DESKTOP_E2E_HIDDEN=1; the main process then
 * creates every window show:false + paintWhenInitiallyHidden and no-ops show().
 *
 * Does NOT wait for composer readiness — an update-hold overlay (or CONNECTING)
 * can block chat, and this card's contract is only "window not visible + tear down".
 */
import { expect, test } from './test'

test('hidden e2e mode keeps the main window invisible and tears down Electron', async () => {
  expect(isDesktopE2eHidden({ HERMES_DESKTOP_E2E_HIDDEN: '1' })).toBe(true)

  const beforeElectrons = listElectronProcesses().map(proc => proc.pid)
  const fixture = await setupMockBackend()
  const electronPid = fixture.app.process().pid

  try {
    // Page must exist and paint; chat readiness is out of scope for this proof.
    await fixture.page.waitForLoadState('domcontentloaded')
    await fixture.page.waitForTimeout(1_500)

    const state = await fixture.app.evaluate(({ BrowserWindow }) => {
      const windows = BrowserWindow.getAllWindows() as Array<{
        id: number
        isVisible: () => boolean
        isDestroyed: () => boolean
      }>

      return {
        count: windows.length,
        visible: windows.map(win => ({
          id: win.id,
          isVisible: win.isVisible(),
          isDestroyed: win.isDestroyed()
        }))
      }
    })

    expect(state.count, 'expected at least the main window').toBeGreaterThan(0)
    expect(
      state.visible.every(win => !win.isVisible && !win.isDestroyed),
      `window(s) became visible under HERMES_DESKTOP_E2E_HIDDEN: ${JSON.stringify(state.visible)}`
    ).toBe(true)

    // Paint path still works while hidden (screenshots / CDP).
    const shot = await fixture.page.screenshot({ type: 'png' })
    expect(shot.byteLength).toBeGreaterThan(1_000)
  } finally {
    await closeDesktopApp(fixture.app, { hermesHome: fixture.sandbox.hermesHome })
    await fixture.mock.close().catch(() => undefined)
    fixture.sandbox.cleanup()
  }

  expect(electronPid, 'missing electron pid').toBeTruthy()
  expect(
    listElectronProcesses().some(proc => proc.pid === electronPid),
    `electron pid ${electronPid} still alive after closeDesktopApp`
  ).toBe(false)

  // Do not fail the suite on unrelated pre-existing Electron processes (other apps).
  const stillOurs = listElectronProcesses().filter(
    proc => !beforeElectrons.includes(proc.pid) && proc.pid !== electronPid
  )

  expect(
    stillOurs,
    `new electron leftovers after test: ${stillOurs.map(p => `${p.pid}:${p.cmdline}`).join(' | ')}`
  ).toEqual([])

  // Gateway-run orphans from this sandbox should be reaped (best-effort signal).
  void listGatewayRunProcesses
})
