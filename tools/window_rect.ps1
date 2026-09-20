Add-Type -AssemblyName System.Windows.Forms

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
}
"@
$procs = Get-Process -Name ipod_manager -ErrorAction SilentlyContinue
if (-not $procs) { Write-Output "NO_PROCESS"; exit }
foreach ($p in $procs) {
  $h = $p.MainWindowHandle
  if ($h -eq 0) { continue }
  $r = New-Object W+RECT
  [void][W]::GetWindowRect($h, [ref]$r)
  $w = $r.R - $r.L
  $hgt = $r.B - $r.T
  Write-Output ("window rect: L=" + $r.L + " T=" + $r.T + " R=" + $r.R + " B=" + $r.B)
  Write-Output ("window size: " + $w + "x" + $hgt)
  $s = [System.Windows.Forms.Screen]::PrimaryScreen
  Write-Output ("screen     : " + $s.Bounds.Width + "x" + $s.Bounds.Height)
  if ($r.R -gt $s.Bounds.Width) { Write-Output "OVERFLOW_RIGHT" } else { Write-Output "FITS_HORIZONTAL" }
}
