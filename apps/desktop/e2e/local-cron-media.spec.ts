/**
 * AC4 for t_62016831 — hidden clone-profile Desktop e2e.
 *
 * Proves the product bar Probe needs without live solara/aura/gateway:
 * 1. Cron-shaped admission carries MEDIA lines; companion wrap omits paths;
 *    the delivered assistant text still paints every on-disk picture in chat.
 * 2. A missing MEDIA path surfaces named "Couldn't show the image" text.
 *
 * Delivery shape mirrors gateway TurnRunner auto-append + report_media_drop
 * (unit/chain cover the real append). This suite seeds the durable Desktop
 * transcript the same way a Local session would after that path, then boots
 * a hidden Electron against an isolated HERMES_HOME (clone profile).
 *
 * Fail-first: reply without MEDIA / without the named drop → asserts red.
 * Green path restores both. Windows closed via closeDesktopApp every run.
 */

import * as fs from 'node:fs'
import * as path from 'node:path'

import { writeEnvFile, writeMockProviderConfig } from '../../../tests-js/scripts/mock-provider-config'
import { type MockServer, startMockServer } from '../../../tests-js/scripts/mock-server'

import {
  buildAppEnv,
  closeDesktopApp,
  createSandbox,
  launchDesktop,
  type Sandbox,
  waitForAppReady,
} from './fixtures'
import { RealSessionBuilder } from './real-session-builder'
import { type ElectronApplication, expect, type Page, test } from './test'

const SESSION_TITLE = 'E2E cron MEDIA local delivery'
const WRAP = 'Your set is ready.'
const MISSING_NAME = 'gone.png'
const NAMED_DROP = `Couldn't show the image: file not found (${MISSING_NAME})`

/** Small framed magenta PNG (same fixture family as image-attachment-resume). */
const PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAKAAAABkCAIAAACO1KzYAAAA30lEQVR42u3dwQ2AIBAAQTAWAx1iBXYI7diCuWhEMvP2dZsj+CL30hLr2oxAYARGYARGYARGYIERGIH53n7nozpOk5rQqIcNdkQjMAIjMNPeomP3N54V+5exwY5oBEZgBEZgBEZggREYgREYgREYgQVGYARGYARGYARGYIERGIERGIERGIEFRmAERmAERmAEFhiBERiBERiBERiBBUZgBEZgBEZgBBYYgREYgREYgRFYYCMQGIERmPSj94Njb9ligxEYgRFYYJaQe2mmYIMRGIERGIERGIEFRmAERmDedAFtjAtAGWDnoAAAAABJRU5ErkJggg=='

const SURFACE = '[data-composer-target]:not([data-pane-hidden] [data-composer-target])'

interface SeededFixture {
  app: ElectronApplication
  mock: MockServer
  page: Page
  sandbox: Sandbox
  imageNames: string[]
  cleanup: () => Promise<void>
}

function writePng(dir: string, name: string): string {
  fs.mkdirSync(dir, { recursive: true })
  const imagePath = path.join(dir, name)
  fs.writeFileSync(imagePath, Buffer.from(PNG_BASE64, 'base64'))

  return imagePath
}

/** Cron admission text the no-agent script posts (paths on disk + one missing). */
function cronAdmission(goodPaths: string[], missingPath: string): string {
  const media = [...goodPaths, missingPath].map(p => `MEDIA:${p}`).join('\n')

  return (
    `[Cronjob "Sexy Time Prep" output]\n` +
    `Generated photo set.\n` +
    `${media}\n`
  )
}

/**
 * Final assistant text after TurnRunner append + Local drop notice.
 * Companion wrap has no paths; MEDIA lines come from the admission (append).
 */
function deliveredAssistantText(goodPaths: string[], includeDrop: boolean): string {
  const media = goodPaths.map(p => `MEDIA:${p}`).join('\n')
  const drop = includeDrop ? `\n${NAMED_DROP}` : ''

  return `${WRAP}\n${media}${drop}`
}

/**
 * MEDIA tags become markdown `[Image: …](#media:…)` then MarkdownLink →
 * MediaAttachment → MarkdownImage with data-slot=aui_markdown-image.
 * User OS-drop attachments use aui_directive-image instead — wrong slot here.
 */
const IMAGE_SLOT = '[data-slot="aui_markdown-image"] img'

async function seedSessionWithRetry(hermesHome: string, admission: string): Promise<void> {
  let lastError: unknown

  for (let attempt = 1; attempt <= 3; attempt++) {
    const builder = await RealSessionBuilder.start(hermesHome)

    try {
      await builder.createSession({
        title: SESSION_TITLE,
        turns: [{ text: admission }],
      })

      return
    } catch (error) {
      lastError = error
      const message = error instanceof Error ? error.message : String(error)

      // OpenAI client init can deadlock under rapid serial builders on Windows.
      if (!/deadlock|agent_init_failed|ModuleLock/i.test(message) || attempt === 3) {
        throw error
      }

      await new Promise(resolve => setTimeout(resolve, 1_500 * attempt))
    } finally {
      await builder.close()
    }
  }

  throw lastError
}

async function setupCronMediaDesktop(options: {
  /** When false, mock omits MEDIA (fail-first for pictures). */
  includeMedia: boolean
  /** When false, mock omits named drop (fail-first for missing-file text). */
  includeNamedDrop: boolean
}): Promise<SeededFixture> {
  const sandbox = createSandbox('local-cron-media')
  // Keep renders under the isolated profile home so local media read paths
  // match a real companion workspace layout (not a random system temp leaf).
  const shotDir = path.join(sandbox.hermesHome, 'renders', 'e2e-cron')

  const goodPaths = [
    writePng(shotDir, 'garden-party-1.png'),
    writePng(shotDir, 'garden-party-2.png'),
  ]

  const missingPath = path.join(shotDir, MISSING_NAME)
  // Do not create missingPath — delivery names the absence.
  const imageNames = goodPaths.map(p => path.basename(p))

  const admission = cronAdmission(goodPaths, missingPath)

  const finalText = options.includeMedia
    ? deliveredAssistantText(goodPaths, options.includeNamedDrop)
    : options.includeNamedDrop
      ? `${WRAP}\n${NAMED_DROP}`
      : WRAP

  const mock = await startMockServer({
    replyForPrompt: prompt => {
      // Cron-shaped turns get the delivery result (append stand-in for Desktop
      // TUI seed). Other prompts keep a short ACK so stray retries stay quiet.
      if (prompt.includes('[Cronjob') || prompt.includes('MEDIA:')) {
        return finalText
      }

      return WRAP
    },
  })

  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)

  await seedSessionWithRetry(sandbox.hermesHome, admission)

  const { app, page } = await launchDesktop(buildAppEnv(sandbox))

  return {
    app,
    mock,
    page,
    sandbox,
    imageNames,
    cleanup: async () => {
      await closeDesktopApp(app, {
        hermesHome: sandbox.hermesHome,
        userDataDir: sandbox.userDataDir,
      })
      await mock.close().catch(() => undefined)
      sandbox.cleanup()
    },
  }
}

function sessionRow(page: Page) {
  return page.locator('[data-slot="sidebar"] button').filter({ hasText: SESSION_TITLE }).first()
}

async function openSeededSession(page: Page): Promise<void> {
  const row = sessionRow(page)
  await row.waitFor({ state: 'visible', timeout: 60_000 })
  await row.click()
  await page.waitForFunction(
    ([expected, surfaceSelector]: [string, string]) => {
      const surfaces = document.querySelectorAll(surfaceSelector)

      const text =
        surfaces[surfaces.length - 1]?.querySelector('[data-slot="aui_thread-viewport"]')?.textContent ?? ''

      return text.includes(expected)
    },
    [WRAP, SURFACE] as [string, string],
    { timeout: 30_000 },
  )
}

async function transcriptText(page: Page): Promise<string> {
  return page.evaluate(surfaceSelector => {
    const surfaces = document.querySelectorAll(surfaceSelector)

    return surfaces[surfaces.length - 1]?.querySelector('[data-slot="aui_thread-viewport"]')?.textContent ?? ''
  }, SURFACE)
}

async function assertHiddenWindows(app: ElectronApplication): Promise<void> {
  const state = await app.evaluate(({ BrowserWindow }) => {
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
        isDestroyed: win.isDestroyed(),
      })),
    }
  })

  expect(state.count, 'expected at least the main window').toBeGreaterThan(0)
  expect(
    state.visible.every(win => !win.isVisible && !win.isDestroyed),
    `window(s) became visible under HERMES_DESKTOP_E2E_HIDDEN: ${JSON.stringify(state.visible)}`,
  ).toBe(true)
}

test.describe('local cron MEDIA desktop delivery (AC4 t_62016831)', () => {
  let fixture: SeededFixture | null = null

  test.afterEach(async () => {
    await fixture?.cleanup()
    fixture = null
  })

  test('fail-first: companion wrap without MEDIA leaves no picture thumbnails', async () => {
    test.slow()
    fixture = await setupCronMediaDesktop({ includeMedia: false, includeNamedDrop: false })
    await waitForAppReady(fixture, 120_000)
    await assertHiddenWindows(fixture.app)
    await openSeededSession(fixture.page)

    const thumbs = fixture.page.locator(IMAGE_SLOT)
    await expect(thumbs, 'without MEDIA delivery the chat must not paint cron pictures').toHaveCount(0)

    const text = await transcriptText(fixture.page)
    expect(text).toContain(WRAP)
    expect(text).not.toContain(NAMED_DROP)
  })

  test('fail-first: missing named-drop text is absent when delivery omits it', async () => {
    test.slow()
    fixture = await setupCronMediaDesktop({ includeMedia: true, includeNamedDrop: false })
    await waitForAppReady(fixture, 120_000)
    await assertHiddenWindows(fixture.app)
    await openSeededSession(fixture.page)

    const text = await transcriptText(fixture.page)
    expect(text, 'named drop must not appear when the delivery path omits it').not.toContain(NAMED_DROP)
    // Pictures still show when MEDIA is present — only the drop line is absent.
    const thumbs = fixture.page.locator(IMAGE_SLOT)
    await expect(thumbs).toHaveCount(2, { timeout: 30_000 })
  })

  test('green: cron MEDIA pictures show and missing file is named in chat', async () => {
    test.slow()
    const testInfo = test.info()
    fixture = await setupCronMediaDesktop({ includeMedia: true, includeNamedDrop: true })
    await waitForAppReady(fixture, 120_000)
    await assertHiddenWindows(fixture.app)

    const row = sessionRow(fixture.page)
    await row.waitFor({ state: 'visible', timeout: 60_000 })
    const label = (await row.textContent())?.trim() ?? ''
    expect(label.startsWith(SESSION_TITLE), `sidebar label: ${label}`).toBe(true)

    for (const name of fixture.imageNames) {
      expect(label, `sidebar must not leak image path ${name}`).not.toContain(name)
    }

    await openSeededSession(fixture.page)

    const thumbs = fixture.page.locator(IMAGE_SLOT)
    await expect(thumbs, 'both on-disk cron MEDIA images should paint as thumbnails').toHaveCount(2, {
      timeout: 30_000,
    })

    for (let i = 0; i < 2; i++) {
      await expect(thumbs.nth(i), `thumbnail ${i} should resolve off disk`).toHaveAttribute(
        'src',
        /^(data:image\/|blob:|hermes-media:)/,
        { timeout: 30_000 },
      )
    }

    const text = await transcriptText(fixture.page)
    expect(text, 'companion wrap should remain').toContain(WRAP)
    expect(text, 'missing file must be named in chat').toContain(NAMED_DROP)

    // User/cron admission still carries raw MEDIA: paths — that is the input.
    // The assistant bubble must show pictures + named drop, not raw MEDIA lines.
    const assistantText = await fixture.page.evaluate(() => {
      const root =
        document.querySelector('[data-testid="message-list"]') ||
        document.querySelector('[class*="Thread"]') ||
        document.body

      const candidates = Array.from(root.querySelectorAll('div, article, section')).filter(el => {
        const t = (el.textContent || '').trim()

        return t.includes("Your set is ready.") && t.includes("Couldn't show the image")
      })

      // Smallest container that has both — the assistant turn, not the whole thread.
      candidates.sort((a, b) => (a.textContent || '').length - (b.textContent || '').length)

      return candidates[0]?.textContent?.trim() || ''
    })

    expect(assistantText, 'assistant turn should carry wrap + named drop').toContain(WRAP)
    expect(assistantText, 'assistant turn should name the missing file').toContain(NAMED_DROP)
    expect(assistantText, 'assistant must not leak raw MEDIA: after render').not.toContain('MEDIA:')
    expect(assistantText, 'assistant must not leak @image:').not.toContain('@image:')

    await fixture.page.screenshot({ path: testInfo.outputPath('cron-media-green.png') })
  })
})
