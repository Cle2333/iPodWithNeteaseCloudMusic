/// 作业日程：后台队列里都干过什么。
///
/// 这是**调试面板**里的东西。下载页只该管"我下了哪些歌"，日程（谁在排队、
/// 谁失败了、每首歌的结果）是属于这里的——出问题的时候来这儿看，然后
/// 「导出诊断记录」把整份现场发给开发。
///
/// 日程是**落盘**的（`.ncm/logs/jobs.jsonl`），重启应用不会丢。否则
/// "出 bug 了导出一下"这件事就变成碰运气：重启一次，罪证全没了。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../theme.dart';
import 'common.dart';

class JobSchedule extends StatefulWidget {
  const JobSchedule({
    super.key,
    required this.api,
    required this.snapshot,
    this.height = 380,
  });

  final ApiClient api;
  final JobsSnapshot? snapshot;
  final double height;

  @override
  State<JobSchedule> createState() => _JobScheduleState();
}

class _JobScheduleState extends State<JobSchedule> {
  /// 已经展开过的作业的日志。**展开时才拉**——50 个作业的日志一次全拉
  /// 会白等好几秒，而用户通常只看一个。
  final Map<String, List<String>> _logs = <String, List<String>>{};
  final Set<String> _loading = <String>{};
  String? _openJobId;

  Future<void> _toggle(JobInfo job) async {
    if (_openJobId == job.id) {
      setState(() => _openJobId = null);
      return;
    }
    setState(() => _openJobId = job.id);
    if (_logs.containsKey(job.id)) return;

    setState(() => _loading.add(job.id));
    try {
      final page = await widget.api.jobLog(job.id);
      if (!mounted) return;
      setState(() {
        _logs[job.id] = page.lines
            .map((line) => '[${line.level}] ${line.ts} ${line.text}')
            .toList(growable: false);
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _logs[job.id] = <String>['（日志拉取失败：$e）']);
    } finally {
      if (mounted) setState(() => _loading.remove(job.id));
    }
  }

  @override
  Widget build(BuildContext context) {
    final jobs = widget.snapshot?.jobs ?? const <JobInfo>[];
    final scheme = Theme.of(context).colorScheme;

    if (jobs.isEmpty) {
      return const Notice(
        icon: Icons.event_available_outlined,
        text: '还没有任何作业记录。\n下过歌、同步过、导入过之后，这里会留下日程。',
      );
    }

    return SizedBox(
      height: widget.height,
      child: ListView.builder(
        itemCount: jobs.length,
        itemBuilder: (context, index) => _jobTile(jobs[index], scheme),
      ),
    );
  }

  Widget _jobTile(JobInfo job, ColorScheme scheme) {
    final expanded = _openJobId == job.id;
    final (IconData icon, Color color) = switch (job.state) {
      'done' => (Icons.check_circle_outline, StatusColors.ok),
      'failed' => (Icons.error_outline, StatusColors.error),
      'cancelled' => (Icons.cancel_outlined, StatusColors.warn),
      'running' => (Icons.play_circle_outline, StatusColors.info),
      _ => (Icons.schedule, StatusColors.idle),
    };

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        InkWell(
          onTap: () => unawaited(_toggle(job)),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 7),
            child: Row(
              children: <Widget>[
                Icon(icon, size: 16, color: color),
                const SizedBox(width: 8),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      Text(
                        job.title,
                        style: const TextStyle(fontSize: 13),
                        overflow: TextOverflow.ellipsis,
                      ),
                      Text(
                        _summary(job),
                        style: TextStyle(
                          fontSize: 11.5,
                          color: job.state == 'failed'
                              ? StatusColors.error
                              : scheme.onSurfaceVariant,
                        ),
                        overflow: TextOverflow.ellipsis,
                      ),
                      if (job.error.isNotEmpty)
                        Text(
                          job.error,
                          style: const TextStyle(
                            fontSize: 11.5,
                            color: StatusColors.error,
                          ),
                          overflow: TextOverflow.ellipsis,
                        ),
                    ],
                  ),
                ),
                const SizedBox(width: 8),
                Icon(
                  expanded ? Icons.expand_less : Icons.expand_more,
                  size: 18,
                  color: scheme.onSurfaceVariant,
                ),
              ],
            ),
          ),
        ),
        if (expanded) _detail(job, scheme),
        Divider(height: 1, color: scheme.outlineVariant),
      ],
    );
  }

  String _summary(JobInfo job) {
    final parts = <String>[job.kind, job.stateText];
    if (job.total > 0) parts.add('${job.done}/${job.total}');
    if (job.itemsFailed > 0) parts.add('失败 ${job.itemsFailed} 首');
    if (job.finished) parts.add('耗时 ${_duration(job.elapsed)}');
    return parts.join(' · ');
  }

  Widget _detail(JobInfo job, ColorScheme scheme) {
    final logs = _logs[job.id];
    final busy = _loading.contains(job.id);

    return Container(
      margin: const EdgeInsets.only(left: 24, bottom: 8),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: scheme.surfaceContainerHighest.withValues(alpha: 0.5),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          if (job.hasItems) ...<Widget>[
            Text(
              '曲目（共 ${job.items.length} 首'
              '，成功 ${job.itemsDone}，失败 ${job.itemsFailed}）',
              style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 4),
            for (final item in job.items) _itemLine(item, scheme),
            const SizedBox(height: 10),
          ],
          if (busy)
            const LoadingLine(text: '正在拉日志…')
          else ...<Widget>[
            Text(
              '日志（${logs?.length ?? 0} 行）',
              style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 4),
            if (logs == null || logs.isEmpty)
              Text(
                '（没有日志）',
                style: TextStyle(
                  fontSize: 11.5,
                  color: scheme.onSurfaceVariant,
                ),
              )
            else
              // 截尾部：日志最重要的是最后几行（失败原因都在这儿）
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  for (final line
                      in logs.length > 40
                          ? logs.sublist(logs.length - 40)
                          : logs)
                    Text(
                      line,
                      style: const TextStyle(
                        fontFamily: 'Consolas',
                        fontFamilyFallback: <String>[
                          'Courier New',
                          'monospace',
                        ],
                        fontSize: 11.5,
                        height: 1.5,
                      ),
                    ),
                ],
              ),
          ],
        ],
      ),
    );
  }

  Widget _itemLine(JobItem item, ColorScheme scheme) {
    final (IconData icon, Color color) = switch (item.status) {
      'done' => (Icons.check, StatusColors.ok),
      'failed' => (Icons.close, StatusColors.error),
      'downloading' => (Icons.downloading, StatusColors.info),
      _ => (Icons.schedule, StatusColors.idle),
    };
    final text = <String>[
      if (item.name.isNotEmpty) item.name else '（没有歌名）',
      if (item.artist.isNotEmpty) '- ${item.artist}',
      if (item.done && item.sizeText.isNotEmpty) item.sizeText,
      if (item.failed && item.reason.isNotEmpty) '← ${item.reason}',
    ].join(' ');

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 1.5),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Icon(icon, size: 13, color: color),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              text,
              style: TextStyle(
                fontSize: 11.5,
                color: item.failed ? StatusColors.error : null,
              ),
            ),
          ),
        ],
      ),
    );
  }

  static String _duration(int seconds) {
    if (seconds < 60) return '$seconds 秒';
    final minutes = seconds ~/ 60;
    if (minutes < 60) return '$minutes 分 ${seconds % 60} 秒';
    return '${minutes ~/ 60} 小时 ${minutes % 60} 分';
  }
}
