/// 进度页：只看进度。
///
/// 这里**只**回答一个问题——"这次跑到哪了"：在哪个阶段、这一步完成多少、
/// 逐首歌的结果（含失败原因）、还要多久、能不能取消。
///
/// ★ 为什么单独强调"阶段"：一次同步要过好几段，每段耗时差一个数量级
/// （实测 147 首：转码 6 分钟、拷贝 3 分钟、写库 6 秒）。以前只报百分比，
/// 而写入 iPod 那一段既不报百分比也不报阶段——界面整整 6 分钟一片空白，
/// 用户的反馈是"传很多歌就以为被卡住了"。现在两样都有：阶段名 + 进度条，
/// 切不出等份的段（刷盘 / 整库重写）显示不确定进度条（一直在滚的）。
/// 判据只有一条：**只要还在干活，界面上就必须有东西在动。**
///
/// 曾经这里还有第二个标签「本地已下载」，管电脑上那份缓存（搜索/排序/多选
/// 删除/按来源歌单分组）。2026-09-20 合并掉了：那套东西回答的是"这首歌本地
/// 有没有"，而歌单页本来就按同一份数据在显示状态（未下载/已下载/已同步）
/// ——同一件事在两页各说一遍、说法还不完全一样，正是 bug 的温床。
/// 删除本地文件的能力挪到了歌单页（右键单首／多选操作条）。
///
/// 不做的事：作业队列、日志——那是设置页调试面板的活。
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/common.dart';

class DownloadPage extends StatelessWidget {
  const DownloadPage({super.key});

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final job = state.jobs?.showcase;
    final api = context.read<AppState>().api;

    return Column(
      children: <Widget>[
        Padding(
          padding: const EdgeInsets.fromLTRB(18, 12, 18, 8),
          child: Row(
            children: <Widget>[
              const Text(
                '进度',
                style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
              ),
              const Spacer(),
              // 正在跑的时候把"在跑什么 / 跑到哪个阶段了"顺带写在标题行，
              // 省得用户去别处找。阶段比数字有用——数字在原地时，
              // 是阶段名在告诉用户"没卡住，只是还没走完这一步"。
              if (job != null && !job.finished)
                Text(
                  job.total > 0
                      ? '${job.title} · ${job.done}/${job.total}'
                      : '${job.title}${job.stage.isNotEmpty ? ' · ${job.stage}' : ''}',
                  style: TextStyle(
                    fontSize: 12.5,
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                  ),
                ),
            ],
          ),
        ),
        const Divider(height: 1),
        Expanded(
          child: _TaskTab(job: job, snapshot: state.jobs, api: api),
        ),
      ],
    );
  }
}

// ══════════════════════════════════════════════════════════════════════

class _TaskTab extends StatefulWidget {
  const _TaskTab({
    required this.job,
    required this.snapshot,
    required this.api,
  });

  final JobInfo? job;
  final JobsSnapshot? snapshot;
  final ApiClient api;

  @override
  State<_TaskTab> createState() => _TaskTabState();
}

class _TaskTabState extends State<_TaskTab> {
  Future<void> _cancel(JobInfo job) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('取消下载'),
        content: Text(
          '确定要取消「${job.title}」吗？\n\n'
          '取消是干净的：会在当前这一首处理完之后停下，不会留下半截文件。',
          style: const TextStyle(height: 1.6),
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('继续下载'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('取消下载'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    final appState = context.read<AppState>();
    try {
      final message = await widget.api.cancelJob(job.id);
      if (!mounted) return;
      _toast(message);
      await appState.refreshNow();
    } catch (e) {
      _toast(e.toString(), error: true);
    }
  }

  void _toast(String message, {bool error = false}) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(message),
        backgroundColor: error ? StatusColors.error : null,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final job = widget.job;
    if (job == null) {
      return const Padding(
        padding: EdgeInsets.all(18),
        child: Notice(
          icon: Icons.download_done_outlined,
          text: '还没有跑过下载或同步。\n'
              '到「歌单」页挑一个歌单，勾几首或者整单下下来 —— '
              '下载和同步的进度都会显示在这里。',
        ),
      );
    }

    return ListView(
      padding: const EdgeInsets.fromLTRB(18, 14, 18, 18),
      children: <Widget>[
        _progressCard(job, widget.snapshot),
        const SizedBox(height: 16),
        if (job.hasItems)
          _itemList(job)
        else if (job.finished)
          // ★ 已结束却没有逐首记录 —— **这是正常的**，不是"还没开始"。
          //
          // 逐首记录来自**下载**那一环。歌要是本地早就下好了，这次同步就
          // 只做"写入 iPod"，一首都不下载，于是 items 是空的。以前这里
          // 一律显示"还没开始下歌，稍等一下"，对一条**已经跑完**的同步说
          // 这句话，用户会以为它卡住了或者没干活。
          //
          // ★ `finished` 同时包含 done / failed / cancelled，所以文案必须
          // 再按 `state == 'done'` 收窄：跟一条失败的同步说"直接写进了 iPod"
          // 会和上面的「任务失败」自相矛盾。
          //
          // 另外别让用户"看上面的进度条"：sync 收尾那一步（重建数据库并签名）
          // 不带总数，`stage()` 会把 total 归零，进度卡片那时显示的是
          // 「没有进度信息」——根本没有进度条可看。
          Notice(
            icon: Icons.info_outline,
            text: job.kind == 'sync' && job.state == 'done'
                ? '这次同步没有下载任何歌（歌都在本地了），直接写进了 iPod。\n'
                  '逐首清单只在「下载」那一环产生；写入的情况看上面的任务卡片。'
                : '这次任务没有逐首记录。\n失败的曲目会列在上面。',
            color: StatusColors.idle,
          )
        else
          const Notice(
            icon: Icons.hourglass_top,
            text: '这个任务还没开始下歌，稍等一下就有清单了。',
            color: StatusColors.warn,
          ),
      ],
    );
  }

  Widget _progressCard(JobInfo job, JobsSnapshot? snapshot) {
    final scheme = Theme.of(context).colorScheme;
    final waiting = snapshot != null && snapshot.queued > 0 && !job.running;

    return InfoCard(
      title: job.title,
      subtitle: job.stateText,
      trailing: job.finished
          ? null
          : OutlinedButton.icon(
              icon: const Icon(Icons.stop_circle_outlined, size: 17),
              label: const Text('取消下载'),
              onPressed: () => _cancel(job),
            ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          const SizedBox(height: 4),
          // ── 阶段行：整个进度页最重要的一行 ─────────────────────────
          // 同步要过好几段，每段耗时差一个数量级（实测转码 6 分钟、拷贝 3 分钟、
          // 写库 6 秒）。进度条只说明"这一段走了多少"，阶段名才说明"在干什么"。
          // 用户投诉"传很多歌就以为被卡住了"——正是因为以前这里一片空白。
          if (job.running && job.stageLine.isNotEmpty) ...<Widget>[
            Row(
              children: <Widget>[
                const SizedBox(
                  width: 13,
                  height: 13,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    job.stageLine,
                    style: const TextStyle(
                      fontSize: 13.5,
                      fontWeight: FontWeight.w600,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                if (job.elapsed > 0)
                  Text(
                    '已用 ${_duration(job.elapsed)}',
                    style: TextStyle(
                      fontSize: 12,
                      color: scheme.onSurfaceVariant,
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 12),
          ],
          if (job.hasCounts) ...<Widget>[
            Row(
              children: <Widget>[
                Text(
                  '${job.done} / ${job.total}',
                  style: const TextStyle(
                    fontSize: 20,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(width: 10),
                Text(
                  '${job.percent.toStringAsFixed(0)}%',
                  style: TextStyle(
                    fontSize: 13,
                    color: scheme.onSurfaceVariant,
                  ),
                ),
                const Spacer(),
                if (job.running && job.etaSeconds != null)
                  Text(
                    '还需约 ${_duration(job.etaSeconds!)}',
                    style: TextStyle(
                      fontSize: 12.5,
                      color: scheme.onSurfaceVariant,
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 10),
            ClipRRect(
              borderRadius: BorderRadius.circular(4),
              child: LinearProgressIndicator(
                value: job.percent / 100,
                minHeight: 8,
                backgroundColor: scheme.surfaceContainerHighest,
              ),
            ),
          ] else if (job.running) ...<Widget>[
            // ★ 没有总数 = 这一段**切不出等份**（整库重写、刷盘、算签名）。
            //
            // 画一根停在 0% 的条是错的——那看起来就是卡死，而写库/刷盘其实
            // 正在干活。所以画**不确定进度条**（一直在滚的那种）：只要它在滚，
            // 用户就知道程序还活着。
            ClipRRect(
              borderRadius: BorderRadius.circular(4),
              child: LinearProgressIndicator(
                minHeight: 8,
                backgroundColor: scheme.surfaceContainerHighest,
              ),
            ),
            const SizedBox(height: 10),
            Text(
              '这一步拆不成小段（整库重写 / 刷盘），进度条只表示"还在干"。'
              '它在滚就说明没卡住 —— 这一步请别拔设备。',
              style: TextStyle(
                fontSize: 12,
                color: scheme.onSurfaceVariant,
                height: 1.5,
              ),
            ),
          ] else
            Text(
              '没有进度信息',
              style: TextStyle(fontSize: 13, color: scheme.onSurfaceVariant),
            ),
          if (job.running && job.current.isNotEmpty) ...<Widget>[
            const SizedBox(height: 10),
            Row(
              children: <Widget>[
                const SizedBox(
                  width: 12,
                  height: 12,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    job.stage.contains('写入') || job.stage.contains('数据库')
                        ? '正在处理：${job.current}'
                        : '正在下载：${job.current}',
                    style: const TextStyle(fontSize: 12.5),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
              ],
            ),
          ],
          if (job.finished) ...<Widget>[
            const SizedBox(height: 10),
            Wrap(
              spacing: 24,
              children: <Widget>[
                Stat(
                  '成功',
                  '${job.itemsDone}',
                  unit: '首',
                  color: StatusColors.ok,
                ),
                if (job.itemsFailed > 0)
                  Stat(
                    '失败',
                    '${job.itemsFailed}',
                    unit: '首',
                    color: StatusColors.error,
                  ),
                Stat('耗时', _duration(job.elapsed)),
              ],
            ),
          ],
          if (waiting) ...<Widget>[
            const SizedBox(height: 10),
            Notice(
              icon: Icons.hourglass_top,
              text:
                  '前面还有 ${snapshot.queued} 个任务在排队，这个要等它们跑完。'
                  '后端是串行的——不并发是刻意的，网易云高频请求会封号。',
              color: StatusColors.warn,
            ),
          ],
          if (job.error.isNotEmpty) ...<Widget>[
            const SizedBox(height: 10),
            Notice(
              icon: Icons.error_outline,
              title: '任务失败',
              text: job.error,
              color: StatusColors.error,
            ),
          ],
        ],
      ),
    );
  }

  Widget _itemList(JobInfo job) {
    return InfoCard(
      title: '本次歌单',
      // 成功/失败都写全，别让用户自己数
      subtitle:
          '共 ${job.items.length} 首'
          ' · 成功 ${job.itemsDone}'
          '${job.itemsFailed > 0 ? ' · 失败 ${job.itemsFailed}' : ''}',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          const SizedBox(height: 2),
          for (final item in job.items) _itemRow(item),
          const SizedBox(height: 10),
          Text(
            '要排查问题：设置 → 后端 → 导出诊断记录',
            style: TextStyle(
              fontSize: 12,
              color: Theme.of(context).colorScheme.onSurfaceVariant,
            ),
          ),
        ],
      ),
    );
  }

  Widget _itemRow(JobItem item) {
    final scheme = Theme.of(context).colorScheme;
    final (IconData icon, Color color) = switch (item.status) {
      'done' => (Icons.check_circle, StatusColors.ok),
      'failed' => (Icons.cancel, StatusColors.error),
      'downloading' => (Icons.downloading, StatusColors.info),
      _ => (Icons.schedule, StatusColors.idle),
    };

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Icon(icon, size: 16, color: color),
          const SizedBox(width: 8),
          SizedBox(
            width: 38,
            child: Text(
              '${item.index}',
              style: TextStyle(fontSize: 11.5, color: scheme.onSurfaceVariant),
            ),
          ),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text(
                  item.name.isEmpty ? '（没有歌名）' : item.name,
                  style: const TextStyle(fontSize: 13),
                  overflow: TextOverflow.ellipsis,
                ),
                if (item.artist.isNotEmpty)
                  Text(
                    item.artist,
                    style: TextStyle(
                      fontSize: 11.5,
                      color: scheme.onSurfaceVariant,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                // 失败原因紧跟在歌名下面——"有 3 首失败"这种话没用，
                // 得知道是哪 3 首、为什么
                if (item.failed && item.reason.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      item.reason,
                      style: const TextStyle(
                        fontSize: 11.5,
                        color: StatusColors.error,
                      ),
                    ),
                  ),
              ],
            ),
          ),
          const SizedBox(width: 10),
          Text(
            item.done && item.sizeText.isNotEmpty
                ? item.sizeText
                : item.statusText,
            style: TextStyle(
              fontSize: 11.5,
              color: item.failed ? StatusColors.error : scheme.onSurfaceVariant,
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
