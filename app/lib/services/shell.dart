/// 交给 Windows 资源管理器做的事。
///
/// 单独放一个文件是因为这个方法里有几处**只能靠试出来**的坑
/// （PowerShell 的引号嵌套、explorer 对逗号的解析），
/// 散在页面里的话每处都得重新踩一遍。
library;

import 'dart:io';

/// 在资源管理器里**定位**到某个文件（选中它）。
///
/// 用 PowerShell 的 `Start-Process -ArgumentList` 而不是直接调 explorer：
/// explorer 对 `/select,` 后面带空格的路径很容易解析错，而导出目录、
/// 缓存目录恰恰可能带空格（"我的文档"之类）。
///
/// 失败不抛异常——这只是个便利功能，打不开文件夹不该让整个操作报错。
/// 返回值表示有没有把命令发出去。
Future<bool> revealInExplorer(String path) async {
  // 这个 Dart 字符串用单引号包：里面要嵌 PowerShell 的单引号引用
  final script =
      'Start-Process -FilePath explorer.exe -ArgumentList \'/select,"$path"\'';
  try {
    await Process.run('powershell.exe', <String>[
      '-NoProfile',
      '-Command',
      script,
    ]);
    return true;
  } catch (_) {
    return false;
  }
}

/// 打开一个文件夹（不选中具体文件）。
Future<bool> openFolder(String path) async {
  try {
    await Process.run('explorer.exe', <String>[path]);
    return true;
  } catch (_) {
    return false;
  }
}
