import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import {
  captureMainWindowPng,
  listBrowserWindows,
  playFixtureVideoOnCapturedWindow,
  seekCapturedWindowVideo,
  sha256Hex,
  visibleBrowserWindowCount,
  waitForCaptureChange,
} from './core/hidden-capture'
import {
  electronProcessesMatching,
  listElectronProcesses,
} from './core/process-census'
import { descendantPids, visibleWindowsForPids } from './core/visible-window-census'
import { closeDesktopApp, setupMockBackend } from './fixtures'
import { expect, test } from './test'

/**
 * Lane 5 (t_98a31409): screenshot proof with no window on the GM's screen.
 *
 * Probe invocation (hidden, no tap):
 *   HERMES_DESKTOP_E2E_HIDDEN=1
 *   HERMES_HIDDEN_CAPTURE_DIR=<dir>
 *   npx playwright test e2e/hidden-call-capture.spec.ts
 *
 * Captures are webContents.capturePage() PNGs. Two shots must hash-differ
 * while an S1 fixture clip (burned-in frame numbers) plays in the hidden
 * window. Win32 census must stay 0 visible, non-minimized windows for this
 * Electron tree during and after. Do not steal focus; do not show().
 */
const CLIP_CANDIDATES = [
  'C:/Users/judso/Projects/Repos/2.1/Force_2.1/Tools/hermes-force-media/tests/fixtures/avatar-map/call/kitchen-morning/segments/spark-laugh-01.mp4',
  'S:/ForceAgents/architect/wt/avatar-map-s3/Tools/hermes-force-media/tests/fixtures/avatar-map/call/kitchen-morning/segments/spark-laugh-01.mp4',
]

function clipPath(): string {
  const found = CLIP_CANDIDATES.find(candidate => fs.existsSync(candidate))

  if (!found) {
    throw new Error(`missing S1 fixture clip; tried ${CLIP_CANDIDATES.join(', ')}`)
  }

  return found
}

test('hidden Desktop capturePage shows advancing fixture frames with zero visible windows', async () => {
  test.setTimeout(120_000)
  const clip = clipPath()
  const outDir = process.env.HERMES_HIDDEN_CAPTURE_DIR || path.join(os.tmpdir(), 'hermes-hidden-call-capture')
  fs.mkdirSync(outDir, { recursive: true })

  const fixture = await setupMockBackend()
  const electronPid = fixture.app.process().pid
  const userDataDir = fixture.sandbox.userDataDir
  const hermesHome = fixture.sandbox.hermesHome

  try {
    await fixture.page.waitForLoadState('domcontentloaded')
    await fixture.page.waitForTimeout(1_500)

    expect(await visibleBrowserWindowCount(fixture.app), 'BrowserWindow.isVisible must stay false').toBe(0)

    const during = visibleWindowsForPids(descendantPids(electronPid ?? 0))
    expect(during, `Win32 visible windows during run: ${JSON.stringify(during)}`).toEqual([])

    const chromePng = await captureMainWindowPng(fixture.app)
    const chromeSha = sha256Hex(chromePng)
    expect(chromePng.byteLength, 'pre-video capture too small').toBeGreaterThan(1_000)
    console.log('HIDDEN_WINDOWS', JSON.stringify(await listBrowserWindows(fixture.app)))

    const played = await playFixtureVideoOnCapturedWindow(fixture.app, clip)
    expect(played.duration, 'fixture clip duration').toBeGreaterThan(0.5)

    // Early seeks stay on app chrome; later timestamps are the ones that painted.
    const t0 = Math.min(Math.max(played.duration * 0.4, 1.0), played.duration - 0.6)
    const t1 = Math.min(Math.max(played.duration * 0.7, t0 + 0.4), played.duration - 0.05)
    expect(t1, 'need two distinct seek points on the fixture').toBeGreaterThan(t0 + 0.2)

    const videoMinBytes = 100_000
    await seekCapturedWindowVideo(fixture.app, t0)
    const first = await waitForCaptureChange(fixture.app, fixture.page, chromeSha, 15_000, videoMinBytes)
    await seekCapturedWindowVideo(fixture.app, t1)
    const second = await waitForCaptureChange(fixture.app, fixture.page, first.sha, 15_000, videoMinBytes)
    const png1 = first.png
    const png2 = second.png
    const sha1 = first.sha
    const sha2 = second.sha

    expect(png1.byteLength, 'capture-1 too small').toBeGreaterThan(1_000)
    expect(png2.byteLength, 'capture-2 too small').toBeGreaterThan(1_000)
    expect(t1, 'fixture currentTime must advance').toBeGreaterThan(t0)
    expect(sha1, 'captures must differ as burned-in frames advance').not.toBe(sha2)
    expect(sha1, 'frame A must leave the pre-video chrome').not.toBe(chromeSha)

    const aPath = path.join(outDir, 'hidden-call-frame-a.png')
    const bPath = path.join(outDir, 'hidden-call-frame-b.png')
    fs.writeFileSync(aPath, png1)
    fs.writeFileSync(bPath, png2)
    fs.writeFileSync(
      path.join(outDir, 'hidden-call-capture.json'),
      JSON.stringify(
        {
          clip,
          t1: t0,
          t2: t1,
          sha1,
          sha2,
          png1Bytes: png1.byteLength,
          png2Bytes: png2.byteLength,
          visibleWindows: 0,
          electronPid,
          aPath,
          bPath,
        },
        null,
        2,
      ),
    )
    console.log(
      `HIDDEN_CALL_CAPTURE t1=${t0.toFixed(3)} t2=${t1.toFixed(3)} shaA=${sha1.slice(0, 12)} shaB=${sha2.slice(0, 12)} visible=0`,
    )

    expect(await visibleBrowserWindowCount(fixture.app)).toBe(0)
    const stillDuring = visibleWindowsForPids(descendantPids(electronPid ?? 0))
    expect(stillDuring, `Win32 visible windows after capture: ${JSON.stringify(stillDuring)}`).toEqual([])
  } finally {
    await closeDesktopApp(fixture.app, { hermesHome, userDataDir })
    await fixture.mock.close().catch(() => undefined)
    fixture.sandbox.cleanup()
  }

  expect(electronPid, 'missing electron pid').toBeTruthy()
  expect(
    listElectronProcesses().some(proc => proc.pid === electronPid),
    `electron pid ${electronPid} still alive after closeDesktopApp`,
  ).toBe(false)

  const stillOurs = [
    ...electronProcessesMatching(userDataDir),
    ...electronProcessesMatching(hermesHome),
  ].filter((proc, index, all) => all.findIndex(other => other.pid === proc.pid) === index)

  expect(
    stillOurs,
    `this launch's electron leftovers after close: ${stillOurs.map(p => `${p.pid}:${p.cmdline}`).join(' | ')}`,
  ).toEqual([])
})
