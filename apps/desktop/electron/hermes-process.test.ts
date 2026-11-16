import assert from 'node:assert/strict'

import { test } from 'vitest'

import { commandLineIsHermes, hermesProcessProbe } from './hermes-process'

test('a hermes command line matches, and a recycled pid does not', () => {
  assert.equal(commandLineIsHermes('python.exe C:\Users\judso\AppData\Local\hermes\hermes-agent\gateway.py'), true)
  assert.equal(commandLineIsHermes('notepad.exe'), false)
  assert.equal(commandLineIsHermes(''), false)
  assert.equal(commandLineIsHermes(null), false)
})

test('Windows probes the process command line because ps and /proc are absent', () => {
  const spec = hermesProcessProbe('win32', 4242)

  assert.equal(spec?.command, 'powershell.exe')
  assert.deepEqual(spec?.args.slice(0, 3), ['-NoProfile', '-NonInteractive', '-Command'])
  assert.match(spec?.args[3] ?? '', /Get-CimInstance Win32_Process/)
  assert.match(spec?.args[3] ?? '', /ProcessId = 4242/)
  assert.equal(hermesProcessProbe('win32', 0), null)
  assert.equal(hermesProcessProbe('win32', 1.5), null)
})

test('other platforms probe with ps args, not a shell string', () => {
  assert.deepEqual(hermesProcessProbe('darwin', 7), { command: 'ps', args: ['-p', '7', '-o', 'args='] })
  assert.deepEqual(hermesProcessProbe('linux', 7), { command: 'ps', args: ['-p', '7', '-o', 'args='] })
})
