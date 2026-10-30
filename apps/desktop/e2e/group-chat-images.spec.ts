/**
 * Group-chat image UX: a real Files drop on the room lands in the pending tray.
 * Vision chips are covered by unit tests (group-vision.test.ts) + gateway receipt
 * tests — this e2e only proves the Electron/room drop path the GM reported broken.
 */

import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { MOCK_REPLY } from '../../../tests-js/scripts/mock-server'

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { expect, test } from './test'

const PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAKAAAABkCAIAAACO1KzYAAAA30lEQVR42u3dwQ2AIBAAQTAWAx1iBXYI7diCuWhEMvP2dZsj+CL30hLr2oxAYARGYARGYARGYIERGIH53n7nozpOk5rQqIcNdkQjMAIjMNPeomP3N54V+5exwY5oBEZgBEZgBEZggREYgREYgREYgQVGYARGYARGYARGYIERGIERGIERGIEFRmAERmAERmAEFhiBERiBERiBERiBBUZgBEZgBEZgBBYYgREYgREYgRFYYCMQGIERmPSj94Njb9ligxEYgRFYYJaQe2mmYIMRGIERGIERGIEFRmAERmDedAFtjAtAGWDnoAAAAABJRU5ErkJggg=='

const SHOTS = path.join(os.tmpdir(), 'group-chat-images-e2e')
let fixture: MockBackendFixture | null = null

async function openBots(page: MockBackendFixture['page']): Promise<void> {
  const tab = page
    .getByRole('button', { name: 'Bots', exact: true })
    .or(page.getByRole('tab', { name: 'Bots', exact: true }))
    .first()

  await tab.click()
  await expect(page.getByRole('button', { name: 'New bot or group chat' })).toBeVisible()
}

async function createAgent(page: MockBackendFixture['page'], name: string, title: string): Promise<void> {
  await page.getByRole('button', { name: 'New bot or group chat' }).click()
  await page.getByRole('menuitem', { name: 'New Bot' }).click()

  const dialog = page.getByRole('dialog', { name: 'New Bot' })
  await dialog.getByPlaceholder('inbox-triage').fill(name)
  await dialog.getByPlaceholder('Inbox Triage').fill(title)
  await dialog.getByRole('button', { name: 'Create Bot' }).click()
  await expect(dialog).toBeHidden({ timeout: 30_000 })
  await expect(page.getByRole('button', { name: new RegExp(`^${title}\\b`) }).first()).toBeVisible({
    timeout: 30_000
  })
}

async function createRoom(page: MockBackendFixture['page']) {
  await openBots(page)
  await createAgent(page, 'vision-a', 'VisionA')
  await createAgent(page, 'vision-b', 'VisionB')

  await page.getByRole('button', { name: 'New bot or group chat' }).click()
  await page.getByRole('menuitem', { name: 'New Group Chat' }).click()

  const dialog = page.getByRole('dialog', { name: 'New Group Chat' })

  for (const title of ['VisionA', 'VisionB']) {
    await dialog.getByText(title, { exact: true }).locator('xpath=ancestor::label').getByRole('checkbox').click()
  }

  await dialog.getByRole('textbox', { name: 'Group name' }).fill('Vision Eval Room')
  await dialog.getByRole('button', { name: 'Create Group (2)' }).click()

  const groupTab = page.getByRole('tab', { name: /Vision Eval Room Close/ })
  const groupComposer = page.getByRole('textbox', { name: 'Message Vision Eval Room' }).filter({ visible: true })
  await expect(groupTab).toBeVisible({ timeout: 20_000 })
  await expect(groupComposer).toBeVisible()

  return groupComposer
}

/** Dispatch a real drag/drop of a File onto the group room root. */
async function dropPngOnRoom(page: MockBackendFixture['page'], fileName: string) {
  const bytes = Buffer.from(PNG_BASE64, 'base64')
  const base64 = PNG_BASE64

  await page.evaluate(
    async ({ base64: b64, fileName: name }) => {
      const bin = atob(b64)
      const arr = new Uint8Array(bin.length)

      for (let i = 0; i < bin.length; i++) {
        arr[i] = bin.charCodeAt(i)
      }

      const file = new File([arr], name, { type: 'image/png' })
      const dt = new DataTransfer()
      dt.items.add(file)

      const root =
        document.querySelector('[data-preview-annotate-destination="group"]') ||
        document.querySelector('[data-preview-annotate-group]')

      if (!root) {
        throw new Error('group room root not found')
      }

      for (const type of ['dragenter', 'dragover', 'drop'] as const) {
        root.dispatchEvent(
          new DragEvent(type, {
            bubbles: true,
            cancelable: true,
            dataTransfer: dt
          })
        )
      }
    },
    { base64, fileName }
  )

  void bytes
}

test.beforeEach(async () => {
  fs.mkdirSync(SHOTS, { recursive: true })
  fixture = await setupMockBackend({
    mockServer: {
      replyForPrompt: () => MOCK_REPLY
    }
  })
  await waitForAppReady(fixture, 120_000)
})

test.afterEach(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('group room file drop puts the image in the pending tray', async () => {
  test.setTimeout(180_000)
  const page = fixture!.page
  await createRoom(page)

  await dropPngOnRoom(page, 'e2e-drop.png')

  const tray = page.getByTestId('group-pending-attachments')
  await expect(tray).toBeVisible({ timeout: 15_000 })
  await expect(tray.getByText('e2e-drop.png')).toBeVisible()
  await expect(tray.locator('img')).toHaveCount(1)

  // Drop must not navigate the SPA away from the room.
  await expect(page.getByRole('textbox', { name: 'Message Vision Eval Room' }).filter({ visible: true })).toBeVisible()

  await page.screenshot({ path: path.join(SHOTS, 'group-drop-pending.png') })
})
