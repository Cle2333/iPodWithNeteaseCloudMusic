/// 界面上反复出现的小组件。
///
/// 抽出来的理由很实在：卡片、字段行、统计块、提示框在四个页面里都要用，
/// 各写一份的话，改一次样式要改四处，迟早改漏一处。
library;

import 'package:flutter/material.dart';

import '../theme.dart';

/// 分组标题，右侧可挂一个操作按钮。
class SectionTitle extends StatelessWidget {
  const SectionTitle({
    super.key,
    required this.title,
    this.action,
    this.subtitle,
  });

  final String title;
  final String? subtitle;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text(
                  title,
                  style: const TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                if (subtitle != null) ...<Widget>[
                  const SizedBox(height: 3),
                  Text(
                    subtitle!,
                    style: TextStyle(
                      fontSize: 12,
                      color: scheme.onSurfaceVariant,
                    ),
                  ),
                ],
              ],
            ),
          ),
          ?action,
        ],
      ),
    );
  }
}

/// 一张内容卡片。
class InfoCard extends StatelessWidget {
  const InfoCard({
    super.key,
    this.title,
    this.subtitle,
    this.rows,
    this.child,
    this.trailing,
    this.highlight = false,
  });

  final String? title;
  final String? subtitle;
  final List<InfoRow>? rows;
  final Widget? child;
  final Widget? trailing;

  /// 出问题时整张卡变红并置顶用。
  final bool highlight;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      color: highlight ? scheme.errorContainer.withValues(alpha: 0.35) : null,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            if (title != null)
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: <Widget>[
                        Text(
                          title!,
                          style: const TextStyle(
                            fontSize: 14.5,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        if (subtitle != null) ...<Widget>[
                          const SizedBox(height: 3),
                          Text(
                            subtitle!,
                            style: TextStyle(
                              fontSize: 11.5,
                              color: scheme.onSurfaceVariant,
                            ),
                          ),
                        ],
                      ],
                    ),
                  ),
                  ?trailing,
                ],
              ),
            if (title != null) const SizedBox(height: 12),
            if (rows != null)
              ...rows!.map(
                (row) => Padding(
                  padding: const EdgeInsets.symmetric(vertical: 4),
                  child: row,
                ),
              )
            else
              ?child,
          ],
        ),
      ),
    );
  }
}

/// 一行「标签 — 值」。
class InfoRow extends StatelessWidget {
  const InfoRow(
    this.label,
    this.value, {
    super.key,
    this.editable = false,
    this.note,
    this.trailing,
    this.mono = false,
  });

  final String label;
  final String value;
  final bool editable;
  final String? note;
  final Widget? trailing;
  final bool mono;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        SizedBox(
          width: 130,
          child: Text(
            label,
            style: TextStyle(fontSize: 13, color: scheme.onSurfaceVariant),
          ),
        ),
        Expanded(
          child: SelectableText(
            value.isEmpty ? '—' : value,
            style: mono ? monoStyle(size: 12.5) : const TextStyle(fontSize: 13),
          ),
        ),
        ?trailing,
        if (editable)
          Tooltip(
            message: note ?? '可修改',
            child: const Padding(
              padding: EdgeInsets.only(left: 6),
              child: Icon(
                Icons.edit_outlined,
                size: 15,
                color: StatusColors.info,
              ),
            ),
          )
        else if (note != null)
          Tooltip(
            message: note!,
            child: Padding(
              padding: const EdgeInsets.only(left: 6),
              child: Icon(Icons.lock_outline, size: 14, color: scheme.outline),
            ),
          ),
      ],
    );
  }
}

/// 数字 + 单位的统计块。
class Stat extends StatelessWidget {
  const Stat(this.label, this.value, {super.key, this.unit = '', this.color});

  final String label;
  final String value;
  final String unit;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Text(
          label,
          style: TextStyle(fontSize: 11.5, color: scheme.onSurfaceVariant),
        ),
        const SizedBox(height: 3),
        Text.rich(
          TextSpan(
            children: <InlineSpan>[
              TextSpan(
                text: value,
                style: TextStyle(
                  fontSize: 19,
                  fontWeight: FontWeight.w600,
                  color: color,
                ),
              ),
              if (unit.isNotEmpty)
                TextSpan(
                  text: ' $unit',
                  style: TextStyle(
                    fontSize: 12,
                    color: scheme.onSurfaceVariant,
                  ),
                ),
            ],
          ),
        ),
      ],
    );
  }
}

/// 一块提示。icon + 可选标题 + 正文。
class Notice extends StatelessWidget {
  const Notice({
    super.key,
    required this.icon,
    required this.text,
    this.title,
    this.color,
    this.action,
  });

  final IconData icon;
  final String text;
  final String? title;
  final Color? color;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final tone = color ?? scheme.onSurfaceVariant;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Icon(icon, size: 20, color: tone),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  if (title != null) ...<Widget>[
                    Text(
                      title!,
                      style: const TextStyle(
                        fontSize: 13.5,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 4),
                  ],
                  Text(
                    text,
                    style: TextStyle(fontSize: 12.5, height: 1.5, color: tone),
                  ),
                ],
              ),
            ),
            ?action,
          ],
        ),
      ),
    );
  }
}

/// 加载中。
class LoadingLine extends StatelessWidget {
  const LoadingLine({super.key, required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: <Widget>[
        const SizedBox(
          width: 16,
          height: 16,
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
        const SizedBox(width: 10),
        Text(text, style: const TextStyle(fontSize: 13)),
      ],
    );
  }
}

/// 状态小圆点。
class Dot extends StatelessWidget {
  const Dot(this.color, {super.key, this.size = 8});

  final Color color;
  final double size;

  @override
  Widget build(BuildContext context) => Container(
    width: size,
    height: size,
    decoration: BoxDecoration(color: color, shape: BoxShape.circle),
  );
}

/// 三种检查状态对应的颜色（健康检查、歌单同步状态都用它）。
Color statusColor(String status) => switch (status) {
  'ok' || 'on_ipod' || 'add' => StatusColors.ok,
  'warn' || 'downloaded' || 'transcode' => StatusColors.warn,
  'fail' || 'error' => StatusColors.error,
  'skip' => StatusColors.idle,
  _ => StatusColors.info,
};

/// 状态徽章。
class StatusBadge extends StatelessWidget {
  const StatusBadge(this.text, {super.key, required this.status});

  final String text;
  final String status;

  @override
  Widget build(BuildContext context) {
    final color = statusColor(status);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(4),
        border: Border.all(color: color.withValues(alpha: 0.4)),
      ),
      child: Text(
        text,
        style: TextStyle(fontSize: 11.5, color: color, height: 1.3),
      ),
    );
  }
}
