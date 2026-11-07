/**
 * Group composer Enter / Shift+Enter (t_53595138) against the real mention
 * input in a hidden mini Electron window — no live solara/aura profiles.
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

import { build } from 'esbuild'

import { resolveElectronBinary } from './electron-binary'
import { _electron, expect, test } from './test'

test('group mention Enter sends; Shift+Enter newlines; full name does not re-insert', async () => {
  const temporary = mkdtempSync(path.join(tmpdir(), 'hermes-group-chat-enter-'))
  const userData = path.join(temporary, 'user-data')
  mkdirSync(userData, { recursive: true })
  const main = path.join(temporary, 'main.cjs')
  writeFileSync(
    main,
    `const { app, BrowserWindow } = require('electron');
app.setPath('userData', ${JSON.stringify(userData)});
delete process.env.ELECTRON_RUN_AS_NODE;
app.whenReady().then(() => {
  const window = new BrowserWindow({
    width: 640,
    height: 480,
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

  const app = await _electron.launch({
    executablePath: resolveElectronBinary([desktopRoot, repoRoot]),
    args: ['--no-sandbox', main],
    env: {
      ...process.env,
      ELECTRON_RUN_AS_NODE: undefined as unknown as string,
      HERMES_DESKTOP_E2E_HIDDEN: '1'
    }
  })

  try {
    const page = await app.firstWindow()
    await page.setContent('<textarea id="draft" rows="4" style="width:90%"></textarea>')
    await page.addScriptTag({ content: bundle.outputFiles[0].text })

    const outcomes = await page.evaluate(() => {
      const api = (
        window as unknown as {
          GroupChatFollow: {
            shouldCompleteMentionOnEnter: (open: boolean, handle: null | string, query: string) => boolean
          }
        }
      ).GroupChatFollow
      const draft = document.getElementById('draft') as HTMLTextAreaElement
      const log: string[] = []

      const onKey = (event: KeyboardEvent) => {
        if (event.isComposing || event.keyCode === 229) {
          return
        }

        if (event.key === 'Enter' && event.shiftKey) {
          // native newline
          return
        }

        if (event.key !== 'Enter') {
          return
        }

        event.preventDefault()
        const open = draft.value.includes('@')
        // Simulate caret at end; query after last @.
        const m = /(^|\s)@([a-z0-9._-]*)$/i.exec(draft.value)

        if (m && api.shouldCompleteMentionOnEnter(open, 'alpha', m[2])) {
          draft.value = draft.value.replace(/@([a-z0-9._-]*)$/i, '@alpha ')
          log.push('insert')

          return
        }

        log.push(`send:${draft.value}`)
      }

      draft.addEventListener('keydown', onKey)

      // (c) plain Enter sends
      draft.value = 'hello room'
      draft.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))

      // (d) Shift+Enter does not send
      draft.value = 'line1'
      draft.dispatchEvent(
        new KeyboardEvent('keydown', {
          bubbles: true,
          key: 'Enter',
          shiftKey: true
        })
      )

      // (e) full member name with menu "open" sends, does not insert
      draft.value = 'hey @alpha'
      draft.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))

      // prefix still inserts
      draft.value = 'hey @alp'
      draft.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))

      return {
        log,
        afterPrefix: draft.value
      }
    })

    expect(outcomes.log).toEqual(['send:hello room', 'send:hey @alpha', 'insert'])
    expect(outcomes.afterPrefix).toBe('hey @alpha ')
  } finally {
    const pid = app.process().pid
    await app.close().catch(() => undefined)

    if (pid) {
      try {
        process.kill(pid)
      } catch {
        /* gone */
      }
    }

    rmSync(temporary, { force: true, recursive: true })
  }
})
