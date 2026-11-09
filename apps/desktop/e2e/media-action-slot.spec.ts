/**
 * Hidden proof for the chat media-action slot (t_aa52bbd5).
 *
 * A picture in an isolated probe chat shows hello-runtime's "Refine test"
 * button. The click hands the plugin the file path, session id and profile.
 * The sandbox is never solara or aura, and the plugin refuses those names.
 *
 * Windows stay hidden. The capture is Playwright's page screenshot (CDP),
 * not an OS screen grab.
 */

import * as fs from 'node:fs'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

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

const SESSION_TITLE = 'Probe media action'
const WRAP = 'Here is the picture.'
const SURFACE = '[data-composer-target]:not([data-pane-hidden] [data-composer-target])'
const IMAGE_SLOT = '[data-slot="aui_markdown-image"] img'
const ACTION = '[data-media-action="hello-runtime:refine-test"]'
const PROOF_PATH = 'S:/ForceAgents/hera/work/media-action-slot-proof.png'

const PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAKAAAABkCAIAAACO1KzYAAAA30lEQVR42u3dwQ2AIBAAQTAWAx1iBXYI7diCuWhEMvP2dZsj+CL30hLr2oxAYARGYARGYARGYIERGIH53n7nozpOk5rQqIcNdkQjMAIjMNPeomP3N54V+5exwY5oBEZgBEZgBEZggREYgREYgREYgQVGYARGYARGYARGYIERGIERGIERGIEFRmAERmAERmAEFhiBERiBERiBERiBBUZgBEZgBEZgBBYYgREYgREYgRFYYCMQGIERmPSj94Njb9ligxEYgRFYYJaQe2mmYIMRGIERGIERGIEFRmAERmDedAFtjAtAGWDnoAAAAABJRU5ErkJggg=='

const here = path.dirname(fileURLToPath(import.meta.url))

interface SeededFixture {
  app: ElectronApplication
  mock: MockServer
  page: Page
  sandbox: Sandbox
  imagePath: string
  cleanup: () => Promise<void>
}

function installHelloRuntime(sandbox: Sandbox): void {
  const source = path.join(here, '../src/plugins/hello-runtime/plugin.runtime.js')
  const destDir = path.join(sandbox.hermesHome, 'desktop-plugins', 'hello-runtime')

  fs.mkdirSync(destDir, { recursive: true })
  fs.copyFileSync(source, path.join(destDir, 'plugin.js'))
}

async function seedSession(hermesHome: string): Promise<void> {
  const builder = await RealSessionBuilder.start(hermesHome)

  try {
    await builder.createSession({
      title: SESSION_TITLE,
      turns: [{ text: `${SESSION_TITLE}. Show the probe picture.` }],
    })
  } finally {
    await builder.close()
  }
}

async function setup(): Promise<SeededFixture> {
  const sandbox = createSandbox('media-action-slot')
  const home = sandbox.hermesHome.replace(/\\/g, '/').toLowerCase()

  expect(home, 'probe sandbox must not be a girl profile').not.toContain('solara')
  expect(home, 'probe sandbox must not be a girl profile').not.toContain('aura')

  const imageDir = path.join(sandbox.hermesHome, 'renders', 'e2e-media-action')
  const imagePath = path.join(imageDir, 'garden.png')

  fs.mkdirSync(imageDir, { recursive: true })
  fs.writeFileSync(imagePath, Buffer.from(PNG_BASE64, 'base64'))

  const mock = await startMockServer({
    replyForPrompt: () => `${WRAP}\nMEDIA:${imagePath}`,
  })

  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)
  await seedSession(sandbox.hermesHome)
  installHelloRuntime(sandbox)

  const { app, page } = await launchDesktop(buildAppEnv(sandbox))

  return {
    app,
    mock,
    page,
    sandbox,
    imagePath,
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

test.describe('chat media-action slot', () => {
  let fixture: SeededFixture | null = null

  test.afterEach(async () => {
    await fixture?.cleanup()
    fixture = null
  })

  test('hello-runtime Refine test shows on a probe picture and never on solara or aura', async () => {
    test.slow()
    fixture = await setup()
    await waitForAppReady(fixture, 120_000)

    const state = await fixture.app.evaluate(({ BrowserWindow }) => {
      const windows = BrowserWindow.getAllWindows() as Array<{
        isVisible: () => boolean
        isDestroyed: () => boolean
        getOpacity: () => number
      }>

      return {
        count: windows.length,
        visible: windows.map(win => ({
          isVisible: win.isVisible(),
          isDestroyed: win.isDestroyed(),
          opacity: win.getOpacity(),
        })),
      }
    })

    expect(state.count, 'expected at least the main window').toBeGreaterThan(0)
    expect(
      state.visible.every(win => !win.isVisible && !win.isDestroyed && win.opacity === 1),
      `window(s) became visible under HERMES_DESKTOP_E2E_HIDDEN: ${JSON.stringify(state.visible)}`,
    ).toBe(true)

    const row = fixture.page.locator('[data-slot="sidebar"] button').filter({ hasText: SESSION_TITLE }).first()

    await row.waitFor({ state: 'visible', timeout: 60_000 })
    await row.click()
    await fixture.page.waitForFunction(
      ([expected, surfaceSelector]: [string, string]) => {
        const surfaces = document.querySelectorAll(surfaceSelector)
        const text = surfaces[surfaces.length - 1]?.querySelector('[data-slot="aui_thread-viewport"]')?.textContent ?? ''

        return text.includes(expected)
      },
      [WRAP, SURFACE] as [string, string],
      { timeout: 30_000 },
    )

    const image = fixture.page.locator(IMAGE_SLOT).first()

    await expect(image, 'the probe picture should paint').toBeVisible({ timeout: 30_000 })
    await image.hover()

    const action = fixture.page.locator(ACTION).first()

    await expect(action, 'Refine test should show on the probe picture').toBeVisible({ timeout: 15_000 })
    await expect(action).toHaveAttribute('aria-label', 'Refine test')

    const png = await fixture.page.screenshot({ type: 'png' })

    fs.mkdirSync(path.dirname(PROOF_PATH), { recursive: true })
    fs.writeFileSync(PROOF_PATH, png)
    expect(png.byteLength, 'hidden capture should be a real image').toBeGreaterThan(1000)

    await action.click()

    const proof = await fixture.page.evaluate(() => {
      const value = (globalThis as { __hermesMediaActionProof?: Record<string, string> }).__hermesMediaActionProof

      return value ?? null
    })

    expect(proof, 'plugin handler should record the click').not.toBeNull()
    expect(proof?.path.toLowerCase()).toContain('garden.png')
    expect(proof?.kind).toBe('image')
    expect((proof?.sessionId ?? '').length).toBeGreaterThan(0)
    expect(proof?.profile.toLowerCase()).not.toBe('solara')
    expect(proof?.profile.toLowerCase()).not.toBe('aura')
    expect(proof?.label).toBe('Refine test')
  })
})
