# 配置 Flutter 环境变量（用户级，持久生效）
#
# 1) 国内镜像：不配的话 pub.dev 和 flutter artifacts 下载会极慢甚至超时
# 2) 把 C:\flutter\bin 加进用户 PATH
#
# PATH 是"读出来再追加"，不是整体覆盖——直接覆盖会把用户原有 PATH 全冲掉，
# 那是灾难级的。
#
# 注意：本文件必须带 UTF-8 BOM。Windows PowerShell 5.1 读 .ps1 时默认按
# ANSI 解码，没有 BOM 的话中文会被解成乱码并破坏引号配对，直接语法错误。

$ErrorActionPreference = 'Stop'

$mirror = @{
    'PUB_HOSTED_URL'           = 'https://pub.flutter-io.cn'
    'FLUTTER_STORAGE_BASE_URL' = 'https://storage.flutter-io.cn'
}

foreach ($kv in $mirror.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($kv.Key, $kv.Value, 'User')
    Write-Output ("  [环境变量] {0} = {1}" -f $kv.Key, $kv.Value)
}

$flutterBin = 'C:\flutter\bin'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($null -eq $userPath) { $userPath = '' }

Write-Output ("  [PATH] 修改前: {0}" -f $userPath)

$parts = @($userPath -split ';' | Where-Object { $_ -ne '' })
if ($parts -contains $flutterBin) {
    Write-Output ("  [PATH] 已包含 {0}，不重复添加" -f $flutterBin)
} else {
    $newPath = (@($parts) + $flutterBin) -join ';'
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    Write-Output ("  [PATH] 修改后: {0}" -f $newPath)
}

Write-Output ''
Write-Output '完成。新开的终端里 flutter 就能直接用了。'
