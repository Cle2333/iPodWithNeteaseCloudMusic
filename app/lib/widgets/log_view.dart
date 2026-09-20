/// 日志面板：调试面板和作业详情共用。
///
/// 三个要点，都是踩过的：
///
/// 1. **增量拉取**。界面记一个 `seq` 游标，每次只要新行。全量重传的话，
///    跑完一次全库同步之后每轮轮询都要搬几千行，界面会肉眼可见地卡。
/// 2. **自动滚动可以关**。用户往上翻着看历史的时候，日志一直在往下顶
///    是没法看的——所以往上滚就自动暂停，滚回底部再恢复。
/// 3. **只看错误**在**本地**过滤，不是让后端重传一遍。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../api/models.dart';
import '../theme.dart';

/// 一次拉取。[since] 是游标（上次拿到的最大 seq）。
typedef LogFetcher = Future<LogPage> Function({int since});

class LogView extends StatefulWidget {
  const LogView({
    super.key,
    required this.fetcher,
    this.height = 260,
    this.onClear,
    this.pollInterval = const Duration(milliseconds: 1500),
    this.emptyText = '（暂无日志）',
  });

  final LogFetcher fetcher;
  final double height;

  /// 给了就在工具栏显示「清空」。
  final Future<void> Function()? onClear;

  final Duration pollInterval;
  final String emptyText;

  @override
  State<LogView> createState() => LogViewState();
}

class LogViewState extends State<LogView> {
  final List<LogLine> _lines = <LogLine>[];
  final ScrollController _scroll = ScrollController();

  int _cursor = 0;
  bool _loading = false;
  bool _errorsOnly = false;
  bool _autoScroll = true;
  bool _atBottom = true;
  String? _error;
  Timer? _timer;

  /// 界面最多留多少行。后端缓冲是 2000，这里跟它对齐。
  static const int _maxLines = 2000;

  @override
  void initState() {
    super.initState();
    _scroll.addListener(_onScroll);
    unawaited(_pull());
    _timer = Timer.periodic(widget.pollInterval, (_) => unawaited(_pull()));
  }

  @override
  void dispose() {
    _timer?.cancel();
    _scroll.removeListener(_onScroll);
    _scroll.dispose();
    super.dispose();
  }

  void _onScroll() {
    if (!_scroll.hasClients) return;
    final position = _scroll.position;
    // 离底部 40 像素以内算"在底部"
    final atBottom = position.pixels >= position.maxScrollExtent - 40;
    if (atBottom != _atBottom) {
      setState(() => _atBottom = atBottom);
    }
  }

  /// 手动立刻拉一次（比如点了清空之后）。
  Future<void> refreshNow() => _pull();

  Future<void> _pull() async {
    if (_loading || !mounted) return;
    _loading = true;
    try {
      final page = await widget.fetcher(since: _cursor);
      if (!mounted) return;

      if (page.lines.isNotEmpty) {
        setState(() {
          _lines.addAll(page.lines);
          if (_lines.length > _maxLines) {
            _lines.removeRange(0, _lines.length - _maxLines);
          }
          _cursor = page.latestSeq;
          _error = null;
        });
        if (_autoScroll && _atBottom) _jumpToBottom();
      }
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      _loading = false;
    }
  }

  void _jumpToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.jumpTo(_scroll.position.maxScrollExtent);
      }
    });
  }

  List<LogLine> get _visible {
    if (!_errorsOnly) return _lines;
    return _lines
        .where(
          (l) =>
              l.level.toUpperCase() == 'ERROR' ||
              l.level.toUpperCase() == 'CRITICAL' ||
              l.text.contains('Traceback') ||
              l.text.contains('失败'),
        )
        .toList();
  }

  Future<void> _clear() async {
    final handler = widget.onClear;
    if (handler == null) return;
    await handler();
    if (!mounted) return;
    setState(() {
      _lines.clear();
      _cursor = 0;
      _error = null;
    });
    await _pull();
  }

  Color _colorOf(LogLine line) {
    final level = line.level.toUpperCase();
    if (level == 'ERROR' || level == 'CRITICAL') return const Color(0xFFFF6B6B);
    if (level == 'WARNING') return const Color(0xFFFFC107);
    if (level == 'DEBUG') return const Color(0xFF8A8A8A);
    return const Color(0xFFD4D4D4);
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final lines = _visible;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Row(
          children: <Widget>[
            Text(
              '${_lines.length} 行',
              style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
            ),
            const SizedBox(width: 14),
            // 自动滚动开关：翻历史的时候日志还一直往下顶是没法看的
            _Toggle(
              label: '自动滚动',
              value: _autoScroll,
              onChanged: (v) => setState(() => _autoScroll = v),
            ),
            const SizedBox(width: 10),
            _Toggle(
              label: '只看错误',
              value: _errorsOnly,
              onChanged: (v) => setState(() => _errorsOnly = v),
            ),
            const Spacer(),
            if (!_atBottom)
              TextButton.icon(
                icon: const Icon(Icons.arrow_downward, size: 15),
                label: const Text('回到底部'),
                onPressed: () {
                  setState(() => _autoScroll = true);
                  _jumpToBottom();
                },
              ),
            if (widget.onClear != null)
              TextButton.icon(
                icon: const Icon(Icons.delete_outline, size: 15),
                label: const Text('清空'),
                onPressed: _lines.isEmpty ? null : _clear,
              ),
          ],
        ),
        const SizedBox(height: 6),
        if (_error != null) ...<Widget>[
          Text(
            '读取日志失败：$_error',
            style: const TextStyle(fontSize: 12, color: StatusColors.error),
          ),
          const SizedBox(height: 6),
        ],
        Container(
          height: widget.height,
          width: double.infinity,
          decoration: BoxDecoration(
            color: const Color(0xFF1E1E1E),
            borderRadius: BorderRadius.circular(8),
          ),
          padding: const EdgeInsets.all(10),
          child: lines.isEmpty
              ? Text(
                  _errorsOnly && _lines.isNotEmpty
                      ? '（没有错误日志）'
                      : widget.emptyText,
                  style: monoStyle(color: Colors.white38),
                )
              : ListView.builder(
                  controller: _scroll,
                  itemCount: lines.length,
                  itemBuilder: (context, index) {
                    final line = lines[index];
                    return SelectableText.rich(
                      TextSpan(
                        children: <InlineSpan>[
                          TextSpan(
                            text: '${line.ts}  ',
                            style: monoStyle(color: const Color(0xFF6A6A6A)),
                          ),
                          TextSpan(
                            text: '${line.text}\n',
                            style: monoStyle(color: _colorOf(line)),
                          ),
                        ],
                      ),
                    );
                  },
                ),
        ),
      ],
    );
  }
}

class _Toggle extends StatelessWidget {
  const _Toggle({
    required this.label,
    required this.value,
    required this.onChanged,
  });

  final String label;
  final bool value;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: () => onChanged(!value),
      borderRadius: BorderRadius.circular(4),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 2),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Icon(
              value ? Icons.check_box : Icons.check_box_outline_blank,
              size: 16,
              color: value
                  ? Theme.of(context).colorScheme.primary
                  : Theme.of(context).colorScheme.outline,
            ),
            const SizedBox(width: 4),
            Text(label, style: const TextStyle(fontSize: 12.5)),
          ],
        ),
      ),
    );
  }
}
