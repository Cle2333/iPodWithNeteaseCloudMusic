/// 数据库修复对话框：扫描「数据库与磁盘对不上」的地方，让用户勾选后清理。
///
/// ## 为什么要是对话框而不是一个直接执行的按钮
///
/// 这两件事都是**不可逆**的：删文件、重写数据库。所以流程刻意做成
/// 「先扫 → 摆出来给用户看 → 用户逐项勾 → 才动手」，而且每一项都写清
/// **后果**，不是只给一个数字：
///
/// * 孤儿文件 = 磁盘上有、数据库不认 → iPod 上看不见，但白占空间
/// * 断链记录 = 数据库有、磁盘没文件 → iPod 上显示得出来，点进去播不了
/// * 残留临时文件 = 写入中断留下的
///
/// 只有断链记录那条**要重写整个数据库**，风险最高，所以它**默认不勾**，
/// 而且旁边必须写明"其他歌不会丢"——不写的话没人敢点。
///
/// ## 一条实现上的注意
///
/// 轮询用**可取消的 Timer**（`_ticker`），在 `dispose()` 里取消。
/// 用裸 `Future.delayed` 循环的话，对话框关掉之后定时器还在跑，
/// 测试里会报 `A Timer is still pending even after the widget tree was disposed`
/// 而真机上就是关不干净的循环。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../theme.dart';
import 'common.dart';

/// 打开修复对话框。返回值表示**是否真的动过手**（用来决定要不要刷新列表）。
Future<bool> showRepairDialog(BuildContext context, ApiClient api) async {
  final changed = await showDialog<bool>(
    context: context,
    barrierDismissible: false,
    builder: (ctx) => _RepairDialog(api: api),
  );
  return changed ?? false;
}

enum _Phase { scanning, ready, cleaning, done, error }

class _RepairDialog extends StatefulWidget {
  const _RepairDialog({required this.api});

  final ApiClient api;

  @override
  State<_RepairDialog> createState() => _RepairDialogState();
}

class _RepairDialogState extends State<_RepairDialog> {
  _Phase _phase = _Phase.scanning;
  RepairScan? _scan;
  RepairCleanResult? _cleaned;
  String _error = '';

  bool _wantOrphans = true;
  bool _wantTemp = true;
  // ★ 危险的那一项**默认不勾**：它要整库重写。用户显式勾了才做。
  bool _wantBroken = false;

  Timer? _ticker;
  String _jobId = '';

  @override
  void initState() {
    super.initState();
    _startScan();
  }

  @override
  void dispose() {
    _ticker?.cancel();
    super.dispose();
  }

  // ── 轮询（可取消的 Timer，别用 Future.delayed 循环）────────────────

  void _watch(String jobId, void Function(JobInfo) onDone) {
    _ticker?.cancel();
    _jobId = jobId;
    _ticker = Timer.periodic(const Duration(milliseconds: 400), (_) {
      unawaited(_tick(onDone));
    });
    unawaited(_tick(onDone));   // 先立刻探一次，短作业不用等 400ms
  }

  Future<void> _tick(void Function(JobInfo) onDone) async {
    if (!mounted || _jobId.isEmpty) return;
    try {
      final job = await widget.api.job(_jobId);
      if (!mounted) return;
      if (job.finished) {
        _ticker?.cancel();
        if (job.state == 'failed') {
          setState(() {
            _phase = _Phase.error;
            _error = job.error.isEmpty ? '任务失败' : job.error;
          });
          return;
        }
        onDone(job);
      }
    } catch (e) {
      if (!mounted) return;
      _ticker?.cancel();
      setState(() {
        _phase = _Phase.error;
        _error = '取任务状态失败：$e';
      });
    }
  }

  // ── 扫描 ──────────────────────────────────────────────────────────

  Future<void> _startScan() async {
    setState(() {
      _phase = _Phase.scanning;
      _error = '';
      _cleaned = null;
    });
    try {
      final jobId = await widget.api.repairScan();
      if (!mounted) return;
      _watch(jobId, (job) {
        final scan = RepairScan.fromJson(_resultOf(job));
        setState(() {
          _scan = scan;
          _phase = _Phase.ready;
          // 有就默认勾上——这两项是纯赚（清掉白占的空间，不动任何有意义的东西）
          _wantOrphans = !scan.orphans.isEmpty;
          _wantTemp = !scan.strayTemp.isEmpty;
          _wantBroken = false;
        });
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _phase = _Phase.error;
        _error = '扫描失败：$e';
      });
    }
  }

  Map<String, dynamic> _resultOf(JobInfo job) => job.result;

  // ── 清理 ──────────────────────────────────────────────────────────

  bool get _anythingSelected => _wantOrphans || _wantTemp || _wantBroken;

  Future<void> _startClean() async {
    setState(() {
      _phase = _Phase.cleaning;
      _error = '';
    });
    try {
      final jobId = await widget.api.repairClean(
        orphans: _wantOrphans,
        strayTemp: _wantTemp,
        brokenRecords: _wantBroken,
      );
      if (!mounted) return;
      _watch(jobId, (job) {
        final result = RepairCleanResult.fromJson(_resultOf(job));
        setState(() {
          _cleaned = result;
          _phase = _Phase.done;
        });
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _phase = _Phase.error;
        _error = '修复失败：$e';
      });
    }
  }

  void _close() {
    // 动过手就让调用方刷新（设备内容/数据库可能变了）
    final changed = _cleaned != null && _cleaned!.cleaned > 0;
    Navigator.of(context).pop(changed);
  }

  /// 期间取消并关掉。
  ///
  /// ★ 必须有这条路：对话框是 `barrierDismissible: false`，如果忙碌时按钮也
  /// 禁用，那扫描一旦卡住（大库扫几千个文件确实要一会儿）用户就**关不掉窗口**，
  /// 只能杀进程 —— 而那正是这个项目一路在消灭的体验。
  ///
  /// 取消是**协作式**的（后端会停在安全的位置），所以对正在进行中的清理也安全；
  /// 但已经清掉的东西不会撤销，所以保守地让调用方刷新一次。
  Future<void> _cancelAndClose() async {
    final jobId = _jobId;
    _ticker?.cancel();
    _jobId = '';
    if (jobId.isNotEmpty) {
      try {
        await widget.api.cancelJob(jobId);
      } catch (_) {
        // 取消失败不重要：作业要么已经跑完，要么会自己结束。
        // 绝不能因为它失败就把用户堵在对话框里。
      }
    }
    if (!mounted) return;
    final touched = _cleaned != null || _phase == _Phase.cleaning;
    Navigator.of(context).pop(touched);
  }

  // ── 界面 ──────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Row(
        children: <Widget>[
          Icon(Icons.build_circle_outlined, size: 20),
          SizedBox(width: 8),
          Text('数据库修复'),
        ],
      ),
      content: SizedBox(
        width: 520,
        child: SingleChildScrollView(child: _body()),
      ),
      actions: _actions(),
    );
  }

  Widget _body() {
    switch (_phase) {
      case _Phase.scanning:
        return const Padding(
          padding: EdgeInsets.symmetric(vertical: 28),
          child: Row(
            children: <Widget>[
              SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
              SizedBox(width: 14),
              Expanded(
                child: Text(
                  '正在扫描 iPod…\n'
                  '文件多的话要几十秒。不想等可以点右下角「取消扫描」。',
                  style: TextStyle(fontSize: 13.5, height: 1.7),
                ),
              ),
            ],
          ),
        );

      case _Phase.error:
        return Notice(
          icon: Icons.error_outline,
          title: '没做成',
          text: _error,
          color: StatusColors.error,
        );

      case _Phase.cleaning:
        return const Padding(
          padding: EdgeInsets.symmetric(vertical: 28),
          child: Row(
            children: <Widget>[
              SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
              SizedBox(width: 14),
              Expanded(
                child: Text(
                  '正在修复…\n这一步可能要几十秒，期间请不要拔设备。',
                  style: TextStyle(fontSize: 13.5, height: 1.7),
                ),
              ),
            ],
          ),
        );

      case _Phase.done:
        return _doneBody();

      case _Phase.ready:
        return _readyBody();
    }
  }

  Widget _doneBody() {
    final result = _cleaned;
    if (result == null) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        const Notice(
          icon: Icons.check_circle_outline,
          title: '修复完成',
          text: '设备只认数据库，所以清完之后列表里不会有任何变化 —— '
              '被清掉的都是本来就不该在的东西。',
          color: StatusColors.ok,
        ),
        const SizedBox(height: 14),
        for (final note in result.notes)
          Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Text('· $note', style: const TextStyle(fontSize: 13)),
          ),
        if (result.freedBytes > 0) ...<Widget>[
          const SizedBox(height: 6),
          Text(
            '共释放 ${result.freedText}',
            style: const TextStyle(
              fontSize: 13.5,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ],
    );
  }

  Widget _readyBody() {
    final scan = _scan;
    if (scan == null) return const SizedBox.shrink();

    if (scan.clean) {
      return const Notice(
        icon: Icons.verified_outlined,
        title: '没有需要修复的地方',
        text: '数据库和磁盘完全一致：每一首记录都有对应的文件，'
            '磁盘上也没有多余的、iPod 看不见的垃圾文件。',
        color: StatusColors.ok,
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(
          '数据库 ${scan.dbTracks} 首 · 磁盘 ${scan.diskFiles} 个文件',
          style: TextStyle(
            fontSize: 12.5,
            color: Theme.of(context).colorScheme.onSurfaceVariant,
          ),
        ),
        const SizedBox(height: 14),
        _category(
          value: scan.orphans.isEmpty ? null : _wantOrphans,
          onChanged: scan.orphans.isEmpty
              ? null
              : (v) => setState(() => _wantOrphans = v ?? false),
          title: '孤儿文件',
          badge: '${scan.orphans.count} 个 · ${scan.orphans.sizeText}',
          why: '磁盘上有、数据库里没有记录。iPod 上**看不见**它们，'
              '但它们占着空间。同步中途失败（写数据库那一步没成）就会留下这种文件。',
          samples: scan.orphans.orphans
              .map((o) => '${o.name}  (${o.sizeText})')
              .toList(),
          more: scan.orphans.count,
        ),
        const SizedBox(height: 10),
        _category(
          value: scan.strayTemp.isEmpty ? null : _wantTemp,
          onChanged: scan.strayTemp.isEmpty
              ? null
              : (v) => setState(() => _wantTemp = v ?? false),
          title: '残留临时文件',
          badge: '${scan.strayTemp.count} 个',
          why: '写入被打断时留下的中间文件，已经没人用了。',
          samples: scan.strayTemp.names,
          more: scan.strayTemp.count,
        ),
        const SizedBox(height: 10),
        _category(
          value: scan.broken.isEmpty ? null : _wantBroken,
          onChanged: scan.broken.isEmpty
              ? null
              : (v) => setState(() => _wantBroken = v ?? false),
          title: '断链记录（要重写数据库）',
          badge: '${scan.broken.count} 首 · ${scan.broken.sizeText}',
          why: '数据库里有记录、磁盘上文件却没了。iPod 上**显示得出来但播不了**，'
              '点进去会卡住或跳过。清掉它需要重写整个数据库 —— '
              '**其他歌不会丢**，只是这一步耗时略长。',
          samples: scan.broken.broken
              .map((t) => t.title + (t.artist.isEmpty ? '' : ' — ${t.artist}'))
              .toList(),
          more: scan.broken.count,
          warn: true,
        ),
        const SizedBox(height: 16),
        Text(
          '勾选的会被清掉，**不能撤销**。没勾的保持原样。',
          style: TextStyle(
            fontSize: 12,
            color: Theme.of(context).colorScheme.onSurfaceVariant,
          ),
        ),
      ],
    );
  }

  Widget _category({
    required bool? value,
    required ValueChanged<bool?>? onChanged,
    required String title,
    required String badge,
    required String why,
    required List<String> samples,
    required int more,
    bool warn = false,
  }) {
    final scheme = Theme.of(context).colorScheme;
    final empty = value == null;
    const previewCount = 6;

    return Container(
      padding: const EdgeInsets.fromLTRB(10, 8, 12, 10),
      decoration: BoxDecoration(
        border: Border.all(
          color: warn && !empty ? StatusColors.warn : scheme.outlineVariant,
        ),
        borderRadius: BorderRadius.circular(6),
        color: empty ? scheme.surfaceContainerLowest : null,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Row(
            children: <Widget>[
              SizedBox(
                width: 28,
                height: 28,
                child: Checkbox(
                  value: empty ? false : value,
                  onChanged: empty ? null : onChanged,
                ),
              ),
              Expanded(
                child: Text(
                  title,
                  style: TextStyle(
                    fontSize: 13.5,
                    fontWeight: FontWeight.w600,
                    color: empty ? scheme.onSurfaceVariant : null,
                  ),
                ),
              ),
              Text(
                empty ? '没有' : badge,
                style: TextStyle(
                  fontSize: 12.5,
                  color: empty ? scheme.onSurfaceVariant : scheme.primary,
                  fontWeight: empty ? null : FontWeight.w600,
                ),
              ),
            ],
          ),
          Padding(
            padding: const EdgeInsets.only(left: 28, top: 2),
            child: Text(
              why.replaceAll('**', ''),
              style: TextStyle(
                fontSize: 12,
                height: 1.6,
                color: scheme.onSurfaceVariant,
              ),
            ),
          ),
          if (samples.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(left: 28, top: 6),
              child: Text(
                samples.take(previewCount).join('\n') +
                    (more > previewCount ? '\n…… 还有 ${more - previewCount} 个' : ''),
                style: TextStyle(
                  fontSize: 11.5,
                  height: 1.6,
                  color: scheme.onSurfaceVariant,
                  fontFamily: 'Consolas',
                ),
              ),
            ),
        ],
      ),
    );
  }

  List<Widget> _actions() {
    switch (_phase) {
      case _Phase.scanning:
      case _Phase.cleaning:
        return <Widget>[
          TextButton(
            onPressed: _cancelAndClose,
            child: Text(_phase == _Phase.scanning ? '取消扫描' : '取消修复'),
          ),
        ];

      case _Phase.error:
        return <Widget>[
          TextButton(onPressed: _close, child: const Text('关闭')),
          FilledButton(onPressed: _startScan, child: const Text('重新扫描')),
        ];

      case _Phase.done:
        return <Widget>[
          FilledButton(onPressed: _close, child: const Text('完成')),
        ];

      case _Phase.ready:
        final scan = _scan;
        if (scan != null && scan.clean) {
          return <Widget>[
            FilledButton(onPressed: _close, child: const Text('知道了')),
          ];
        }
        return <Widget>[
          TextButton(onPressed: _close, child: const Text('取消')),
          FilledButton.icon(
            icon: const Icon(Icons.build_outlined, size: 17),
            label: const Text('开始修复'),
            onPressed: _anythingSelected ? _startClean : null,
          ),
        ];
    }
  }
}
