import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test } from 'vitest'

import { $desktopBoot } from '@/store/boot'

import { BootFailureOverlay } from './boot-failure-overlay'

afterEach(() => {
  cleanup()
  $desktopBoot.set({
    error: null,
    fakeMode: false,
    message: 'idle',
    phase: 'renderer.init',
    progress: 0,
    running: false,
    timestamp: Date.now(),
    visible: false
  })
})

test('probe timeout shows the still-starting retry state, not first-run setup', () => {
  $desktopBoot.set({
    error: 'Hermes is still starting (probe took 22 s)',
    fakeMode: false,
    message: 'Hermes is still starting (probe took 22 s)',
    phase: 'renderer.error',
    progress: 20,
    running: false,
    timestamp: Date.now(),
    visible: true
  })

  render(<BootFailureOverlay />)

  expect(screen.getByRole('dialog').querySelector('[data-still-starting="true"]')).not.toBeNull()
  expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
  expect(screen.queryByText(/Connect to existing Hermes/i)).toBeNull()
  expect(screen.getAllByText(/Hermes is still starting \(probe took 22 s\)/).length).toBeGreaterThan(0)
})
