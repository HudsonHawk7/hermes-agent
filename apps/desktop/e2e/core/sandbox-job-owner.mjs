import { spawn } from 'node:child_process'
import fs from 'node:fs'

const [python, holder, ready, pidFile] = process.argv.slice(2)
const pyLauncher = /(^|[\\/])py(\.exe)?$/i.test(python)
const holderProc = spawn(
  python,
  pyLauncher ? ['-3', holder, String(process.pid), ready] : [holder, String(process.pid), ready],
  { stdio: 'ignore', windowsHide: true }
)
holderProc.unref()

const deadline = Date.now() + 8_000
while (!fs.existsSync(ready) && Date.now() < deadline) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 50)
}

const status = fs.existsSync(ready) ? fs.readFileSync(ready, 'utf8').trim() : 'missing'
if (status !== 'ok') {
  process.stderr.write(`holder ${status}\n`)
  process.exit(2)
}

const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], {
  stdio: 'ignore',
  windowsHide: true,
})
fs.writeFileSync(pidFile, String(child.pid))
setInterval(() => {}, 1000)
