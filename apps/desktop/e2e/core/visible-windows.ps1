param(
  [Parameter(Mandatory = $true)]
  [string] $ProcessIds
)

$ids = @($ProcessIds.Split(',') | ForEach-Object { [int]$_.Trim() } | Where-Object { $_ -gt 0 })

if (-not ('HermesVisWins' -as [type])) {
  Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class HermesVisWins {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int maxCount);
  [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
  public static string Dump(int[] pids) {
    var want = new HashSet<uint>();
    foreach (var p in pids) want.Add((uint)p);
    var rows = new List<string>();
    EnumWindows((h, l) => {
      uint pid;
      GetWindowThreadProcessId(h, out pid);
      if (want.Contains(pid) && IsWindowVisible(h) && !IsIconic(h)) {
        int len = GetWindowTextLength(h);
        var sb = new StringBuilder(Math.Max(1, len + 1));
        GetWindowText(h, sb, sb.Capacity);
        rows.Add(((long)h).ToString() + "\t" + pid.ToString() + "\t" + sb.ToString());
      }
      return true;
    }, IntPtr.Zero);
    return string.Join("\n", rows);
  }
}
"@
}

[HermesVisWins]::Dump($ids)
