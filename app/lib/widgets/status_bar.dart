/// 顶部常驻状态条：设备 / 账号 / 服务 / 作业。
///
/// 设计要点：**任何异常都要能一眼看出来，并且能点开看原因**。
/// 只显示一个红点而不说为什么，用户只会来问"为什么红了"。
///
/// 作业那一段尤其重要：后端是全局串行的，用户点了下载之后很可能
/// 前面还排着别的活。不显示"排队中"，用户只会觉得"点了没反应"。
library;

import 'package:flutter/material.dart';

import '../api/models.dart';
import '../theme.dart';
import 'common.dart';

class TopStatusBar extends StatelessWidget {
  const TopStatusBar({
    super.key,
    required this.status,
    required this.error,
    this.jobs,
    this.onJobsTap,
    this.onRefresh,
  });

  final AppStatus? status;
  final String? error;
  final JobsSnapshot? jobs;
  final VoidCallback? onJobsTap;
  final VoidCallback? onRefresh;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final data = status;
    final queue = jobs;

    return Container(
      decoration: BoxDecoration(
        color: scheme.surfaceContainerLowest,
        border: Border(bottom: BorderSide(color: scheme.outlineVariant)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
      child: Row(
        children: <Widget>[
          _Chip(
            color: data == null
                ? StatusColors.idle
                : (data.ipod.connected ? StatusColors.ok : StatusColors.error),
            label: 'iPod',
            value: data == null ? '读取中…' : data.ipod.summary,
            tooltip: data?.ipod.hint,
          ),
          const SizedBox(width: 10),
          _Chip(
            color: data == null
                ? StatusColors.idle
                : (data.account.loggedIn ? StatusColors.ok : StatusColors.warn),
            label: '网易云',
            value: data == null ? '读取中…' : data.account.summary,
          ),
          const SizedBox(width: 10),
          _Chip(
            color: data == null
                ? StatusColors.idle
                : (data.service.reachable
                      ? StatusColors.ok
                      : StatusColors.error),
            label: '服务',
            value: data == null ? '读取中…' : data.service.summary,
            tooltip: data?.service.detail,
          ),
          const Spacer(),
          if (error != null) ...<Widget>[
            Flexible(
              child: Tooltip(
                message: error!,
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: <Widget>[
                    const Icon(
                      Icons.error_outline,
                      size: 16,
                      color: StatusColors.error,
                    ),
                    const SizedBox(width: 4),
                    Flexible(
                      child: Text(
                        error!,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          fontSize: 12,
                          color: StatusColors.error,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(width: 10),
          ],
          _JobsChip(snapshot: queue, onTap: onJobsTap),
          const SizedBox(width: 6),
          IconButton(
            icon: const Icon(Icons.refresh, size: 18),
            tooltip: '立即刷新状态',
            onPressed: onRefresh,
            visualDensity: VisualDensity.compact,
          ),
        ],
      ),
    );
  }
}

/// 作业状态：跑着的显示标题 + 进度，排队的显示数量。
class _JobsChip extends StatelessWidget {
  const _JobsChip({required this.snapshot, this.onTap});

  final JobsSnapshot? snapshot;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final queue = snapshot;

    if (queue == null) {
      return Text(
        '读取中…',
        style: TextStyle(fontSize: 12.5, color: scheme.onSurfaceVariant),
      );
    }

    final running = queue.running;
    if (running == null && queue.queued == 0) {
      return Text(
        '空闲',
        style: TextStyle(fontSize: 12.5, color: scheme.onSurfaceVariant),
      );
    }

    final label = running == null
        ? '排队 ${queue.queued} 个'
        : (running.total > 0
              ? '${running.title} ${running.done}/${running.total}'
              : running.title);

    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(6),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            if (running != null) ...<Widget>[
              const SizedBox(
                width: 12,
                height: 12,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
              const SizedBox(width: 6),
            ] else ...<Widget>[
              const Dot(StatusColors.warn, size: 8),
              const SizedBox(width: 6),
            ],
            ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 280),
              child: Text(
                label,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 12.5),
              ),
            ),
            if (running != null && running.total > 0) ...<Widget>[
              const SizedBox(width: 6),
              Text(
                '${running.percent.toStringAsFixed(0)}%',
                style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip({
    required this.color,
    required this.label,
    required this.value,
    this.tooltip,
  });

  final Color color;
  final String label;
  final String value;
  final String? tooltip;

  @override
  Widget build(BuildContext context) {
    final text = Text.rich(
      TextSpan(
        children: <InlineSpan>[
          TextSpan(
            text: '$label ',
            style: TextStyle(
              color: Theme.of(context).colorScheme.onSurfaceVariant,
              fontSize: 12.5,
            ),
          ),
          TextSpan(
            text: value,
            style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w500),
          ),
        ],
      ),
      overflow: TextOverflow.ellipsis,
    );

    final row = Row(
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Dot(color),
        const SizedBox(width: 6),
        ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 320),
          child: text,
        ),
      ],
    );

    final hint = tooltip;
    if (hint == null || hint.isEmpty) return row;
    return Tooltip(message: hint, child: row);
  }
}
