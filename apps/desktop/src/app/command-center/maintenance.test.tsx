import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { getActionStatus, runCarryMerge } from '@/hermes'
import { $desktopActionTasks } from '@/store/activity'
import { confirm } from '@/store/confirm'
import { $notifications } from '@/store/notifications'

import { MaintenancePanel } from './maintenance'

// The backend spawns each op under one fixed action name ('doctor', 'security-audit',
// 'backup', 'curator-run'), a re-spawn replaces the record under that name, and
// /api/actions/<name>/status reports the latest run. The fake keeps that contract.
const runs: Record<string, number> = {}
const running: Record<string, boolean> = {}
const exitCodes: Record<string, number> = {}
let nextRunStaysRunning = false

function spawn(name: string) {
  runs[name] = (runs[name] ?? 0) + 1
  running[name] = nextRunStaysRunning

  return Promise.resolve({ name, ok: true, pid: 1000 + runs[name] })
}

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  getActionStatus: vi.fn(async (name: string) => ({
    exit_code: running[name] ? null : (exitCodes[name] ?? 0),
    lines: exitCodes[name]
      ? [`FAILED: ${name} exited ${exitCodes[name]}`]
      : [`${name} run ${runs[name]} output`],
    name,
    pid: 1000 + runs[name],
    running: running[name]
  })),
  getCarryMergePlan: vi.fn(async () => ({
    branch: 'force/carry',
    checks_out_another_branch: false,
    commands: [
      'git -C S:/ForceAgents/live-install fetch fork force/carry',
      'git -C S:/ForceAgents/live-install merge --no-edit fork/force/carry',
      'python -m pm.cli install'
    ],
    remote: 'fork',
    repo: 'S:/ForceAgents/live-install'
  })),
  getCuratorStatus: vi.fn(() => new Promise(() => {})),
  getMemoryStatus: vi.fn(() => new Promise(() => {})),
  runDoctor: vi.fn(() => spawn('doctor')),
  runSecurityAudit: vi.fn(() => spawn('security-audit')),
  runCarryMerge: vi.fn(() => spawn('carry-merge'))
}))

vi.mock('@/store/confirm', () => ({
  confirm: vi.fn(async () => false)
}))

beforeEach(() => {
  for (const key of Object.keys(runs)) {
    delete runs[key]
    delete running[key]
  }

  for (const key of Object.keys(exitCodes)) {
    delete exitCodes[key]
  }

  nextRunStaysRunning = false
  $desktopActionTasks.set({})
  $notifications.set([])
  vi.mocked(confirm).mockResolvedValue(false)
  vi.mocked(getActionStatus).mockClear()
})

afterEach(cleanup)

const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement

describe('MaintenancePanel action tail', () => {
  it('tails a second run of the same op', async () => {
    render(<MaintenancePanel />)

    await act(async () => void fireEvent.click(button('Run doctor')))
    await screen.findByText('doctor run 1 output')

    nextRunStaysRunning = true
    await act(async () => void fireEvent.click(button('Run doctor')))

    await screen.findByText('doctor run 2 output')
    expect(vi.mocked(getActionStatus)).toHaveBeenCalledTimes(2)
    expect(screen.getByText('Running...')).toBeTruthy()
    expect(button('Run doctor').disabled).toBe(true)
    expect($desktopActionTasks.get().doctor?.status).toMatchObject({ pid: 1002, running: true })
  })

  it('tails a different op launched after the first', async () => {
    render(<MaintenancePanel />)

    await act(async () => void fireEvent.click(button('Run doctor')))
    await screen.findByText('doctor run 1 output')

    nextRunStaysRunning = true
    await act(async () => void fireEvent.click(button('Security audit')))

    await screen.findByText('security-audit run 1 output')
    expect(vi.mocked(getActionStatus)).toHaveBeenLastCalledWith('security-audit', 200)
    expect(button('Security audit').disabled).toBe(true)
  })
})

const carryPlan = [
  'git -C S:/ForceAgents/live-install fetch fork force/carry',
  'git -C S:/ForceAgents/live-install merge --no-edit fork/force/carry',
  'python -m pm.cli install'
]

describe('Force carry merge', () => {
  it('names the fork fetch, merge, and rebuild before it runs', async () => {
    render(<MaintenancePanel />)

    await act(async () => void fireEvent.click(button('Merge fork force/carry')))

    expect(vi.mocked(confirm)).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Merge the fork and rebuild?',
        details: [
          { label: 'Install', value: 'S:/ForceAgents/live-install' },
          { label: 'Fetch', value: carryPlan[0] },
          { label: 'Merge', value: carryPlan[1] },
          { label: 'Rebuild', value: `${carryPlan[2]} (cwd S:/ForceAgents/live-install)` }
        ]
      })
    )
    expect(vi.mocked(runCarryMerge)).not.toHaveBeenCalled()
  })

  it('does not report success while the rebuild is still running', async () => {
    vi.mocked(confirm).mockResolvedValue(true)
    nextRunStaysRunning = true
    render(<MaintenancePanel />)

    await act(async () => void fireEvent.click(button('Merge fork force/carry')))

    await screen.findByText('carry-merge run 1 output')
    expect(
      $notifications.get().some((item: { title?: string }) => item.title === 'Force carry merge finished')
    ).toBe(false)
  })

  it('surfaces a failed rebuild and never calls it finished', async () => {
    vi.mocked(confirm).mockResolvedValue(true)
    exitCodes['carry-merge'] = 2
    render(<MaintenancePanel />)

    await act(async () => void fireEvent.click(button('Merge fork force/carry')))

    expect(
      (await screen.findAllByText('FAILED: carry-merge exited 2')).length
    ).toBeGreaterThan(1)
    expect(
      $notifications.get().some((item: { title?: string }) => item.title === 'Force carry merge finished')
    ).toBe(false)
    expect(
      $notifications.get().some((item: { title?: string }) => item.title === 'Force carry merge failed')
    ).toBe(true)
  })
})
