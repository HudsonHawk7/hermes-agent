/**
 * Approvals 2/2 — amber Needs you column + one-tap answers (Hermes Desktop).
 *
 * Seeds two boards via the part-1 CLI surface into a scratch HERMES_HOME, enables
 * the kanban plugin, and drives the real board UI for the GM proof cases a–g.
 *
 * Screenshots land under S:/ForceProof/hermes-desktop/gm-asks/<sha>/.
 */
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { writeEnvFile, writeMockProviderConfig } from '../../../tests-js/scripts/mock-provider-config'
import { startMockServer } from '../../../tests-js/scripts/mock-server'

import {
  buildAppEnv,
  createSandbox,
  killProcessTree,
  launchDesktop,
  listGatewayRunProcesses,
  type MockBackendFixture,
  reapSandboxGatewayRuns,
  waitForAppReady,
} from './fixtures'
import { allowErrorBanners, expect, type Page, test } from './test'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = path.resolve(__dirname, '../..', '..')
const SEED_SCRIPT = path.join(__dirname, 'seed-gm-asks.py')

function gitSha(): string {
  try {
    return execFileSync('git', ['rev-parse', '--short=12', 'HEAD'], {
      cwd: REPO_ROOT,
      encoding: 'utf8',
    }).trim()
  } catch {
    return 'unknown'
  }
}

function proofDir(sha: string): string {
  const dir = path.join('S:/ForceProof/hermes-desktop/gm-asks', sha)
  fs.mkdirSync(dir, { recursive: true })

  return dir
}

function seedHome(hermesHome: string): { ids: Record<string, string> } {
  const out = path.join(hermesHome, 'gm-asks-seed.json')
  const py = process.env.PYTHON || 'python'
  execFileSync(py, [SEED_SCRIPT, '--home', hermesHome, '--out', out], {
    cwd: REPO_ROOT,
    env: { ...process.env, HERMES_HOME: hermesHome, PYTHONPATH: REPO_ROOT },
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  return JSON.parse(fs.readFileSync(out, 'utf8')) as { ids: Record<string, string> }
}

async function openKanban(page: Page) {
  await page.evaluate(() => {
    try {
      localStorage.setItem('hermes.desktop.pluginDecisions.v2', JSON.stringify({ kanban: true }))
    } catch {
      // ignore
    }
  })
  await page.reload()
  await page.waitForTimeout(1500)
  await page.evaluate(() => {
    window.location.hash = '#/kanban'
  })
  const col = page.getByTestId('needs-you-column')

  try {
    await col.waitFor({ state: 'visible', timeout: 20_000 })

    return
  } catch {
    // palette fallback
  }

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+k' : 'Control+k')
  await page.keyboard.type('Open board')
  await page.keyboard.press('Enter')
  await col.waitFor({ state: 'visible', timeout: 45_000 })
}

/** Task drawer stays open across serial tests and blocks the board — close it. */
async function closeDrawer(page: Page) {
  for (let i = 0; i < 3; i++) {
    const bar = page.getByTestId('needs-you-drawer-bar')
    const dialog = page.getByRole('dialog')

    if ((await bar.count()) === 0 && (await dialog.count()) === 0) {break}
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)
  }

  // Completion-notify toasts (e.g. "Task blocked — needs your input") sit center-screen
  // and hide cards under them — dismiss so pixel proof can see the board.
  for (let i = 0; i < 5; i++) {
    const dismiss = page.getByRole('button', { name: /Dismiss notification/i }).first()

    if ((await dismiss.count()) === 0 || !(await dismiss.isVisible().catch(() => false))) {break}
    await dismiss.click({ force: true }).catch(() => undefined)
    await page.waitForTimeout(150)
  }
}

test.describe('Needs you column (gm-asks part 2)', () => {
  test.setTimeout(240_000)

  let fixture: MockBackendFixture | null = null
  let seed: { ids: Record<string, string> } | null = null
  let shots: string = ''

  test.beforeAll(async () => {
    shots = proofDir(gitSha())
    // Custom setup so we can seed HERMES_HOME before launch.
    const mock = await startMockServer()
    const sandbox = createSandbox('gm-asks')
    writeMockProviderConfig(sandbox.hermesHome, mock.url)
    writeEnvFile(sandbox.hermesHome)
    seed = seedHome(sandbox.hermesHome)

    const env = buildAppEnv(sandbox)

    // Worktree has no venv; use the live install's Python so the backend can
    // boot against HERMES_DESKTOP_HERMES_ROOT (this checkout with part-1 APIs).
    const livePython = path.join(
      process.env.LOCALAPPDATA || '',
      'hermes',
      'hermes-agent',
      'venv',
      process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
    )

    if (fs.existsSync(livePython)) {
      env.HERMES_DESKTOP_PYTHON = livePython
    }

    const { app, page } = await launchDesktop(env)

    fixture = {
      app,
      page,
      mock,
      mockUrl: mock.url,
      sandbox,
      cleanup: async () => {
        let electronPid: number | undefined

        try {
          electronPid = app.process().pid
        } catch {
          // already closed
        }

        await app.close().catch(() => undefined)

        // app.close() alone leaves hermes_cli.main gateway run pairs on Windows
        // (tree detached). Kill the electron tree, then any sandbox-tied gateways.
        if (electronPid) {
          killProcessTree(electronPid)
        }

        const reap = reapSandboxGatewayRuns(sandbox.hermesHome)
        const report = path.join(shots, 'gateway-reap.json')

        try {
          fs.writeFileSync(
            report,
            JSON.stringify(
              {
                after: reap.after.map(p => ({ pid: p.pid, cmd: p.commandLine.slice(0, 200) })),
                before: reap.before.map(p => ({ pid: p.pid, cmd: p.commandLine.slice(0, 200) })),
                killed: reap.killed,
                lockPids: reap.lockPids,
                sandbox: sandbox.hermesHome,
              },
              null,
              2,
            ),
            'utf8',
          )
        } catch {
          // proof dir may be gone; non-fatal
        }

        await mock.close()
        sandbox.cleanup()
      },
    }

    await waitForAppReady(fixture, 120_000)
    await openKanban(page)
  }, { timeout: 180_000 })

  test.afterAll(async () => {
    // Snapshot gateways before teardown so the summary can show before/after.
    const beforeTeardown = listGatewayRunProcesses()
    const beforePath = path.join(shots || proofDir(gitSha()), 'gateway-before-teardown.json')

    try {
      fs.writeFileSync(
        beforePath,
        JSON.stringify(
          beforeTeardown.map(p => ({ pid: p.pid, cmd: p.commandLine.slice(0, 240) })),
          null,
          2,
        ),
        'utf8',
      )
    } catch {
      // ignore
    }

    await fixture?.cleanup()
    fixture = null
  })

  test('(a) seeded asks appear in Needs you with column + tab counts', async () => {
    const page = fixture!.page
    const col = page.getByTestId('needs-you-column')
    await expect(col).toBeVisible()
    // 6 on alpha + 1 on beta = 7
    await expect(page.getByTestId('needs-you-count')).toHaveText(/7/)
    await expect(page.getByTestId('needs-you-card').filter({ hasText: 'Rebase Alpha PR?' })).toBeVisible()
    await expect(page.getByTestId('needs-you-card').filter({ hasText: 'Beta board ask' })).toBeVisible()
    await expect(page.getByTestId('needs-you-card').filter({ hasText: 'Legacy free-text block' })).toBeVisible()
    await page.screenshot({ path: path.join(shots, 'a-needs-you-column.png'), fullPage: true })
  })

  test('(b) tap Yes → card leaves Needs you; DB has gm_decision and ready', async () => {
    const page = fixture!.page
    const card = page.getByTestId('needs-you-card').filter({ hasText: 'Rebase Alpha PR?' })
    await expect(card).toBeVisible()
    await card.getByRole('button', { name: /Yes/i }).click()
    await expect(card).toHaveCount(0, { timeout: 15_000 })
    // counts drop
    await expect(page.getByTestId('needs-you-count')).toHaveText(/6/)

    // DB check via python one-shot
    const home = fixture!.sandbox.hermesHome
    const tid = seed!.ids.yes
    const py = process.env.PYTHON || 'python'
    const check = path.join(home, 'check-yes.py')
    fs.writeFileSync(
      check,
      `
import os, json, sys
from pathlib import Path
os.environ["HERMES_HOME"] = r"${home.replace(/\\/g, '/')}"
for _k in (
    "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_SESSION_SOURCE",
):
    os.environ.pop(_k, None)
sys.path.insert(0, r"${REPO_ROOT.replace(/\\/g, '/')}")
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
with kbc.connect_closing(board="alpha") as conn:
    t = kb.get_task(conn, "${tid}")
    dec = [e for e in kb.list_events(conn, "${tid}") if e.kind == "gm_decision"]
    print(json.dumps({"status": t.status if t else None, "decisions": len(dec)}))
`,
      'utf8',
    )

    const out = execFileSync(py, [check], {
      cwd: REPO_ROOT,
      env: (() => {
        const e: NodeJS.ProcessEnv = { ...process.env, HERMES_HOME: home, PYTHONPATH: REPO_ROOT }
        delete e.HERMES_DELEGATED_CHILD_CONTEXT
        delete e.HERMES_KANBAN_TASK

        return e
      })(),
      encoding: 'utf8',
    })

    const parsed = JSON.parse(out.trim().split('\n').pop()!) as { status: string; decisions: number }
    expect(parsed.status).toBe('ready')
    expect(parsed.decisions).toBe(1)
    await page.screenshot({ path: path.join(shots, 'b-after-yes.png'), fullPage: true })
  })

  test('(c) A/B pick with two attached images', async () => {
    const page = fixture!.page
    await closeDrawer(page)
    const card = page.getByTestId('needs-you-card').filter({ hasText: 'Pick mockup A or B' })
    await expect(card).toBeVisible()
    // open drawer for large images
    await card.getByTestId('needs-you-open').click()
    const bar = page.getByTestId('needs-you-drawer-bar')
    await expect(bar).toBeVisible()
    await expect(bar.locator('img')).toHaveCount(2, { timeout: 10_000 })
    await page.screenshot({ path: path.join(shots, 'c-drawer-ab.png'), fullPage: true })
    await bar.getByRole('button', { name: /Pick A/i }).first().click()
    await expect(page.getByTestId('needs-you-card').filter({ hasText: 'Pick mockup A or B' })).toHaveCount(0, {
      timeout: 15_000,
    })
    await closeDrawer(page)
  })

  test('(d) send back with a note', async () => {
    const page = fixture!.page
    await closeDrawer(page)
    const card = page.getByTestId('needs-you-card').filter({ hasText: 'Need more context' })
    await expect(card).toBeVisible()
    await card.getByRole('button', { name: /Send back with a note/i }).click()
    await card.getByPlaceholder(/What should the agent change/i).fill('need more screenshots')
    await card.getByRole('button', { name: /Send note/i }).click()
    await expect(card).toHaveCount(0, { timeout: 15_000 })
  })

  test('(e) Undo, then Undo refused after simulated claim', async () => {
    const page = fixture!.page
    await closeDrawer(page)
    const card = page.getByTestId('needs-you-card').filter({ hasText: 'Undo me' })
    await expect(card).toBeVisible()
    // Open drawer first so Undo stays on the decision bar after the answer.
    await card.getByTestId('needs-you-open').click()
    const bar = page.getByTestId('needs-you-drawer-bar')
    await expect(bar).toBeVisible()
    await bar.getByRole('button', { name: /Keep/i }).click()
    await expect(page.getByTestId('needs-you-card').filter({ hasText: 'Undo me' })).toHaveCount(0, {
      timeout: 15_000,
    })
    await expect(bar.getByRole('button', { name: /Undo/i })).toBeVisible({ timeout: 10_000 })
    await bar.getByRole('button', { name: /Undo/i }).click()
    // Card should return to Needs you
    await expect(page.getByTestId('needs-you-card').filter({ hasText: 'Undo me' })).toBeVisible({ timeout: 15_000 })
    await closeDrawer(page)

    // Answer again then simulate claim so undo is refused
    const card2 = page.getByTestId('needs-you-card').filter({ hasText: 'Undo me' })
    await card2.getByTestId('needs-you-open').click()
    const bar2 = page.getByTestId('needs-you-drawer-bar')
    await bar2.getByRole('button', { name: /Keep/i }).click()
    await expect(page.getByTestId('needs-you-card').filter({ hasText: 'Undo me' })).toHaveCount(0, {
      timeout: 15_000,
    })
    await expect(bar2.getByRole('button', { name: /Undo/i })).toBeVisible({ timeout: 10_000 })

    const home = fixture!.sandbox.hermesHome
    const tid = seed!.ids.undo
    const py = process.env.PYTHON || 'python'
    const claim = path.join(home, 'claim-undo.py')
    fs.writeFileSync(
      claim,
      `
import os, sys
os.environ["HERMES_HOME"] = r"${home.replace(/\\/g, '/')}"
for _k in (
    "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_SESSION_SOURCE",
):
    os.environ.pop(_k, None)
sys.path.insert(0, r"${REPO_ROOT.replace(/\\/g, '/')}")
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
with kbc.connect_closing(board="alpha") as conn:
    claimed = kb.claim_task(conn, "${tid}", claimer="worker")
    print("claimed", bool(claimed))
`,
      'utf8',
    )
    const claimEnv: NodeJS.ProcessEnv = { ...process.env, HERMES_HOME: home, PYTHONPATH: REPO_ROOT }
    delete claimEnv.HERMES_DELEGATED_CHILD_CONTEXT
    delete claimEnv.HERMES_KANBAN_TASK
    execFileSync(py, [claim], {
      cwd: REPO_ROOT,
      env: claimEnv,
      encoding: 'utf8',
    })

    allowErrorBanners()
    await bar2.getByRole('button', { name: /Undo/i }).click()
    // Refusal message surfaces (toast or inline on the decision bar)
    await expect(page.getByText(/already|picked up|running|claimed|cannot undo|refused|worker/i).first()).toBeVisible({
      timeout: 15_000,
    })
    await closeDrawer(page)
  })

  test('(f) legacy needs_input card — Send back clicks and unblocks', async () => {
    const page = fixture!.page
    const home = fixture!.sandbox.hermesHome
    await closeDrawer(page)
    // After (b–e) the Needs you column scrolls; legacy is often below the fold
    // under Fail path + Beta. Scroll it on-screen so the PNG proves AC5.
    const card = page.getByTestId('needs-you-card').filter({ hasText: 'Legacy free-text block' })
    await expect(card).toBeVisible()
    await card.scrollIntoViewIfNeeded()
    await expect(card.getByText(/Which branch should this land on/i)).toBeVisible()
    await expect(card.getByText(/No buttons: ask the agent to re-ask with options/i)).toBeVisible()
    await expect(card.getByRole('button', { name: /Send back with a note/i })).toBeVisible()
    // Card-scoped shot so title/reason/hint/Send back stay readable (not cropped).
    await card.screenshot({ path: path.join(shots, 'f-legacy-card.png') })

    // P1: Send back must land a comment + unblock (not 400 "no open GM ask").
    // NoteBox uses placeholder + "Send note" (same as structured send-back in (d)).
    await card.getByRole('button', { name: /Send back with a note/i }).click()
    await expect(card.getByPlaceholder(/What should the agent change/i)).toBeVisible()
    const sendNote = `e2e legacy send-back ${Date.now()}`
    await card.getByPlaceholder(/What should the agent change/i).fill(sendNote)
    await card.getByRole('button', { name: /Send note/i }).click()
    await expect(page.getByTestId('needs-you-card').filter({ hasText: 'Legacy free-text block' })).toHaveCount(0, {
      timeout: 15_000,
    })
    // Prove comment + ready on the alpha board DB (legacy is seeded there).
    const tid = seed!.ids.legacy
    const py = process.env.PYTHON || 'python'
    const check = path.join(home, 'legacy-sendback-check.py')
    fs.writeFileSync(
      check,
      `
import os, sys, json
os.environ["HERMES_HOME"] = r"${home.replace(/\\/g, '/')}"
for _k in (
    "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_SESSION_SOURCE",
):
    os.environ.pop(_k, None)
sys.path.insert(0, r"${REPO_ROOT.replace(/\\/g, '/')}")
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
with kbc.connect_closing(board="alpha") as conn:
    task = kb.get_task(conn, "${tid}")
    bodies = [c.body for c in kb.list_comments(conn, "${tid}")]
    print(json.dumps({
        "status": None if task is None else task.status,
        "bodies": bodies,
    }))
`,
      'utf8',
    )

    const legacyEnv: NodeJS.ProcessEnv = { ...process.env, HERMES_HOME: home, PYTHONPATH: REPO_ROOT }
    delete legacyEnv.HERMES_DELEGATED_CHILD_CONTEXT
    delete legacyEnv.HERMES_KANBAN_TASK

    const out = execFileSync(py, [check], {
      cwd: REPO_ROOT,
      env: legacyEnv,
      encoding: 'utf8',
    }).trim()

    const parsed = JSON.parse(out.split('\n').filter(Boolean).pop()!) as {
      bodies: string[]
      status: string | null
    }

    expect(parsed.status).toBe('ready')
    expect(parsed.bodies.some(b => b.includes(sendNote))).toBe(true)
  })

  test('(g) POST fail → rollback', async () => {
    const page = fixture!.page
    await closeDrawer(page)
    allowErrorBanners()
    // contextBridge freezes hermesDesktop.api — use the app e2e seam instead.
    await page.evaluate(() => {
      ;(window as unknown as { __HERMES_E2E_FAIL_ANSWER?: boolean }).__HERMES_E2E_FAIL_ANSWER = true
    })
    const card = page.getByTestId('needs-you-card').filter({ hasText: 'Fail path card' })
    await expect(card).toBeVisible()
    await card.getByRole('button', { name: /Yes/i }).click()
    // Optimistic leave then rollback on error — card returns
    await expect(page.getByTestId('needs-you-card').filter({ hasText: 'Fail path card' })).toBeVisible({
      timeout: 15_000,
    })
    await page.evaluate(() => {
      ;(window as unknown as { __HERMES_E2E_FAIL_ANSWER?: boolean }).__HERMES_E2E_FAIL_ANSWER = false
    })
  })
})
