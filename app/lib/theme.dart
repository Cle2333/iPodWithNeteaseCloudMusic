/// 主题：字体、配色。
///
/// 字体用**微软雅黑**：中文界面里它最稳，字重齐全，不会出现
/// 某些开源中文字体在小字号下发虚、或者 CJK 被系统 fallback 成宋体的情况。
/// 日志区另用等宽（Consolas）。
library;

import 'package:flutter/material.dart';

/// 界面正文字体。
const String kFontFamily = 'Microsoft YaHei';

/// 字体回退链。写多一层是因为不同 Windows 版本上雅黑的全名有两种写法
/// （"Microsoft YaHei" / "Microsoft YaHei UI"），漏了就 fallback 到默认字体。
const List<String> kFontFallback = <String>[
  'Microsoft YaHei',
  'Microsoft YaHei UI',
  'Segoe UI',
];

/// 日志 / 代码区的等宽字体。
const List<String> kMonoFontFallback = <String>[
  'Consolas',
  'Cascadia Mono',
  'Courier New',
];

/// 状态色的语义：绿=正常 黄=警告 红=故障 灰=未连接。
class StatusColors {
  static const Color ok = Color(0xFF2E7D32);
  static const Color warn = Color(0xFFE65100);
  static const Color error = Color(0xFFC62828);
  static const Color idle = Color(0xFF9E9E9E);
  static const Color info = Color(0xFF1565C0);
}

ThemeData buildTheme() {
  final base = ThemeData(
    useMaterial3: true,
    colorScheme: ColorScheme.fromSeed(
      seedColor: const Color(0xFF1565C0),
      brightness: Brightness.light,
    ),
  );

  return base.copyWith(
    textTheme: base.textTheme.apply(
      fontFamily: kFontFamily,
      fontFamilyFallback: kFontFallback,
    ),
    // 侧边导航：选中项底色浅一点，别用默认那种饱和色，配中文看着吵
    navigationRailTheme: NavigationRailThemeData(
      backgroundColor: base.colorScheme.surfaceContainerLowest,
      indicatorColor: base.colorScheme.primaryContainer.withValues(alpha: 0.6),
      selectedIconTheme: IconThemeData(color: base.colorScheme.primary),
      unselectedIconTheme: IconThemeData(
        color: base.colorScheme.onSurfaceVariant,
      ),
    ),
    cardTheme: CardThemeData(
      elevation: 0,
      margin: EdgeInsets.zero,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(10),
        side: BorderSide(color: base.colorScheme.outlineVariant),
      ),
    ),
    dividerTheme: DividerThemeData(
      color: base.colorScheme.outlineVariant,
      space: 1,
      thickness: 1,
    ),
  );
}

/// 等宽文本样式（日志区用）。
TextStyle monoStyle({
  double size = 12.5,
  Color? color,
  FontWeight weight = FontWeight.normal,
}) {
  return TextStyle(
    fontFamily: 'Consolas',
    fontFamilyFallback: kMonoFontFallback,
    fontSize: size,
    height: 1.45,
    color: color,
    fontWeight: weight,
  );
}
