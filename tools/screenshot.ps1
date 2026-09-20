
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System.Runtime.InteropServices;
public class DpiHelper {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
"@
# Without this the capture is virtualized to the DPI-unaware size (e.g. 1707x1067
# on a 150%-scaled 2560x1600 screen) and clips the right/bottom of the desktop.
[void][DpiHelper]::SetProcessDPIAware()

$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$out = Join-Path $PSScriptRoot "..\.hermes\screenshot.png"
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$gfx.Dispose()
$bmp.Dispose()
Write-Output ("saved: " + $out + " (" + $bounds.Width + "x" + $bounds.Height + ")")
