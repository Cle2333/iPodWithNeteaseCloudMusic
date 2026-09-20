/// 列表行的右键菜单。
///
/// 三个列表（网易云歌单曲目 / 本地已下载 / iPod 音乐管理）里的每一行都能
/// 右键，弹出跟当前上下文相关的操作。**同一个操作在右键菜单和页面按钮上
/// 用同一个入口**——菜单不做页面按钮做不到的事，它只是让手不用跑那么远。
///
/// 几条约定：
///
/// * 不可用的项**留在菜单里但置灰**，并把原因写在下面。直接不显示的话，
///   用户会以为"这个功能没有"，而不是"现在还不能用"。
/// * 删除类的项标红。右键菜单最容易误点，颜色是最后一道提示。
/// * 危险操作**仍然要弹确认框**，不能因为"在菜单里点了一次"就算确认过。
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../theme.dart';

/// 右键菜单里的一项。
class SongAction {
  const SongAction({
    required this.label,
    this.icon,
    this.onTap,
    this.unavailableReason,
    this.danger = false,
    this.dividerBefore = false,
  });

  final String label;
  final IconData? icon;

  /// 点下去干什么。为 null = 不可用（会置灰）。
  final VoidCallback? onTap;

  /// 不可用的原因，显示在标签下面。
  ///
  /// 不给的话，用户只看到一个灰掉的项，不知道是为什么。
  final String? unavailableReason;

  /// 危险操作（删除类），标红。
  final bool danger;

  /// 在这一项之前画一条分隔线，用来分组。
  final bool dividerBefore;

  bool get enabled => onTap != null;
}

/// 在 [globalPosition] 处弹出菜单。
Future<void> showSongMenu(
  BuildContext context,
  Offset globalPosition,
  List<SongAction> actions,
) async {
  if (actions.isEmpty) return;

  final overlay = Overlay.of(context).context.findRenderObject() as RenderBox?;
  if (overlay == null) return;

  final scheme = Theme.of(context).colorScheme;

  final entries = <PopupMenuEntry<int>>[];
  for (var i = 0; i < actions.length; i++) {
    final action = actions[i];
    if (action.dividerBefore) {
      entries.add(const PopupMenuDivider(height: 8));
    }
    final color = action.danger
        ? (action.enabled ? scheme.error : scheme.error.withValues(alpha: 0.4))
        : (action.enabled ? scheme.onSurface : scheme.onSurfaceVariant);

    entries.add(
      PopupMenuItem<int>(
        value: i,
        enabled: action.enabled,
        height: action.unavailableReason == null ? 38 : 50,
        child: Row(
          children: <Widget>[
            if (action.icon != null) ...<Widget>[
              Icon(action.icon, size: 16, color: color),
              const SizedBox(width: 10),
            ],
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  Text(
                    action.label,
                    style: TextStyle(fontSize: 13, color: color),
                    overflow: TextOverflow.ellipsis,
                  ),
                  if (action.unavailableReason != null)
                    Text(
                      action.unavailableReason!,
                      style: TextStyle(
                        fontSize: 11,
                        color: scheme.onSurfaceVariant.withValues(alpha: 0.75),
                      ),
                      overflow: TextOverflow.ellipsis,
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  final picked = await showMenu<int>(
    context: context,
    position: RelativeRect.fromRect(
      globalPosition & const Size(1, 1),
      Offset.zero & overlay.size,
    ),
    items: entries,
  );

  if (picked == null) return;
  actions[picked].onTap?.call();
}

/// 让 [child] 支持右键弹出菜单。
///
/// [actions] 是个回调而不是列表：菜单内容经常依赖"这一行现在什么状态"，
/// 在右键的那一刻现算最准（列表可能刚被刷新过）。
class ContextMenuRegion extends StatelessWidget {
  const ContextMenuRegion({
    super.key,
    required this.actions,
    required this.child,
  });

  final List<SongAction> Function() actions;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      behavior: HitTestBehavior.translucent,
      onSecondaryTapDown: (details) =>
          showSongMenu(context, details.globalPosition, actions()),
      child: child,
    );
  }
}

/// 复制文本到剪贴板并提示。菜单里"复制歌名"这类操作用。
Future<void> copyText(
  BuildContext context,
  String text, {
  required String what,
}) async {
  await Clipboard.setData(ClipboardData(text: text));
  if (!context.mounted) return;
  ScaffoldMessenger.of(context).showSnackBar(
    SnackBar(
      content: Text(text.isEmpty ? '没有内容可复制' : '$what已复制：$text'),
      duration: const Duration(seconds: 2),
      backgroundColor: text.isEmpty ? StatusColors.warn : null,
    ),
  );
}
