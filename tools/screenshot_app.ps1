
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
}
"@
[void][Win]::SetProcessDPIAware()

$p = Get-Process -Name ipod_manager -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $p) { Write-Output "NO_APP_WINDOW"; exit 1 }

$h = $p.MainWindowHandle
if ($h -eq 0) { Write-Output "NO_MAIN_WINDOW"; exit 1 }

if ([Win]::IsIconic($h)) { [void][Win]::ShowWindow($h, 9) }   # SW_RESTORE
[void][Win]::ShowWindow($h, 5)                                 # SW_SHOW
[void][Win]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 900
[void][Win]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 600

$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$out = Join-Path $PSScriptRoot "..\.hermes\screenshot.png"
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output ("focused + saved " + $b.Width + "x" + $b.Height)
