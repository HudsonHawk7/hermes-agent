// How Desktop decides a pid is a Hermes process. Extracted so Windows can be
// tested without Electron. /proc is not available on Windows, and `ps` is not
// either, so the fallback there is a CIM command-line query.

export function commandLineIsHermes(cmdline: string | null | undefined): boolean {
  return typeof cmdline === 'string' && cmdline.toLowerCase().includes('hermes')
}

export function hermesProcessProbe(
  platform: string,
  pid: number
): { command: string; args: string[] } | null {
  if (!Number.isInteger(pid) || pid <= 0) {
    return null
  }

  if (platform === 'win32') {
    return {
      command: 'powershell.exe',
      args: [
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        `(Get-CimInstance Win32_Process -Filter 'ProcessId = ${pid}').CommandLine`
      ]
    }
  }

  return { command: 'ps', args: ['-p', String(pid), '-o', 'args='] }
}
