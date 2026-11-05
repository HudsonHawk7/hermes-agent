/**
 * Group chat follow-latest + Jump-to-latest (t_53595138).
 *
 * Mini Electron window (hidden) drives the pure follow helpers against a real
 * Chromium ResizeObserver + scroll geometry — streaming text growth and a
 * late-loading image both change content height without flipping log.length.
 * Scrolled-up stays put and surfaces the pill; stuck-to-bottom follows.
 *
 * Run hidden and tear down the process tree (see t_ecc82493).
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

import { build } from 'esbuild'

import { resolveElectronBinary } from './electron-binary'
import { _electron, expect, test } from './test'

test('group chat follow stays on latest for height growth; scrolled-up shows pill and no jump', async () => {
  const temporary = mkdtempSync(path.join(tmpdir(), 'hermes-group-chat-follow-'))
  const userData = path.join(temporary, 'user-data')
  mkdirSync(userData, { recursive: true })
  const main = path.join(temporary, 'main.cjs')
  writeFileSync(
    main,
    `const { app, BrowserWindow } = require('electron');
app.setPath('userData', ${JSON.stringify(userData)});
// Strip ELECTRON_RUN_AS_NODE so the binary actually boots Electron (t_ecc82493).
delete process.env.ELECTRON_RUN_AS_NODE;
app.whenReady().then(() => {
  const window = new BrowserWindow({
    width: 640,
    height: 720,
    show: false,
    paintWhenInitiallyHidden: true,
    webPreferences: { sandbox: true, contextIsolation: true, backgroundThrottling: false }
  });
  window.loadURL('about:blank');
});`
  )

  const here = path.dirname(fileURLToPath(import.meta.url))
  const desktopRoot = path.resolve(here, '..')
  const repoRoot = path.resolve(desktopRoot, '../..')
  const helperEntry = path.resolve(desktopRoot, 'src/plugins/hermes-bots/group-chat-follow.ts')

  const bundle = await build({
    entryPoints: [helperEntry],
    bundle: true,
    format: 'iife',
    globalName: 'GroupChatFollow',
    write: false
  })

  const electronPath = resolveElectronBinary([desktopRoot, repoRoot])
  const app = await _electron.launch({
    executablePath: electronPath,
    args: ['--no-sandbox', main],
    env: {
      ...process.env,
      ELECTRON_RUN_AS_NODE: undefined as unknown as string,
      HERMES_DESKTOP_E2E_HIDDEN: '1'
    }
  })

  try {
    const page = await app.firstWindow()
    await page.setContent(`<!doctype html>
<html><body style="margin:0">
  <div id="shell" style="position:relative;height:400px;width:360px">
    <div id="viewport" style="height:400px;overflow:auto;border:1px solid #333">
      <div id="content"></div>
    </div>
    <button id="pill" type="button" style="display:none;position:absolute;bottom:8px;left:50%;transform:translateX(-50%)">Jump to latest</button>
  </div>
</body></html>`)
    await page.addScriptTag({ content: bundle.outputFiles[0].text })

    const result = await page.evaluate(async () => {
      const api = (
        window as unknown as {
          GroupChatFollow: {
            isGroupChatNearBottom: (v: HTMLElement) => boolean
            observeGroupChatLogResize: (content: Element, viewport: HTMLElement, getStick: () => boolean) => () => void
            scrollGroupChatToBottom: (v: HTMLElement, behavior?: ScrollBehavior) => void
          }
        }
      ).GroupChatFollow

      const viewport = document.getElementById('viewport') as HTMLElement
      const content = document.getElementById('content') as HTMLElement
      const pill = document.getElementById('pill') as HTMLButtonElement

      let stick = true
      const syncPill = () => {
        stick = api.isGroupChatNearBottom(viewport)
        pill.style.display = stick ? 'none' : 'block'
      }

      viewport.addEventListener('scroll', syncPill, { passive: true })
      api.observeGroupChatLogResize(content, viewport, () => stick)
      pill.onclick = () => {
        stick = true
        api.scrollGroupChatToBottom(viewport, 'auto')
        syncPill()
      }

      const addBlock = (height: number, id: string) => {
        const block = document.createElement('div')
        block.id = id
        block.style.cssText = `height:${height}px;border-bottom:1px solid #666;box-sizing:border-box`
        block.textContent = id
        content.append(block)
      }

      // Seed tall history so the viewport can scroll.
      for (let i = 0; i < 12; i++) {
        addBlock(80, `seed-${i}`)
      }

      await new Promise<void>(r => requestAnimationFrame(() => requestAnimationFrame(() => r())))
      api.scrollGroupChatToBottom(viewport, 'auto')
      await new Promise<void>(r => requestAnimationFrame(() => requestAnimationFrame(() => r())))
      syncPill()

      const afterSeedNear = api.isGroupChatNearBottom(viewport)
      const seedTop = viewport.scrollTop

      // (a) streaming-style growth while stuck — height change only.
      addBlock(220, 'stream-chunk')
      await new Promise<void>(r => requestAnimationFrame(() => requestAnimationFrame(() => r())))
      // Give ResizeObserver a tick.
      await new Promise<void>(r => setTimeout(r, 50))
      const afterStreamNear = api.isGroupChatNearBottom(viewport)
      const afterStreamTop = viewport.scrollTop

      // Late image: grow an existing block (same node count, taller content).
      const stream = document.getElementById('stream-chunk')!
      stream.style.height = '480px'
      await new Promise<void>(r => setTimeout(r, 50))
      await new Promise<void>(r => requestAnimationFrame(() => requestAnimationFrame(() => r())))
      const afterImageNear = api.isGroupChatNearBottom(viewport)

      // (b) scroll up — pill shows; growth must NOT yank.
      viewport.scrollTop = 40
      await new Promise<void>(r => requestAnimationFrame(() => requestAnimationFrame(() => r())))
      syncPill()
      const pillWhileUp = pill.style.display !== 'none'
      const topWhileUp = viewport.scrollTop
      addBlock(300, 'while-scrolled-up')
      await new Promise<void>(r => setTimeout(r, 50))
      await new Promise<void>(r => requestAnimationFrame(() => requestAnimationFrame(() => r())))
      const topAfterGrowWhileUp = viewport.scrollTop
      const stillUp = !api.isGroupChatNearBottom(viewport)

      // Pill click jumps to latest.
      pill.click()
      await new Promise<void>(r => requestAnimationFrame(() => requestAnimationFrame(() => r())))
      const afterPillNear = api.isGroupChatNearBottom(viewport)
      const pillHidden = pill.style.display === 'none'

      return {
        afterSeedNear,
        seedTop,
        afterStreamNear,
        afterStreamTop,
        afterImageNear,
        pillWhileUp,
        topWhileUp,
        topAfterGrowWhileUp,
        stillUp,
        afterPillNear,
        pillHidden
      }
    })

    expect(result.afterSeedNear, 'seed ends near bottom').toBe(true)
    expect(result.afterStreamNear, 'streaming growth stays near bottom').toBe(true)
    expect(result.afterStreamTop, 'streaming growth advanced scrollTop').toBeGreaterThan(result.seedTop)
    expect(result.afterImageNear, 'late image growth stays near bottom').toBe(true)
    expect(result.pillWhileUp, 'pill visible when scrolled up').toBe(true)
    expect(result.topAfterGrowWhileUp, 'no yank while scrolled up').toBe(result.topWhileUp)
    expect(result.stillUp, 'still not near bottom after growth while up').toBe(true)
    expect(result.afterPillNear, 'pill click jumps to latest').toBe(true)
    expect(result.pillHidden, 'pill hides after jump').toBe(true)
  } finally {
    const pid = app.process().pid
    await app.close().catch(() => undefined)

    // Best-effort tree kill if close left orphans (t_ecc82493).
    if (pid) {
      try {
        process.kill(pid)
      } catch {
        /* already gone */
      }
    }

    rmSync(temporary, { force: true, recursive: true })
  }
})
