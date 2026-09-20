/// 显示用的格式化。
///
/// 单独放一个文件是因为 `api/` 和 `pages/` 都要用——放在 `widgets/common.dart`
/// 里的话，数据模型就得反过来依赖界面层。
library;

/// 字节数 → 人类可读。**十进制单位**（1 KB = 1000 B）。
///
/// 跟引擎的 `discovery.human_size` 保持一致：同一个文件在"缓存"页显示
/// 7.0 MB、在"本地已下载"显示 6.7 MB，用户只会觉得哪里坏了。
/// 十进制也是厂商标称容量的用法。
String humanSize(int bytes) {
  if (bytes <= 0) return '0 B';
  var size = bytes.toDouble();
  for (final unit in const <String>['B', 'KB', 'MB', 'GB', 'TB']) {
    if (size < 1000 || unit == 'TB') {
      return unit == 'B'
          ? '${size.toInt()} $unit'
          : '${size.toStringAsFixed(1)} $unit';
    }
    size /= 1000;
  }
  return '${size.toStringAsFixed(1)} TB';
}
