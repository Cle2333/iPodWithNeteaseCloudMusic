/// 歌单浏览：左边歌单列表，右边曲目 + 逐个勾选 + 批量下载/同步。
///
/// 设计要点：
/// * **歌单列表 10 分钟缓存**（后端做的），界面给显式「刷新」绕过。
///   浏览不该反复打接口——那是在消耗有风控的配额。
/// * **选择跨页保持**。50 首一页，翻页不该把已经勾好的清掉。
/// * **「一键选中所有未下载的」要覆盖整单，不是当前页**。所以它走后端的
///   `/ids` 轻量接口拿全部 ID，而不是只勾当前页——246 首的歌单里
///   只勾到 50 首，用户会以为都在跑了。
/// * **下载前必须先看预览**，而且预览要按**选中的那些**算。
///   预览说"要下 246 首"、实际只下 5 首的话，这个预览就是假的。
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/common.dart';
import '../widgets/context_menu.dart';

class PlaylistPage extends StatefulWidget {
  const PlaylistPage({super.key});

  @override
  State<PlaylistPage> createState() => _PlaylistPageState();
}

class _PlaylistPageState extends State<PlaylistPage> {
  PlaylistList? _data;
  String? _error;
  bool _loading = false;
  PlaylistSummary? _selected;

  ApiClient get _api => context.read<AppState>().api;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => unawaited(_load()));
  }

  Future<void> _load({bool refresh = false}) async {
    if (!mounted) return;
    setState(() => _loading = true);
    try {
      final page = await _api.playlists(refresh: refresh);
      if (!mounted) return;
      setState(() {
        _data = page;
        _error = null;
        if (page.playlists.isNotEmpty && _selected == null) {
          _selected = page.playlists.first;
        } else if (_selected != null) {
          // 刷新后保持选中同一个（列表可能换了顺序）
          _selected =
              page.playlists.where((p) => p.id == _selected!.id).firstOrNull ??
              page.playlists.firstOrNull;
        }
      });
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final data = _data;

    return Stack(
      children: <Widget>[
        Row(
          children: <Widget>[
            SizedBox(
              width: 300,
              child: Column(
                children: <Widget>[
                  Padding(
                    padding: const EdgeInsets.fromLTRB(14, 14, 8, 8),
                    child: Row(
                      children: <Widget>[
                        const Text(
                          '歌单',
                          style: TextStyle(
                            fontSize: 15,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        const Spacer(),
                        if (data != null && data.cached)
                          Tooltip(
                            message: '10 分钟内的缓存。点刷新可以强制重新读取。',
                            child: Text(
                              '缓存',
                              style: TextStyle(
                                fontSize: 11,
                                color: scheme.onSurfaceVariant,
                              ),
                            ),
                          ),
                        IconButton(
                          icon: const Icon(Icons.refresh, size: 18),
                          tooltip: '重新读取（会打一次接口）',
                          onPressed: _loading
                              ? null
                              : () => _load(refresh: true),
                        ),
                      ],
                    ),
                  ),
                  const Divider(height: 1),
                  Expanded(child: _list()),
                ],
              ),
            ),
            const VerticalDivider(width: 1),
            Expanded(child: _detail()),
          ],
        ),
        if (_loading)
          const Positioned(
            top: 0,
            left: 0,
            right: 0,
            child: LinearProgressIndicator(minHeight: 2),
          ),
      ],
    );
  }

  Widget _list() {
    if (_error != null) {
      return Padding(
        padding: const EdgeInsets.all(14),
        child: Notice(
          icon: Icons.error_outline,
          title: '读不到歌单',
          text: _error!,
          color: StatusColors.error,
          action: TextButton(onPressed: () => _load(), child: const Text('重试')),
        ),
      );
    }

    final data = _data;
    if (data == null) {
      return const Padding(
        padding: EdgeInsets.all(14),
        child: LoadingLine(text: '正在读取歌单…'),
      );
    }
    if (data.loading) {
      return Padding(
        padding: const EdgeInsets.all(14),
        child: Notice(
          icon: Icons.hourglass_top,
          title: '正在排队',
          text: data.message.isEmpty ? '前面有作业在跑，歌单要排在它后面。' : data.message,
          color: StatusColors.warn,
          action: TextButton(onPressed: () => _load(), child: const Text('重试')),
        ),
      );
    }
    if (data.playlists.isEmpty) {
      return const Padding(
        padding: EdgeInsets.all(14),
        child: Notice(
          icon: Icons.queue_music_outlined,
          text: '这个账号下没有歌单。还没登录的话，先去设置页扫码登录。',
        ),
      );
    }

    return ListView.builder(
      itemCount: data.playlists.length,
      itemBuilder: (context, index) {
        final playlist = data.playlists[index];
        final selected = playlist.id == _selected?.id;
        return ListTile(
          dense: true,
          selected: selected,
          leading: Icon(
            playlist.liked ? Icons.favorite : Icons.queue_music,
            size: 18,
            color: playlist.liked ? StatusColors.error : null,
          ),
          title: Text(playlist.name, style: const TextStyle(fontSize: 13)),
          subtitle: Text(
            '${playlist.trackCount} 首',
            style: const TextStyle(fontSize: 11.5),
          ),
          onTap: () => setState(() => _selected = playlist),
        );
      },
    );
  }

  Widget _detail() {
    final playlist = _selected;
    if (playlist == null) {
      return Center(
        child: Text(
          '从左边选一个歌单',
          style: TextStyle(
            fontSize: 13,
            color: Theme.of(context).colorScheme.onSurfaceVariant,
          ),
        ),
      );
    }
    // key 跟歌单 ID 绑定：切歌单时重建，避免把上一个歌单的曲目先显示一帧
    return _PlaylistDetail(
      key: ValueKey<int>(playlist.id),
      playlist: playlist,
      api: _api,
    );
  }
}

class _PlaylistDetail extends StatefulWidget {
  const _PlaylistDetail({super.key, required this.playlist, required this.api});

  final PlaylistSummary playlist;
  final ApiClient api;

  @override
  State<_PlaylistDetail> createState() => _PlaylistDetailState();
}

class _PlaylistDetailState extends State<_PlaylistDetail> {
  PlaylistSongs? _songs;
  String? _error;
  bool _loading = false;
  int _page = 1;
  SongFilter _filter = SongFilter.all;
  static const int _pageSize = 50;

  /// 按歌名/艺人搜当前歌单。
  ///
  /// 在**服务端**筛（`/songs?search=`）而不是本地过一遍：本地只能筛当前页
  /// 的 50 首，246 首的歌单"搜了跟没搜一样"。
  final TextEditingController _search = TextEditingController();

  /// 勾选的曲子。**跨页保持**——翻页不该清空。
  final Set<int> _selected = <int>{};

  /// 作业提交后排的那次"过一会儿自动刷新"。
  ///
  /// 必须是可取消的 Timer 而不是裸的 `Future.delayed`：页面在倒计时里
  /// 被销毁的话，裸 delayed 会在树都拆了之后还去碰状态（测试直接报
  /// "A Timer is still pending even after the widget tree was disposed"）。

  /// `null` 表示"整个歌单"，这是跟"选了 0 首"完全不同的意思。
  Set<int>? get _selection => _selected.isEmpty ? null : _selected;

  ApiClient get _api => widget.api;

  late final AppState _state = context.read<AppState>();

  @override
  void initState() {
    super.initState();
    // 作业干完自动重刷：下载/同步完成之后，行上的状态
    //（未下载 → 已下载 → 已同步）才会变。以前是等 5 秒碰运气。
    _state.refreshSignal.addListener(_onJobFinished);
    unawaited(_load());
  }

  void _onJobFinished() {
    if (mounted) unawaited(_load());
  }

  @override
  void dispose() {
    _state.refreshSignal.removeListener(_onJobFinished);
    _search.dispose();
    super.dispose();
  }

  Future<void> _load({int? page}) async {
    if (!mounted) return;
    setState(() {
      _loading = true;
      _page = page ?? _page;
    });
    try {
      final songs = await _api.playlistSongs(
        widget.playlist.id,
        page: _page,
        size: _pageSize,
        status: _filter.value,
        search: _search.text.trim(),
      );
      if (!mounted) return;
      setState(() {
        _songs = songs;
        _error = null;
      });
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  void _setFilter(SongFilter filter) {
    if (filter == _filter) return;
    // 切筛选**不清空**勾选：用户可能就是想"先勾几首、再换个筛选接着勾"
    setState(() => _filter = filter);
    unawaited(_load(page: 1));
  }

  void _toggle(int songId) {
    setState(() {
      if (!_selected.remove(songId)) _selected.add(songId);
    });
  }

  void _selectCurrentPage() {
    final songs = _songs?.songs ?? const <SongRow>[];
    setState(() {
      for (final song in songs) {
        if (song.selectable) _selected.add(song.id);
      }
    });
  }

  void _clearSelection() => setState(_selected.clear);

  /// 一键选中**所有**未下载的。
  ///
  /// 关键在"所有"：走后端 `/ids` 拿整单的未下载 ID，而不是只勾当前页。
  /// 只勾当前页的话，246 首的歌单里用户以为全选上了，实际 50 首。
  Future<void> _selectAllPending() async {
    setState(() => _loading = true);
    try {
      final ids = await _api.playlistIds(
        widget.playlist.id,
        status: SongFilter.pending.value,
      );
      if (!mounted) return;
      setState(() => _selected.addAll(ids));
      _toast('已选中 ${ids.length} 首未下载的');
    } catch (e) {
      _toast(e.toString(), error: true);
    } finally {
      if (mounted) setState(() => _loading = false);
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

  /// 从电脑上删掉这几首的本地文件。
  ///
  /// **只动电脑上那份**——iPod 上已经同步进去的歌不受影响。对话框里必须
  /// 写明这一点，不然用户会以为"删了就没了"而不敢清。
  ///
  /// 删本地文件是**破坏性**的（想再听要重新下载，又是网易云请求），
  /// 所以先弹确认。空集也会被后端拒（400），这里先挡住省一次往返。
  Future<void> _removeLocal(List<int> ids) async {
    if (ids.isEmpty) {
      _toast('还没有选任何歌', error: true);
      return;
    }

    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('删除 ${ids.length} 首的本地文件'),
        content: SizedBox(
          width: 470,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              const Text(
                '删的是电脑上那份。iPod 上已经同步进去的歌不受影响。',
                style: TextStyle(fontSize: 13.5, height: 1.7),
              ),
              const SizedBox(height: 8),
              const Text(
                '其中本地确实有文件的会被删掉，本地没有的自动跳过。'
                '以后再想听要重新下载（会再走一次网易云请求）。',
                style: TextStyle(fontSize: 12.5, height: 1.7),
              ),
            ],
          ),
        ),
        actions: <Widget>[
          // 「取消」放前面：手滑按回车不该删文件
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: Theme.of(ctx).colorScheme.error,
            ),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: Text('删除 ${ids.length} 首'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    try {
      await _api.removeCacheEntries(ids);
      _toast('已加入队列：删除本地 ${ids.length} 首');
      _clearSelection();
    } catch (e) {
      _toast(e.toString(), error: true);
    }
  }

  /// 预览 → 确认 → 提交作业。
  ///
  /// [wholePlaylist] 为 true 时处理整单（`song_ids` 传 null）。
  /// [onlyIds] 是给右键菜单用的"只处理这几首"——不给就按界面上的勾选算。
  /// 特意做成参数而不是临时改 `_selected`：改界面状态会有副作用
  /// （操作条突然冒出来、勾选被清掉），右键点一下不该动整页。
  Future<void> _start({
    required bool push,
    required bool wholePlaylist,
    List<int>? onlyIds,
  }) async {
    final Set<int>? ids = wholePlaylist
        ? null
        : (onlyIds == null ? _selection : onlyIds.toSet());
    if (!wholePlaylist && ids == null) {
      _toast('还没有选任何歌。要处理整个歌单请点「${push ? '同步' : '下载'}整个歌单」。', error: true);
      return;
    }

    PlanPreview plan;
    try {
      plan = await _api.playlistPlan(
        widget.playlist.id,
        push: push,
        songIds: ids,
      );
    } catch (e) {
      _toast(e.toString(), error: true);
      return;
    }
    if (!mounted) return;

    if (plan.loading) {
      // 后端没在等待窗口内算完（队列前面有别的活）。这时候数字全是 0，
      // **不能当成"没什么要下的"**——那会让用户以为不用下了。
      _toast(plan.message.isEmpty ? '正在规划（前面有作业在排队），稍等一下再点' : plan.message);
      return;
    }
    if (plan.needsFetch == 0 && plan.toDownload == 0) {
      // 说清楚**为什么**没事可做。只说"都已经就绪"的话，用户在
      // "删了本地文件想重新下载"的场景下会彻底懵：本地明明没有。
      final why = plan.skipReasons.entries
          .map((e) => '${e.key} ${e.value} 首')
          .join('、');
      _toast(why.isEmpty ? '选中的曲目都已经就绪，没有要处理的' : '没有要处理的：$why');
      return;
    }

    final scopeText = wholePlaylist ? '整个歌单' : '选中的 ${ids!.length} 首';
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('${push ? '同步到 iPod' : '下载到本地'} · $scopeText'),
        content: SizedBox(
          width: 470,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(
                '歌单：${widget.playlist.name}',
                style: const TextStyle(fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 12),
              _kv('本次范围', scopeText),
              _kv('要处理', '${plan.toDownload} 首'),
              _kv('本地已有（不用再下）', '${plan.toDownload - plan.needsFetch} 首'),
              _kv('需要下载', '${plan.needsFetch} 首'),
              if (plan.alreadyReady > 0)
                _kv('已经就绪（可跳过）', '${plan.alreadyReady} 首'),
              for (final entry in plan.skipReasons.entries)
                _kv('　· ${entry.key}', '${entry.value} 首'),
              if (plan.unavailable > 0)
                _kv('拿不到（多半无版权）', '${plan.unavailable} 首'),
              _kv('预计体积', '约 ${plan.estimatedMb.toStringAsFixed(0)} MB'),
              if (plan.preview.isNotEmpty) ...<Widget>[
                const SizedBox(height: 14),
                Text(
                  '将要下载：',
                  style: TextStyle(
                    fontSize: 12,
                    color: Theme.of(ctx).colorScheme.onSurfaceVariant,
                  ),
                ),
                const SizedBox(height: 4),
                for (final line in plan.preview.take(6))
                  Text('· $line', style: const TextStyle(fontSize: 12)),
                if (plan.preview.length > 6)
                  Text(
                    '… 共 ${plan.preview.length} 首',
                    style: const TextStyle(fontSize: 12),
                  ),
              ],
              if (push) ...<Widget>[
                const SizedBox(height: 14),
                const Notice(
                  icon: Icons.warning_amber_outlined,
                  text: '写入时会整库重写 iPod 数据库，期间不要拔设备。',
                  color: StatusColors.warn,
                ),
              ],
            ],
          ),
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: Text(
              push ? '开始同步 ${plan.needsFetch} 首' : '开始下载 ${plan.needsFetch} 首',
            ),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    // await 之前先取出来：后面再碰 context 会踩
    // "BuildContext across async gaps"
    final appState = context.read<AppState>();
    try {
      await _api.downloadPlaylist(widget.playlist.id, push: push, songIds: ids);
      _toast('已加入队列：${push ? '同步' : '下载'} $scopeText');
      await appState.refreshNow();
      // 下完之后状态会变（未下载 → 已下载）。以前是拍脑袋等 5 秒再刷，
      // 现在由 AppState 的 refreshSignal 在作业**真的完成**时通知，
      // 准而且所有页面同步刷。见 RefreshSignal 的说明。
    } catch (e) {
      _toast(e.toString(), error: true);
    }
  }

  Widget _kv(String key, String value) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 2),
    child: Row(
      children: <Widget>[
        Expanded(child: Text(key, style: const TextStyle(fontSize: 12.5))),
        Text(
          value,
          style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w500),
        ),
      ],
    ),
  );

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final songs = _songs;

    return CallbackShortcuts(
      bindings: <ShortcutActivator, VoidCallback>{
        const SingleActivator(LogicalKeyboardKey.escape): _clearSelection,
      },
      child: Focus(
        autofocus: true,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Padding(
              padding: const EdgeInsets.fromLTRB(18, 14, 18, 10),
              child: Row(
                children: <Widget>[
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: <Widget>[
                        Text(
                          widget.playlist.name,
                          style: const TextStyle(
                            fontSize: 16,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        const SizedBox(height: 3),
                        if (songs != null && !songs.loading)
                          Text(
                            '共 ${songs.total} 首'
                            '${songs.filter == SongFilter.all ? '' : '（当前筛选 ${songs.filtered} 首）'}'
                            // ★ 两个维度分开报，不再混成三个互斥的桶。
                            //   混着报的时候，界面上永远看不出"设备上有、
                            //   本地没留"这种情况——而那正是要重新下的那批。
                            ' · 本地：已下 ${songs.localOk}'
                            ' / 缺 ${songs.localMissing}'
                            ' · iPod：'
                            '${songs.deviceUnknown ? '未插设备（状态未知）' : '已有 ${songs.onIpod} / 缺 ${songs.offIpod}'}',
                            style: TextStyle(
                              fontSize: 12,
                              color: scheme.onSurfaceVariant,
                            ),
                          )
                        else
                          Text(
                            '${widget.playlist.trackCount} 首',
                            style: TextStyle(
                              fontSize: 12,
                              color: scheme.onSurfaceVariant,
                            ),
                          ),
                      ],
                    ),
                  ),
                  const SizedBox(width: 10),
                  SizedBox(
                    width: 190,
                    child: TextField(
                      controller: _search,
                      decoration: InputDecoration(
                        hintText: '搜索歌名 / 艺人',
                        prefixIcon: const Icon(Icons.search, size: 18),
                        isDense: true,
                        border: const OutlineInputBorder(),
                        suffixIcon: _search.text.isEmpty
                            ? null
                            : IconButton(
                                icon: const Icon(Icons.clear, size: 16),
                                onPressed: () {
                                  _search.clear();
                                  unawaited(_load(page: 1));
                                },
                              ),
                      ),
                      // 回车才发请求：每敲一个字就查一次的话，246 首的歌单
                      // 会把请求打成一串
                      onSubmitted: (_) => _load(page: 1),
                    ),
                  ),
                  const SizedBox(width: 10),
                  DropdownButton<SongFilter>(
                    value: _filter,
                    isDense: true,
                    underline: const SizedBox.shrink(),
                    items: <DropdownMenuItem<SongFilter>>[
                      for (final f in SongFilter.values)
                        DropdownMenuItem<SongFilter>(
                          value: f,
                          child: Text(
                            f.label,
                            style: const TextStyle(fontSize: 12.5),
                          ),
                        ),
                    ],
                    onChanged: _loading
                        ? null
                        : (value) {
                            if (value != null) _setFilter(value);
                          },
                  ),
                  const SizedBox(width: 10),
                  OutlinedButton.icon(
                    icon: const Icon(Icons.sync, size: 17),
                    label: const Text('同步整个歌单'),
                    onPressed: songs == null || songs.loading
                        ? null
                        : () => _start(push: true, wholePlaylist: true),
                  ),
                ],
              ),
            ),
            const Divider(height: 1),
            Expanded(child: _body(songs)),
            if (_selected.isNotEmpty) _actionBar(songs),
            if (songs != null && songs.pages > 1) _pager(songs),
          ],
        ),
      ),
    );
  }

  Widget _body(PlaylistSongs? songs) {
    if (_error != null) {
      return Padding(
        padding: const EdgeInsets.all(18),
        child: Notice(
          icon: Icons.error_outline,
          title: '读不到曲目',
          text: _error!,
          color: StatusColors.error,
          action: TextButton(onPressed: () => _load(), child: const Text('重试')),
        ),
      );
    }
    if (songs == null) {
      return const Padding(
        padding: EdgeInsets.all(18),
        child: LoadingLine(text: '正在读取曲目…'),
      );
    }
    if (songs.loading) {
      return Padding(
        padding: const EdgeInsets.all(18),
        child: Notice(
          icon: Icons.hourglass_top,
          title: '正在排队',
          text: songs.message.isEmpty ? '前面有作业在跑，曲目要排在它后面。' : songs.message,
          color: StatusColors.warn,
          action: TextButton(onPressed: () => _load(), child: const Text('重试')),
        ),
      );
    }
    if (songs.songs.isEmpty) {
      return Padding(
        padding: const EdgeInsets.all(18),
        child: Notice(
          icon: Icons.filter_alt_off_outlined,
          text: songs.total == 0
              ? '这个歌单里没有曲目。'
              : '当前筛选（${songs.filter.label}）下没有曲目。换成「全部」看看。',
        ),
      );
    }

    return ListView.builder(
      itemCount: songs.songs.length,
      itemBuilder: (context, index) {
        final song = songs.songs[index];
        final number = (songs.page - 1) * _pageSize + index + 1;
        final checked = _selected.contains(song.id);

        return ContextMenuRegion(
          actions: () => _menuFor(song),
          child: ListTile(
            dense: true,
            selected: checked,
            selectedTileColor: Theme.of(context).colorScheme.primaryContainer
                .withValues(alpha: 0.35),
            leading: SizedBox(
              width: 62,
              child: Row(
                children: <Widget>[
                  // 已经在 iPod 上的不让勾：勾了也没事可做，只会让
                  // "已选 N 首"里混进一堆不用处理的。
                  Checkbox(
                    value: checked,
                    visualDensity: VisualDensity.compact,
                    materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    onChanged: song.selectable ? (_) => _toggle(song.id) : null,
                  ),
                  Text(
                    '$number',
                    style: TextStyle(
                      fontSize: 11.5,
                      color: Theme.of(context).colorScheme.onSurfaceVariant,
                    ),
                  ),
                ],
              ),
            ),
            title: Text(
              song.name,
              style: const TextStyle(fontSize: 13),
              overflow: TextOverflow.ellipsis,
            ),
            subtitle: Text(
              song.artist.isEmpty
                  ? song.album
                  : '${song.artist}${song.album.isEmpty ? '' : ' · ${song.album}'}',
              style: const TextStyle(fontSize: 11.5),
              overflow: TextOverflow.ellipsis,
            ),
            trailing: Row(
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                Text(
                  song.durationText,
                  style: TextStyle(
                    fontSize: 11.5,
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                  ),
                ),
                const SizedBox(width: 10),
                // ★ 两个维度各给一个徽章，不再合成一个词。
                //   以前合成"已同步"，于是"设备上有、本地没留"的歌看起来
                //   不用管——其实点「下载到本地」时它有活干。
                StatusBadge(
                  song.localText,
                  status: song.local ? 'downloaded' : 'pending',
                ),
                const SizedBox(width: 6),
                StatusBadge(
                  song.deviceText,
                  status: switch (song.device) {
                    'on_ipod' => 'on_ipod',
                    'off_ipod' => 'off_ipod',
                    _ => 'unknown',
                  },
                ),
              ],
            ),
            onTap: song.selectable ? () => _toggle(song.id) : null,
          ),
        );
      },
    );
  }

  /// 一首歌的右键菜单。
  ///
  /// ★ **每个动作只看自己那一维**，判据和理由都是分开的：
  ///
  /// * 「下载到本地」只看本地文件在不在 —— 跟 iPod 上有没有**无关**。
  /// * 「同步这一首到 iPod」只看设备 —— 没插设备时置灰并说明，
  ///   而不是含糊地说"没事可做"。
  ///
  /// 以前两个动作共用一个"两边都有了"的判据：一首歌只要在 iPod 上，
  /// 「下载到本地」就跟着一起灰掉——用户明明本地没留文件，想下都点不了。
  /// 这就是"未下载已同步就不能下载"那个 bug 的另一半。
  ///
  /// 置灰而不是隐藏：菜单里直接不显示的话，用户会以为"这个功能没有"，
  /// 而不是"这首不用处理"。
  List<SongAction> _menuFor(SongRow song) {
    // 本地这一维：本地有文件 = 下载没事可做
    final localDone = song.local ? '本地也留着一份' : null;
    // 设备这一维：设备上有 = 同步没事可做；判断不了 = 同步做不了
    final String? deviceDone;
    if (!song.deviceKnown) {
      deviceDone = '没插 iPod，判断不了它上面有没有';
    } else if (song.onIpod) {
      deviceDone = 'iPod 上已经有了';
    } else {
      deviceDone = null;
    }
    return <SongAction>[
      SongAction(
        label: '下载到本地',
        icon: Icons.download_outlined,
        onTap: song.local
            ? null
            : () => _start(
                push: false,
                wholePlaylist: false,
                onlyIds: <int>[song.id],
              ),
        unavailableReason: localDone,
      ),
      SongAction(
        label: '同步这一首到 iPod',
        icon: Icons.sync,
        onTap: deviceDone != null
            ? null
            : () => _start(
                push: true,
                wholePlaylist: false,
                onlyIds: <int>[song.id],
              ),
        unavailableReason: deviceDone,
      ),
      SongAction(
        label: _isSelected(song) ? '取消勾选' : '勾选这一首',
        icon: Icons.check_box_outlined,
        dividerBefore: true,
        // 勾选的意义是"接下来处理它"。两个维度各说各的：
        // 只要**有任何一边还缺**，勾上就有意义。
        onTap: song.selectable ? () => _toggle(song.id) : null,
        unavailableReason: song.selectable ? null : '本地和 iPod 上都有了',
      ),
      SongAction(
        label: '删除本地那份',
        icon: Icons.delete_outline,
        danger: true,
        dividerBefore: true,
        onTap: song.local ? () => _removeLocal(<int>[song.id]) : null,
        unavailableReason: song.local ? null : '本地没有这份文件',
      ),
      SongAction(
        label: '只看未下载的',
        icon: Icons.filter_alt_outlined,
        onTap: () {
          setState(() => _filter = SongFilter.pending);
          unawaited(_load(page: 1));
        },
      ),
      SongAction(
        label: '复制歌名',
        icon: Icons.copy,
        dividerBefore: true,
        onTap: () => copyText(context, song.name, what: '歌名'),
      ),
      SongAction(
        label: '复制「歌名 - 艺人」',
        icon: Icons.link,
        onTap: () => copyText(
          context,
          song.artist.isEmpty ? song.name : '${song.name} - ${song.artist}',
          what: '曲目信息',
        ),
      ),
    ];
  }

  bool _isSelected(SongRow song) => _selected.contains(song.id);

  /// 吸顶操作条。主操作固定在最右，次要控件可横向滚动——
  /// 窗口窄的时候不能让"下载/同步"被挤出屏幕。
  Widget _actionBar(PlaylistSongs? songs) {
    final scheme = Theme.of(context).colorScheme;
    final pageCount = songs?.songs.where((s) => s.selectable).length ?? 0;
    final pending = songs?.pending ?? 0;

    return Container(
      decoration: BoxDecoration(
        color: scheme.primaryContainer.withValues(alpha: 0.45),
        border: Border(top: BorderSide(color: scheme.outlineVariant)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: Row(
        children: <Widget>[
          Flexible(
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Row(
                children: <Widget>[
                  Text(
                    '已选 ${_selected.length} 首',
                    style: const TextStyle(
                      fontSize: 13.5,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(width: 14),
                  TextButton(
                    onPressed: pageCount == 0 ? null : _selectCurrentPage,
                    child: Text('全选本页（$pageCount 首）'),
                  ),
                  // ★ 一等公民：不然 246 首要手点 246 次
                  TextButton(
                    onPressed: _loading || pending == 0
                        ? null
                        : _selectAllPending,
                    child: Text('一键选中所有未下载的（$pending 首）'),
                  ),
                  TextButton(
                    onPressed: _clearSelection,
                    child: const Text('清空选择（Esc）'),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(width: 12),
          OutlinedButton.icon(
            icon: const Icon(Icons.download_outlined, size: 17),
            label: Text('下载选中的 ${_selected.length} 首'),
            onPressed: () => _start(push: false, wholePlaylist: false),
          ),
          const SizedBox(width: 8),
          FilledButton.icon(
            icon: const Icon(Icons.sync, size: 17),
            label: Text('同步选中的 ${_selected.length} 首'),
            onPressed: () => _start(push: true, wholePlaylist: false),
          ),
          const SizedBox(width: 8),
          // 删本地文件排在最后、用红色：它是**破坏性**的，不该跟
          // "下载/同步"挨着放，手滑的代价不一样。
          TextButton.icon(
            icon: const Icon(Icons.delete_outline, size: 17),
            label: const Text('删除本地那份'),
            style: TextButton.styleFrom(
              foregroundColor: Theme.of(context).colorScheme.error,
            ),
            onPressed: () => _removeLocal(_selected.toList()),
          ),
        ],
      ),
    );
  }

  Widget _pager(PlaylistSongs songs) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: scheme.outlineVariant)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: Row(
        children: <Widget>[
          Text(
            '第 ${songs.page} / ${songs.pages} 页'
            '${_selected.isEmpty ? '' : ' · 已选 ${_selected.length} 首（翻页不会清空）'}',
            style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
          ),
          const Spacer(),
          IconButton(
            icon: const Icon(Icons.chevron_left, size: 20),
            onPressed: songs.page <= 1 || _loading
                ? null
                : () => _load(page: songs.page - 1),
          ),
          IconButton(
            icon: const Icon(Icons.chevron_right, size: 20),
            onPressed: songs.page >= songs.pages || _loading
                ? null
                : () => _load(page: songs.page + 1),
          ),
        ],
      ),
    );
  }
}
