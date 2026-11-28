import assert from 'node:assert/strict'
import test from 'node:test'
import { commandLineIsHermesRuntime, hermesProcessProbe } from './hermes-process'

test('commandLineIsHermesRuntime matches only the hermes executable token', () => {
  assert.equal(commandLineIsHermesRuntime('/usr/local/bin/hermes chat'), true)
  assert.equal(commandLineIsHermesRuntime('hermes.exe --profile default'), true)
  assert.equal(commandLineIsHermesRuntime('C:\\Hermes\\hermes.cmd gateway'), true)

  assert.equal(commandLineIsHermesRuntime('ps aux | grep hermes'), false)
  assert.equal(commandLineIsHermesRuntime('tail -f /tmp/hermes.log'), false)
  assert.equal(commandLineIsHermesRuntime('/usr/local/bin/hermes-dashboard'), false)
  assert.equal(commandLineIsHermesRuntime('node /opt/hermes-data/server.js'), false)
  assert.equal(commandLineIsHermesRuntime(''), false)
})

test('windows probe reads the process command line through CIM, not ps', () => {
  const spec = hermesProcessProbe('win32', 4242)
  assert.equal(spec?.command, 'powershell.exe')
  assert.match(spec?.args.join(' ') ?? '', /ProcessId=4242/)
  assert.match(spec?.args.join(' ') ?? '', /Win32_Process/)
  assert.equal(hermesProcessProbe('linux', 4242), null)
  assert.equal(hermesProcessProbe('win32', 0), null)
})
