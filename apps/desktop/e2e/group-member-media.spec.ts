import fs from 'node:fs'
import path from 'node:path'

import { MEMBER_MEDIA_TRIGGER } from '../../../tests-js/scripts/mock-server'

import { type MockBackendFixture, selectSessionTab, setupMockBackend, waitForAppReady } from './fixtures'
import { expect, test } from './test'

const ROOM = 'Programmer, Reviewer'

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL9xQAAAABJRU5ErkJggg==',
  'base64',
)

let fixture: MockBackendFixture | null = null

async function openBots(page: MockBackendFixture['page']): Promise<void> {
  const tab = page.getByRole('button', { name: 'Bots', exact: true }).or(page.getByRole('tab', { name: 'Bots', exact: true })).first()
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
  await expect(page.getByRole('button', { name: new RegExp(`^${title}\\b`) }).first()).toBeVisible({ timeout: 30_000 })
}

async function createRoom(page: MockBackendFixture['page']) {
  await openBots(page)
  await createAgent(page, 'programmer', 'Programmer')
  await createAgent(page, 'reviewer', 'Reviewer')
  await page.getByRole('button', { name: 'New bot or group chat' }).click()
  await page.getByRole('menuitem', { name: 'New Group Chat' }).click()
  const dialog = page.getByRole('dialog', { name: 'New Group Chat' })

  for (const title of ['Programmer', 'Reviewer']) {
    await dialog.getByText(title, { exact: true }).locator('xpath=ancestor::label').getByRole('checkbox').click()
  }

  await dialog.getByRole('textbox', { name: 'Group name' }).fill(ROOM)
  await dialog.getByRole('button', { name: 'Create Group (2)' }).click()
  const composer = page.getByRole('textbox', { name: 'Group message' }).filter({ visible: true })
  await expect(async () => {
    await selectSessionTab(page, ROOM, 10_000)
    await expect(composer).toBeVisible({ timeout: 3_000 })
  }).toPass({ timeout: 30_000 })

  return composer
}

test.beforeEach(async () => {
  fixture = await setupMockBackend()
  await waitForAppReady(fixture, 120_000)
  // waitForAppReady pins the legacy localStorage rooms, which never see the
  // gateway's attachment commit. This spec drives the hosted room.
  await fixture.page.evaluate(() => {
    ;(window as unknown as { __HERMES_E2E_LEGACY_GROUPS?: boolean }).__HERMES_E2E_LEGACY_GROUPS = false
  })
})

test.afterEach(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('a member picture renders inline in the hosted room and a disallowed path stays a failure', async () => {
  test.setTimeout(240_000)
  const page = fixture!.page
  const composer = await createRoom(page)
  // native-inputs is an existing per-profile room-media root, so no bot config is mutated.
  const nativeInputs = path.join(fixture!.sandbox.hermesHome, 'profiles', 'programmer', 'cache', 'documents', 'native-inputs')
  const allowed = path.join(nativeInputs, 'member-picture.png')
  const outside = path.join(fixture!.sandbox.root, 'not-member-media.png')
  fs.mkdirSync(nativeInputs, { recursive: true })
  fs.writeFileSync(allowed, PNG)
  fs.writeFileSync(outside, PNG)

  await composer.fill(`@programmer ${MEMBER_MEDIA_TRIGGER}\n${allowed}\n${outside}`)
  await page.getByRole('button', { name: 'Send', exact: true }).filter({ visible: true }).click()
  await expect.poll(() => fixture!.mock.receivedPrompts.some(prompt => prompt.includes(MEMBER_MEDIA_TRIGGER)), { timeout: 60_000 }).toBe(true)

  const transcript = page.getByRole('log').filter({ visible: true })
  const picture = transcript.getByRole('img', { name: 'member-picture.png' })

  await expect(async () => {
    // A just-created bot's intro turn can front its private tab while the
    // room reply arrives; assert after returning to the room.
    await selectSessionTab(page, ROOM, 5_000)
    await expect(picture).toBeVisible({ timeout: 5_000 })
  }).toPass({ timeout: 120_000 })

  await expect.poll(() => picture.evaluate((image: HTMLImageElement) => ({ loaded: image.naturalWidth > 0, blob: image.currentSrc.startsWith('blob:') }))).toEqual({
    loaded: true,
    blob: true,
  })
  await expect(transcript.getByRole('button', { name: 'Download', exact: true })).toHaveCount(1)
  await expect(transcript.getByText('(picture not shared: outside allowed folders)')).toBeVisible()
  await expect(transcript.getByRole('img', { name: 'not-member-media.png' })).toHaveCount(0)
  await expect(transcript).not.toContainText('MEDIA:')
})
