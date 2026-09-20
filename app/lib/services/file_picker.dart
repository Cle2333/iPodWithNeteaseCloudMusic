/// 选文件：用系统自带的原生对话框。
///
/// 为什么不用 `file_selector` / `file_picker` 这类包：它们是**原生插件**，
/// 而 Flutter 在 Windows 上构建插件需要系统开启「开发者模式」
/// （插件构建要建符号链接）。开发者模式得管理员权限才能开，
/// 为了选个文件去要权限不值得。
///
/// PowerShell 的 `System.Windows.Forms.OpenFileDialog` 是系统自带的，
/// 什么额外前提都不用。代价是要起一个 PowerShell 进程（约 1 秒），
/// 对"用户点一下选文件"这个场景完全够用。
library;

import 'dart:convert';
import 'dart:io';

/// 音频扩展名。和后端能认的格式保持一致。
const List<String> kAudioExtensions = <String>[
  'mp3',
  'm4a',
  'aac',
  'flac',
  'wav',
  'ogg',
  'wma',
  'aiff',
  'alac',
];

class FilePickerService {
  /// 弹出原生多选对话框，返回选中的文件路径（取消则返回空列表）。
  Future<List<String>> pickAudioFiles() async {
    if (!Platform.isWindows) {
      throw const FilePickException('目前只支持在 Windows 上选文件');
    }

    final script = File(
      '${Directory.systemTemp.path}${Platform.pathSeparator}'
      'ipod_manager_pick_${DateTime.now().microsecondsSinceEpoch}.ps1',
    );

    const body = r'''
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Multiselect = $true
$dialog.Title = '选择要导入 iPod 的音乐文件'
$dialog.Filter = '音频文件|*.mp3;*.m4a;*.aac;*.flac;*.wav;*.ogg;*.wma;*.aiff|所有文件|*.*'
$dialog.RestoreDirectory = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  foreach ($name in $dialog.FileNames) { Write-Output $name }
}
''';

    try {
      // **带 BOM 写**：Windows PowerShell 5.1 按 ANSI 读 .ps1，
      // 脚本里的中文会把引号配对弄坏，直接语法错误。BOM 是唯一可靠的标记。
      await script.writeAsString('\uFEFF$body', encoding: utf8);

      final result = await Process.run('powershell.exe', <String>[
        '-NoProfile',
        '-STA', // 窗体对话框必须是 STA 线程，不加会直接抛
        '-ExecutionPolicy', 'Bypass',
        '-File', script.path,
      ], runInShell: false);

      if (result.exitCode != 0) {
        throw FilePickException('打开文件对话框失败：${_tail(result.stderr.toString())}');
      }

      return result.stdout
          .toString()
          .split(RegExp(r'\r?\n'))
          .map((line) => line.trim())
          .where((line) => line.isNotEmpty)
          .toList();
    } finally {
      // 临时脚本不该留在 temp 里
      try {
        if (await script.exists()) await script.delete();
      } catch (_) {
        // 删不掉无所谓，系统会清 temp
      }
    }
  }

  static String _tail(String text) {
    final trimmed = text.trim();
    if (trimmed.isEmpty) return '（没有错误输出）';
    final lines = trimmed.split(RegExp(r'\r?\n'));
    return lines.length <= 3
        ? trimmed
        : lines.sublist(lines.length - 3).join(' ');
  }
}

class FilePickException implements Exception {
  const FilePickException(this.message);

  final String message;

  @override
  String toString() => message;
}
