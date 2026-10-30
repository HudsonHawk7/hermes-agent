/**
 * Layout bounding-box proof for Needs you option cards (GM 10/29).
 *
 * Full desktop boot is covered by needs-you.spec.ts (j). This file loads a
 * static fixture that mirrors the fixed DOM/CSS contract at the real column
 * width so we can assert no badge/label overlap and no effect clip without
 * waiting on the gateway.
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { expect, test } from '@playwright/test'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const FIXTURE = path.join(__dirname, 'fixtures', 'needs-you-layout.html')
const PROOF = path.join('S:/ForceProof/hermes-desktop/gm-asks', 'layout-t_62556591')

test.describe('Needs you layout fixture (column width)', () => {
  test('(h-static) long-text layout boxes: badge above label, effect inside button, short link, outline recommended', async ({
    page,
  }) => {
    fs.mkdirSync(PROOF, { recursive: true })
    await page.setViewportSize({ width: 400, height: 720 })
    await page.goto(pathToFileURL(FIXTURE).href)

    const card = page.getByTestId('needs-you-card')
    await expect(card).toBeVisible()

    const openBtn = card.getByTestId('needs-you-open')
    await expect(openBtn).toHaveText(/build the read-only work mail/i)
    await expect(openBtn).not.toHaveText(/GM\s*yes\s*\/\s*no\s*:/i)

    await expect(card.getByText(/Asker's pick\s*:/i)).toHaveCount(0)
    await expect(card.getByTestId('needs-you-recommend')).toBeVisible()
    const badge = card.getByTestId('needs-you-pick-badge')
    await expect(badge).toBeVisible()

    const yesBtn = card.getByTestId('needs-you-opt-yes')
    await expect(yesBtn).toHaveAttribute('data-recommended', 'true')
    await expect(yesBtn).not.toHaveClass(/filled-primary|bg-primary/)

    const label = yesBtn.getByTestId('needs-you-opt-label')
    const effect = yesBtn.getByTestId('needs-you-opt-effect')
    await expect(effect).toContainText(/Backbone notes land on force\/carry/i)
    // Effect is long enough to wrap but still under EFFECT_MAX (110).

    const badgeBox = await badge.boundingBox()
    const labelBox = await label.boundingBox()
    const effectBox = await effect.boundingBox()
    const btnBox = await yesBtn.boundingBox()
    expect(badgeBox && labelBox && effectBox && btnBox).toBeTruthy()

    // Badge on its own line above the label (no overlap).
    expect(badgeBox!.y + badgeBox!.height).toBeLessThanOrEqual(labelBox!.y + 1)
    // Effect fully inside the option button (no clip at the edge).
    expect(effectBox!.x).toBeGreaterThanOrEqual(btnBox!.x - 1)
    expect(effectBox!.x + effectBox!.width).toBeLessThanOrEqual(btnBox!.x + btnBox!.width + 1)
    expect(effectBox!.y + effectBox!.height).toBeLessThanOrEqual(btnBox!.y + btnBox!.height + 1)

    const link = card
      .getByTestId('needs-you-facts')
      .locator('a[href*="github.com/HudsonHawk7/Hyperdrive/pull/2"]')

    await expect(link).toBeVisible()
    await expect(link).toHaveText(/Hyperdrive PR #2/i)
    await expect(link).not.toHaveText(/https?:\/\//i)

    await card.screenshot({ path: path.join(PROOF, 'h-layout-long-text-after.png') })
    await page.screenshot({ path: path.join(PROOF, 'h-layout-column-after.png'), fullPage: true })
  })
})
