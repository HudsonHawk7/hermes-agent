/**
 * Who does this process belong to? On Windows the only honest answer is its environment.
 *
 * `/proc` does not exist there, and a sandbox gateway's command line is just
 * `hermes_cli.main --profile default gateway run --quiet` — no home path anywhere in it. The
 * `HERMES_HOME` a process actually runs against lives in its own environment block, which is what
 * this module reads: NtQueryInformationProcess for the PEB, then the process parameters'
 * environment pointer, then the UTF-16 block itself.
 *
 * Nothing here signals or kills anything. The census in `process-census.ts` decides.
 */
import { spawnSync } from 'node:child_process'
import path from 'node:path'

/** `HERMES_HOME` per pid, read from each process's own environment block. Windows only. */
export function windowsProcessHomes(pids: number[]): Map<number, string> {
  const homes = new Map<number, string>()

  if (process.platform !== 'win32' || pids.length === 0) {
    return homes
  }

  const targets = pids.filter(pid => Number.isInteger(pid) && pid > 0)

  if (targets.length === 0) {
    return homes
  }

  const script = WINDOWS_ENV_SCRIPT.replace('__TARGETS__', targets.join(', '))
  const encoded = Buffer.from(script, 'utf16le').toString('base64')

  const result = spawnSync(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
    { encoding: 'utf8', timeout: 120_000, windowsHide: true, maxBuffer: 4 * 1024 * 1024 }
  )

  for (const line of String(result.stdout ?? '').split(/\r?\n/)) {
    const [pid, home] = line.split('\t')

    if (!/^\d+$/.test(pid ?? '') || !home) {
      continue
    }

    homes.set(Number(pid), home.trim())
  }

  return homes
}

/** Same location on this host (case-insensitive on Windows, resolved on both). */
export function samePath(left: string, right: string): boolean {
  const normalize = (value: string) => {
    try {
      return path.resolve(value)
    } catch {
      return value
    }
  }

  const [a, b] = [normalize(left), normalize(right)]

  return process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b
}

/** True when `child` is `root` itself or sits underneath it (never a sibling with a longer name). */
export function pathUnder(child: string, root: string): boolean {
  const resolve = (value: string) => {
    try {
      return path.resolve(value)
    } catch {
      return value
    }
  }

  const [candidate, base] = [resolve(child), resolve(root)]
  const [a, b] = process.platform === 'win32'
    ? [candidate.toLowerCase(), base.toLowerCase()]
    : [candidate, base]

  return a === b || a.startsWith(b.endsWith(path.sep) ? b : b + path.sep)
}

const WINDOWS_ENV_SCRIPT = `$ErrorActionPreference = 'Stop'
$source = @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class HermesEnvHome
{
    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_BASIC_INFORMATION
    {
        public IntPtr Reserved1;
        public IntPtr PebBaseAddress;
        public IntPtr Reserved2_0;
        public IntPtr Reserved2_1;
        public IntPtr UniqueProcessId;
        public IntPtr Reserved3;
    }

    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(IntPtr handle, int infoClass, ref PROCESS_BASIC_INFORMATION info, int length, out int returned);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(int access, bool inherit, int pid);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ReadProcessMemory(IntPtr handle, IntPtr address, byte[] buffer, int size, out IntPtr read);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    public static string Home(int pid)
    {
        // PROCESS_QUERY_INFORMATION | PROCESS_VM_READ. Another user's process, a protected one or
        // one that exited between the listing and this call simply has no answer.
        IntPtr handle = OpenProcess(0x0410, false, pid);

        if (handle == IntPtr.Zero)
        {
            return "";
        }

        try
        {
            PROCESS_BASIC_INFORMATION info = new PROCESS_BASIC_INFORMATION();
            int returned;

            if (NtQueryInformationProcess(handle, 0, ref info, Marshal.SizeOf(info), out returned) != 0)
            {
                return "";
            }

            byte[] pointer = new byte[8];
            IntPtr read;

            if (!ReadProcessMemory(handle, (IntPtr)((long)info.PebBaseAddress + 0x20), pointer, 8, out read))
            {
                return "";
            }

            long parameters = BitConverter.ToInt64(pointer, 0);
            byte[] environment = new byte[8];

            if (!ReadProcessMemory(handle, (IntPtr)(parameters + 0x80), environment, 8, out read))
            {
                return "";
            }

            long start = BitConverter.ToInt64(environment, 0);

            if (start == 0)
            {
                return "";
            }

            StringBuilder block = new StringBuilder(65536);
            byte[] chunk = new byte[4096];

            // Windows stores no readable length here, so read until the pages stop being readable.
            for (int index = 0; index < 64; index++)
            {
                if (!ReadProcessMemory(handle, (IntPtr)(start + index * 4096), chunk, chunk.Length, out read))
                {
                    break;
                }

                block.Append(Encoding.Unicode.GetString(chunk));
            }

            string joined = block.ToString();
            int found = joined.IndexOf("HERMES_HOME=", StringComparison.OrdinalIgnoreCase);

            if (found < 0)
            {
                return "";
            }

            int value = found + "HERMES_HOME=".Length;
            int end = joined.IndexOf((char)0, value);

            return joined.Substring(value, (end < 0 ? joined.Length : end) - value).Trim();
        }
        catch (Exception)
        {
            return "";
        }
        finally
        {
            CloseHandle(handle);
        }
    }
}
"@

Add-Type -TypeDefinition $source -Language CSharp

$targets = @(__TARGETS__)

foreach ($target in $targets) {
    "{0}{1}{2}" -f $target, [char]9, [HermesEnvHome]::Home([int]$target)
}
`
