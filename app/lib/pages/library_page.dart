/// iPod 音乐管理：iPod **设备上**的曲目列表、批量删除、从外部导入。
///
/// 这里是 iPod 的音乐数据库（iTunesDB）在界面上的样子。跟「下载」页那个
/// "电脑上已下载的音乐"是**两回事**——那个删了不影响 iPod，这个删了
/// 就是真从 iPod 上删歌。两页的说法必须分得清，不然用户会搞混。
///
/// 这一页是**真的会删歌**的地方，所以多选这块写得比别处仔细：
///
/// * **「全选本页」和「全选筛选结果」是两个按钮，各自写明数量**。
///   合成一个"全选"的后果是：用户以为选中了 246 首，实际只选了当前页
///   50 首——或者反过来，以为选了 1 页，实际把整个库选上了。代价是真删歌。
/// * **选择跨页保持**。翻页不该把之前选的清掉。
/// * **删除三步走**：点删除 → 预览（多少首 + 释放多少空间 + 前几首曲名）
///   → 确认按钮文案具体到数量，且默认不聚焦。
/// * **Esc 清空、Ctrl+A 全选**，跟文件管理器的习惯一致。
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../services/file_picker.dart';
import '../state/app_state.dart';
import '../format.dart';
import '../theme.dart';
import '../widgets/common.dart';
import '../widgets/context_menu.dart';
import '../widgets/repair_dialog.dart';

class LibraryPage extends StatefulWidget {
  const LibraryPage({super.key});

  @override
  State<LibraryPage> createState() => _LibraryPageState();
}

class _LibraryPageState extends State<LibraryPage> {
  final TextEditingController _search = TextEditingController();
  final FilePickerService _picker = FilePickerService();

  TrackPage? _page;
  String? _error;
  bool _loading = false;
  String _sort = 'title';
  int _currentPage = 1;
  static const int _pageSize = 100;

  /// 选中的 `db_id`。用 Set 是为了 O(1) 查"这行选没选"——
  /// 列表每帧都要问一次，用 List.contains 在几百行时会明显卡。
  final Set<String> _selected = <String>{};

  /// Shift 范围选的锚点。
  String? _anchor;

  /// 见过的曲目体积（db_id → 字节）。跨页选择时算总体积要用。
  final Map<String, int> _sizes = <String, int>{};

  late final AppState _state = context.read<AppState>();

  ApiClient get _api => context.read<AppState>().api;

  // ── 歌单（iPod **设备上**的播放列表）─────────────────────────────
  //
  // 这些都是设备上的结构，跟「歌单」页那些网易云在线歌单没有关系。
  // 写操作全部走后端作业队列，完成后 refreshSignal 会触发重刷。

  /// 顶部视图模式：false = 全部歌曲，true = 歌单。
  bool _playlistMode = false;

  IpodPlaylistList? _playlists;

  /// 当前选中的歌单（`null` 表示还没选/设备上没有歌单）。
  IpodPlaylist? _current;

  IpodPlaylistTracks? _playlistTracks;
  bool _plLoading = false;
  String? _plError;

  @override
  void initState() {
    super.initState();
    // 作业干完自动重刷：导入、删除、健康检查都走队列，
    // 干完之后 iPod 上的曲目列表就变了。以前是拍脑袋等 5 秒，
    // 现在由 refreshSignal 在作业**真的完成**时通知。
    _state.refreshSignal.addListener(_onJobFinished);
    WidgetsBinding.instance.addPostFrameCallback((_) => unawaited(_load()));
  }

  @override
  void dispose() {
    _state.refreshSignal.removeListener(_onJobFinished);
    _search.dispose();
    super.dispose();
  }

  void _onJobFinished() {
    if (!mounted) return;
    unawaited(_load());
    // 歌单的写操作也走作业队列（新建/改名/删除/增删成员），
    // 完成后同样要重刷——不然界面上还是改动前的样子。
    if (_playlistMode) unawaited(_loadPlaylists());
  }

  Future<void> _load({int? page}) async {
    if (!mounted) return;
    setState(() {
      _loading = true;
      _currentPage = page ?? _currentPage;
    });
    try {
      final result = await _api.tracks(
        search: _search.text.trim(),
        sort: _sort,
        page: _currentPage,
        size: _pageSize,
      );
      if (!mounted) return;
      setState(() {
        _page = result;
        _error = null;
        // 记住见过的体积，跨页算总体积要用
        for (final track in result.tracks) {
          _sizes[track.dbId] = track.size;
        }
      });
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  // ── 选择 ──────────────────────────────────────────────────────────

  void _toggle(TrackRow track) {
    setState(() {
      if (_selected.contains(track.dbId)) {
        _selected.remove(track.dbId);
      } else {
        _selected.add(track.dbId);
      }
      _anchor = track.dbId;
    });
  }

  void _selectOnly(TrackRow track) {
    setState(() {
      _selected
        ..clear()
        ..add(track.dbId);
      _anchor = track.dbId;
    });
  }

  /// Shift 范围选：从锚点到当前行，**全部选中**（不清掉已有的）。
  ///
  /// 不清掉是故意的：用户常先逐个点几首、再用 Shift 补一段。
  void _selectRange(TrackRow track) {
    final tracks = _page?.tracks ?? const <TrackRow>[];
    final anchorIndex = tracks.indexWhere((t) => t.dbId == _anchor);
    final targetIndex = tracks.indexWhere((t) => t.dbId == track.dbId);
    if (anchorIndex < 0 || targetIndex < 0) {
      _selectOnly(track);
      return;
    }
    final from = anchorIndex < targetIndex ? anchorIndex : targetIndex;
    final to = anchorIndex < targetIndex ? targetIndex : anchorIndex;
    setState(() {
      for (var i = from; i <= to; i++) {
        _selected.add(tracks[i].dbId);
      }
    });
  }

  /// 点一行干什么。
  ///
  /// **普通单击 = 勾选/取消勾选这一首**，跟歌单页一模一样。
  ///
  /// 这里以前是"只选这一首"（清掉其它），照着文件管理器的老习惯设计的。
  /// 但行左边画的是**复选框**——用户看到方框就以为点一下是"勾它"，
  /// 结果点第二首时第一首自动取消了，多选根本没法用（用户原话：
  /// "选择一个另一个的选框就会取消"）。而且歌单页同一件事是累加的，
  /// 两页行为不一致，更容易懵。
  ///
  /// 复选框就该是复选框的行为。Ctrl 和单击现在等价（都累加），
  /// Shift 保留范围选。
  void _onRowTap(TrackRow track) {
    if (HardwareKeyboard.instance.isShiftPressed) {
      _selectRange(track);
      return;
    }
    _toggle(track);
  }

  void _selectPage() {
    final tracks = _page?.tracks ?? const <TrackRow>[];
    setState(() => _selected.addAll(tracks.map((t) => t.dbId)));
  }

  /// 全选**筛选结果**。
  ///
  /// 注意这里只能选中"已经加载出来的页"——所以按钮上必须写清数量，
  /// 而且这个数量是筛选总数、不是已加载数。没加载到的那些，
  /// 界面提示用户翻页或者用当前页操作，绝不悄悄扩大范围。
  void _selectAllFiltered() {
    final page = _page;
    if (page == null) return;
    setState(() {
      _selected.addAll(page.tracks.map((t) => t.dbId));
      for (final track in page.tracks) {
        _sizes[track.dbId] = track.size;
      }
    });
    if (page.filtered > page.tracks.length) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            '已选中当前这一页的 ${page.tracks.length} 首'
            '（筛选结果共 ${page.filtered} 首，其余需要翻页后继续选）。',
          ),
          duration: const Duration(seconds: 4),
        ),
      );
    }
  }

  void _clearSelection() => setState(() {
    _selected.clear();
    _anchor = null;
  });

  int get _selectedBytes =>
      _selected.fold<int>(0, (sum, id) => sum + (_sizes[id] ?? 0));

  String get _humanBytes => humanSize(_selectedBytes);

  // ── 导入 ──────────────────────────────────────────────────────────

  Future<void> _import() async {
    List<String> paths;
    try {
      paths = await _picker.pickAudioFiles();
    } catch (e) {
      _toast(e.toString(), error: true);
      return;
    }
    if (paths.isEmpty || !mounted) return;

    _toast('正在分析 ${paths.length} 个文件…');

    ImportPreview preview;
    try {
      preview = await _api.importPreview(paths);
    } catch (e) {
      _toast(e.toString(), error: true);
      return;
    }
    if (!mounted) return;

    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('确认导入'),
        content: SizedBox(
          width: 560,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(
                preview.summary,
                style: const TextStyle(
                  fontSize: 13.5,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 6),
              Text(
                '设备可用空间：${preview.freeText}',
                style: const TextStyle(fontSize: 12.5),
              ),
              const SizedBox(height: 14),
              if (!preview.fits) ...<Widget>[
                const Notice(
                  icon: Icons.sd_card_alert_outlined,
                  title: '设备空间不够',
                  text: '需要拷入的文件比可用空间还大。请先删掉一些歌，或者少选几个文件。',
                  color: StatusColors.error,
                ),
                const SizedBox(height: 10),
              ],
              if (preview.items.isNotEmpty)
                Container(
                  height: 240,
                  width: double.infinity,
                  decoration: BoxDecoration(
                    border: Border.all(
                      color: Theme.of(ctx).colorScheme.outlineVariant,
                    ),
                    borderRadius: BorderRadius.circular(6),
                  ),
                  child: ListView.builder(
                    itemCount: preview.items.length,
                    itemBuilder: (_, index) {
                      final item = preview.items[index];
                      return ListTile(
                        dense: true,
                        title: Text(
                          item.title.isEmpty ? item.name : item.title,
                          style: const TextStyle(fontSize: 12.5),
                          overflow: TextOverflow.ellipsis,
                        ),
                        subtitle: item.reason.isEmpty
                            ? null
                            : Text(
                                item.reason,
                                style: const TextStyle(fontSize: 11),
                                overflow: TextOverflow.ellipsis,
                              ),
                        trailing: StatusBadge(
                          item.actionText,
                          status: item.action,
                        ),
                      );
                    },
                  ),
                ),
            ],
          ),
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: preview.toAdd == 0 || !preview.fits
                ? null
                : () => Navigator.of(ctx).pop(true),
            child: Text('导入 ${preview.toAdd} 首'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    try {
      await _api.importFiles(paths);
      _toast('已加入队列：导入 ${preview.toAdd} 首');
      if (mounted) await context.read<AppState>().refreshNow();
    } catch (e) {
      _toast(e.toString(), error: true);
    }
  }

  // ── 删除 ──────────────────────────────────────────────────────────

  /// 从 iPod 删除。
  ///
  /// [onlyIds] 是给右键菜单用的"只删这一首"——不给就按界面上的勾选算。
  /// 做成参数而不是临时改 `_selected`：改界面状态会有副作用
  /// （操作条冒出来、勾选被清），右键点一下不该动整页。
  Future<void> _remove({List<String>? onlyIds}) async {
    final ids = onlyIds ?? _selected.toList();
    if (ids.isEmpty) return;

    RemovePreview preview;
    try {
      preview = await _api.removePreview(ids);
    } on ApiException catch (e) {
      if (!mounted) return;
      if (e.statusCode == 409) {
        // 409 不是"出错了"，是"这件事本工具不做"（比如要把曲库删空）。
        // 消息里带着原因和替代做法，必须让人**看得完**——塞进 4 秒就消失的
        // SnackBar 等于没说。实测：用户逐首删到只剩 1 首，再删就没反应，
        // 只看到一闪而过的提示，完全不知道发生了什么。
        await _showRefusal(e.message);
      } else {
        _toast(e.message, error: true);
      }
      return;
    } catch (e) {
      _toast(e.toString(), error: true);
      return;
    }
    if (!mounted) return;

    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('确认删除'),
        content: SizedBox(
          width: 520,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(
                '将从 iPod 上删除 ${preview.count} 首，'
                '释放 ${preview.sizeText}。',
                style: const TextStyle(
                  fontSize: 13.5,
                  fontWeight: FontWeight.w600,
                  height: 1.6,
                ),
              ),
              const SizedBox(height: 4),
              Text(
                '删除后库中剩余 ${preview.remaining} 首。',
                style: const TextStyle(fontSize: 12.5),
              ),
              const SizedBox(height: 14),
              for (final item in preview.items.take(10))
                Text(
                  '· ${item.title}'
                  '${item.artist.isEmpty ? '' : ' - ${item.artist}'}',
                  style: const TextStyle(fontSize: 12.5),
                  overflow: TextOverflow.ellipsis,
                ),
              if (preview.items.length > 10)
                Text(
                  '… 等 ${preview.items.length} 首',
                  style: const TextStyle(fontSize: 12.5),
                ),
              const SizedBox(height: 14),
              const Notice(
                icon: Icons.warning_amber_outlined,
                text:
                    '会整库重写 iPod 数据库，期间不要拔设备。这个操作不能撤销'
                    '（可以先到设置里确认设备备份）。',
                color: StatusColors.warn,
              ),
            ],
          ),
        ),
        actions: <Widget>[
          // 「取消」放前面且默认不聚焦：手滑按回车不该删歌
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: Theme.of(ctx).colorScheme.error,
            ),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: Text('确认删除 ${preview.count} 首'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    try {
      // ★ 必须传 `ids`（跟上面预览用的是同一份），不能再用 `_selected`。
      //   预览一首、删另一首的话，"预览==实际"这个保证就破了——
      //   用户看到"将删除《A》"，结果《B》没了。
      await _api.removeTracks(ids, previewId: preview.previewId);
      _toast('已加入队列：删除 ${preview.count} 首');
      _clearSelection();
      if (mounted) await context.read<AppState>().refreshNow();
    } catch (e) {
      _toast(e.toString(), error: true);
    }
  }

  Future<void> _verify() async {
    try {
      await _api.runVerify();
      _toast('已开始健康检查，到「下载」页看结果');
      if (mounted) await context.read<AppState>().refreshNow();
    } catch (e) {
      _toast(e.toString(), error: true);
    }
  }

  /// 打开「数据库修复」：扫出数据库与磁盘对不上的地方，用户勾选后清理。
  ///
  /// 干净地收尾很重要：只有**真的动过手**才刷新列表，否则用户只是打开看一眼
  /// 也会触发一次全量重读（几百首要读标签，白等）。
  Future<void> _repair() async {
    final changed = await showRepairDialog(context, _api);
    if (!mounted || !changed) return;
    _toast('修复完成，正在重新读取设备…');
    await _load();
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

  /// 显示一条"这件事本工具不做"的说明。
  ///
  /// 跟普通报错分开：普通报错是"操作失败了，重试看看"，一闪而过没问题；
  /// 这类是"我们**决定**不做这件事"，里面写着原因和替代做法，用户得
  /// 能读完、能复制。用对话框，不用 SnackBar。
  Future<void> _showRefusal(String message) async {
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        icon: const Icon(Icons.info_outline),
        title: const Text('这个操作没有执行'),
        content: SizedBox(
          width: 520,
          child: SelectableText(
            message,
            style: const TextStyle(fontSize: 13, height: 1.8),
          ),
        ),
        actions: <Widget>[
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('知道了'),
          ),
        ],
      ),
    );
  }

  // ── 歌单操作 ──────────────────────────────────────────────────────

  /// 只把歌单列表读回来，**不动当前选中项、不清勾选**。
  ///
  /// 「全部歌曲」模式下点「加入歌单」也要用到这份列表。那里不能调
  /// `_loadPlaylists`——它会顺带切换选中歌单并清空勾选，等于把用户
  /// 刚勾好的选择抹掉。实测踩到过：不读列表就直接报
  /// "设备上还没有可编辑的歌单"（明明是有的）。
  Future<void> _fetchPlaylistList() async {
    final list = await _api.ipodPlaylists();
    if (!mounted) return;
    setState(() => _playlists = list);
  }

  Future<void> _loadPlaylists({String? selectId}) async {
    setState(() {
      _plLoading = true;
      _plError = null;
    });
    try {
      await _fetchPlaylistList();
      final list = _playlists!;
      if (!mounted) return;

      // 选中项：优先保住当前选的那个（改完名/加完歌之后要停在原地），
      // 它没了再退回"最后一个可编辑的"，都没有就不选。
      final wanted = selectId ?? _current?.playlistId;
      IpodPlaylist? pick;
      for (final item in list.playlists) {
        if (item.playlistId == wanted) {
          pick = item;
          break;
        }
      }
      if (pick == null) {
        for (final item in list.playlists) {
          if (item.playlistId == selectId) pick = item;
        }
      }
      if (pick == null) {
        for (final item in list.playlists) {
          if (item.editable) pick = item;
        }
      }
      if (pick != null) {
        await _selectPlaylist(pick);
      } else {
        setState(() {
          _current = null;
          _playlistTracks = null;
        });
      }
    } catch (e) {
      if (mounted) setState(() => _plError = e.toString());
    } finally {
      if (mounted) setState(() => _plLoading = false);
    }
  }

  Future<void> _selectPlaylist(IpodPlaylist item) async {
    // ★ 切歌单时**必须清空勾选**：`_selected` 里装的是上一个列表的 db_id，
    //   带过去会让「从歌单移除」移错歌——那是真的改用户的歌单内容。
    setState(() {
      _current = item;
      _playlistTracks = null;
      _selected.clear();
      _anchor = null;
    });
    try {
      final tracks = await _api.ipodPlaylistTracks(item.playlistId);
      if (!mounted) return;
      setState(() {
        _playlistTracks = tracks;
        for (final row in tracks.tracks) {
          _sizes[row.dbId] = row.size;
        }
      });
    } catch (e) {
      if (mounted) _toast(e.toString(), error: true);
    }
  }

  /// 让用户输入一个歌单名。取消返回 null。
  Future<String?> _askPlaylistName({
    required String title,
    required String hint,
    String initial = '',
  }) async {
    final controller = TextEditingController(text: initial);
    final result = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: SizedBox(
          width: 420,
          child: TextField(
            controller: controller,
            autofocus: true,
            maxLength: 120,
            decoration: InputDecoration(hintText: hint),
            onSubmitted: (value) => Navigator.of(ctx).pop(value.trim()),
          ),
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(controller.text.trim()),
            child: const Text('确定'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (result == null || result.isEmpty) return null;
    return result;
  }

  Future<void> _createPlaylist() async {
    final numbers = _selected.length;
    final name = await _askPlaylistName(
      title: '新建歌单',
      hint: numbers > 0
          ? '会把当前选中的 $numbers 首一起放进去'
          : '给这个歌单起个名字',
    );
    if (name == null) return;
    try {
      await _api.createIpodPlaylist(name, trackIds: _selected.toList());
      _toast('已提交：新建歌单「$name」');
      _clearSelection();
    } catch (e) {
      await _showRefusal(e.toString());
    }
  }

  Future<void> _renamePlaylist() async {
    final current = _current;
    if (current == null) return;
    final name = await _askPlaylistName(
      title: '歌单改名',
      hint: '新的名字',
      initial: current.name,
    );
    if (name == null || name == current.name) return;
    try {
      await _api.renameIpodPlaylist(current.playlistId, name);
      _toast('已提交：改名为「$name」');
    } catch (e) {
      await _showRefusal(e.toString());
    }
  }

  /// 删除歌单。**两步走**：先预览（后端给令牌），确认后才真删。
  ///
  /// 后端的 `/delete` 没有真实预览过的令牌就直接拒绝——所以这里漏掉确认框
  /// 也删不掉，这是有意的结构性保证。
  Future<void> _deletePlaylist() async {
    final current = _current;
    if (current == null) return;

    IpodPlaylistDeletePreview preview;
    try {
      preview = await _api.ipodPlaylistDeletePreview(current.playlistId);
    } catch (e) {
      await _showRefusal(e.toString());
      return;
    }
    if (!mounted) return;

    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        icon: const Icon(Icons.delete_outline),
        title: Text('删除歌单「${preview.name}」？'),
        content: SizedBox(
          width: 520,
          child: SelectableText(
            '这个歌单里有 ${preview.count} 首。\n\n${preview.note}',
            style: const TextStyle(fontSize: 13, height: 1.8),
          ),
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('取消'),
          ),
          // 危险按钮放右边、文案具体到"歌单"，避免跟"删歌"混淆
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: Theme.of(ctx).colorScheme.error,
            ),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('删除这个歌单'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;

    try {
      await _api.deleteIpodPlaylist(
        current.playlistId,
        previewId: preview.previewId,
      );
      _toast('已提交：删除歌单「${preview.name}」');
      setState(() => _current = null);
    } catch (e) {
      await _showRefusal(e.toString());
    }
  }

  /// 把选中的曲目加进某个歌单（先让用户挑一个）。
  Future<void> _addSelectedToPlaylist() async {
    if (_selected.isEmpty) return;

    // 「全部歌曲」模式进来时歌单列表还没读过（那是切到歌单模式才读的），
    // 按需读一次。不读的话下面会误报"设备上还没有可编辑的歌单"。
    if (_playlists == null) {
      try {
        await _fetchPlaylistList();
      } catch (e) {
        await _showRefusal(e.toString());
        return;
      }
      if (!mounted) return;
    }

    final list = _playlists?.playlists ?? const <IpodPlaylist>[];
    final targets = list.where((p) => p.editable).toList();
    if (targets.isEmpty) {
      _toast('设备上还没有可编辑的歌单，先新建一个。', error: true);
      return;
    }

    final picked = await showDialog<IpodPlaylist?>(
      context: context,
      builder: (ctx) => SimpleDialog(
        title: Text('把选中的 ${_selected.length} 首加进…'),
        children: <Widget>[
          for (final item in targets)
            SimpleDialogOption(
              onPressed: () => Navigator.of(ctx).pop(item),
              child: Row(
                children: <Widget>[
                  Expanded(
                    child: Text(item.name, overflow: TextOverflow.ellipsis),
                  ),
                  const SizedBox(width: 8),
                  Text(
                    '${item.count} 首',
                    style: TextStyle(
                      fontSize: 11.5,
                      color: Theme.of(ctx).colorScheme.onSurfaceVariant,
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
    if (picked == null) return;

    try {
      await _api.editIpodPlaylistTracks(
        picked.playlistId,
        add: _selected.toList(),
      );
      _toast('已提交：加入「${picked.name}」');
      _clearSelection();
      // 加完停在这个歌单上，用户能立刻看到结果
      if (_playlistMode) await _loadPlaylists(selectId: picked.playlistId);
    } catch (e) {
      await _showRefusal(e.toString());
    }
  }

  Future<void> _removeSelectedFromPlaylist() async {
    final current = _current;
    if (current == null || _selected.isEmpty) return;

    try {
      await _api.editIpodPlaylistTracks(
        current.playlistId,
        remove: _selected.toList(),
      );
      _toast('已提交：从「${current.name}」移出 ${_selected.length} 首');
      _clearSelection();
    } catch (e) {
      await _showRefusal(e.toString());
    }
  }

  // ── 界面 ──────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final page = _page;
    final busy = _loading || (context.watch<AppState>().jobs?.hasWork ?? false);

    return CallbackShortcuts(
      bindings: <ShortcutActivator, VoidCallback>{
        // 跟文件管理器的习惯一致：Esc 清空、Ctrl+A 全选
        const SingleActivator(LogicalKeyboardKey.escape): _clearSelection,
        const SingleActivator(LogicalKeyboardKey.keyA, control: true):
            _selectAllFiltered,
      },
      child: Focus(
        autofocus: true,
        child: Column(
          children: <Widget>[
            _toolbar(page, busy),
            const Divider(height: 1),
            Expanded(child: _playlistMode ? _playlistBody() : _body(page)),
            if (_selected.isNotEmpty)
              _playlistMode ? _playlistActionBar() : _actionBar(page),
            // 分页只对「全部歌曲」有意义：歌单里的曲目是一次取完的
            if (!_playlistMode && page != null && page.pages > 1) _pager(page),
          ],
        ),
      ),
    );
  }

  Widget _toolbar(TrackPage? page, bool busy) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 10),
      child: Row(
        children: <Widget>[
          // 左半边（模式切换 + 搜索 + 排序）**可横向滚动**。
          //
          // ★ 768px 窄窗口下这一条放不下——加了模式切换后实测溢出 127px。
          //   处理方式跟下面操作栏一致：让它滚，而不是把右边的「导入音乐」
          //   挤出去。用户找不到主操作比"要多滑一下"糟得多。
          Flexible(
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Row(
                children: <Widget>[
          // 视图模式。放在最前面：它是"这一页在看什么"的开关，
          // 用户第一眼就该看到，而不是藏在别处。
          SegmentedButton<bool>(
            segments: const <ButtonSegment<bool>>[
              ButtonSegment<bool>(
                value: false,
                label: Text('全部歌曲'),
                icon: Icon(Icons.library_music_outlined, size: 16),
              ),
              ButtonSegment<bool>(
                value: true,
                label: Text('歌单'),
                icon: Icon(Icons.queue_music_outlined, size: 16),
              ),
            ],
            selected: <bool>{_playlistMode},
            showSelectedIcon: false,
            onSelectionChanged: (value) => _switchMode(value.first),
          ),
          const SizedBox(width: 14),
          if (!_playlistMode) ...<Widget>[
          SizedBox(
            width: 260,
            child: TextField(
              controller: _search,
              decoration: InputDecoration(
                hintText: '搜索曲名 / 艺人 / 专辑',
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
              onSubmitted: (_) => _load(page: 1),
            ),
          ),
          const SizedBox(width: 8),
          OutlinedButton(
            onPressed: () => _load(page: 1),
            child: const Text('搜索'),
          ),
          const SizedBox(width: 16),
          if (page != null) ...<Widget>[
            const Text('排序', style: TextStyle(fontSize: 12.5)),
            const SizedBox(width: 6),
            DropdownButton<String>(
              value: _sort,
              isDense: true,
              underline: const SizedBox.shrink(),
              items: <DropdownMenuItem<String>>[
                for (final option in page.sorts)
                  DropdownMenuItem<String>(
                    value: option.value,
                    child: Text(
                      option.label,
                      style: const TextStyle(fontSize: 12.5),
                    ),
                  ),
              ],
              onChanged: (value) {
                if (value == null) return;
                setState(() => _sort = value);
                unawaited(_load(page: 1));
              },
            ),
          ],
          ],  // end if (!_playlistMode)
                ],
              ),
            ),
          ),
          const SizedBox(width: 12),
          IconButton(
            icon: const Icon(Icons.refresh, size: 18),
            tooltip: '重新读取',
            onPressed: _loading
                ? null
                : (_playlistMode ? () => _loadPlaylists() : () => _load()),
          ),
          const SizedBox(width: 6),
          OutlinedButton.icon(
            icon: const Icon(Icons.monitor_heart_outlined, size: 16),
            label: const Text('健康检查'),
            onPressed: busy ? null : _verify,
          ),
          const SizedBox(width: 8),
          // 「健康检查」告诉你**哪儿不对**，「数据库修复」负责**把它修好**。
          // 两个挨着放：看完报告正好顺手修。
          OutlinedButton.icon(
            icon: const Icon(Icons.build_outlined, size: 16),
            label: const Text('数据库修复'),
            onPressed: busy ? null : _repair,
          ),
          const SizedBox(width: 8),
          FilledButton.icon(
            icon: const Icon(Icons.add, size: 17),
            label: const Text('导入音乐'),
            onPressed: busy ? null : _import,
          ),
        ],
      ),
    );
  }

  Widget _body(TrackPage? page) {
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
    if (page == null) {
      return const Padding(
        padding: EdgeInsets.all(18),
        child: LoadingLine(text: '正在读取 iPod 上的曲目…'),
      );
    }
    if (page.total == 0) {
      return const Padding(
        padding: EdgeInsets.all(18),
        child: Notice(
          icon: Icons.sd_storage_outlined,
          title: 'iPod 上还没有歌',
          text:
              '可以点右上角「导入音乐」从电脑上选文件拷进去，'
              '或者到歌单页挑一个歌单同步。',
        ),
      );
    }
    if (page.tracks.isEmpty) {
      return Padding(
        padding: const EdgeInsets.all(18),
        child: Notice(
          icon: Icons.search_off,
          text: '没有匹配「${_search.text}」的曲目。共 ${page.total} 首。',
        ),
      );
    }

    return ListView.builder(
      itemCount: page.tracks.length,
      itemBuilder: (context, index) => _trackTile(page.tracks[index]),
    );
  }

  /// 一行曲目。**两种模式共用**——同一个"曲目"在「全部歌曲」和「歌单」里
  /// 长得不一样的话，改一处必然漏另一处。
  Widget _trackTile(TrackRow track) {
    final selected = _selected.contains(track.dbId);
    return ContextMenuRegion(
      actions: () => _menuFor(track),
      child: ListTile(
        dense: true,
        selected: selected,
        selectedTileColor: Theme.of(context).colorScheme.primaryContainer
            .withValues(alpha: 0.35),
        leading: Icon(
          selected ? Icons.check_box : Icons.check_box_outline_blank,
          size: 18,
          color: selected
              ? Theme.of(context).colorScheme.primary
              : Theme.of(context).colorScheme.outline,
        ),
        title: Text(
          track.title,
          style: const TextStyle(fontSize: 13),
          overflow: TextOverflow.ellipsis,
        ),
        subtitle: Text(
          track.artist.isEmpty
              ? track.album
              : '${track.artist}${track.album.isEmpty ? '' : ' · ${track.album}'}',
          style: const TextStyle(fontSize: 11.5),
          overflow: TextOverflow.ellipsis,
        ),
        trailing: Row(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Text(
              track.lengthText,
              style: TextStyle(
                fontSize: 11.5,
                color: Theme.of(context).colorScheme.onSurfaceVariant,
              ),
            ),
            const SizedBox(width: 12),
            SizedBox(
              width: 74,
              child: Text(
                track.sizeText,
                textAlign: TextAlign.right,
                style: const TextStyle(fontSize: 11.5),
              ),
            ),
          ],
        ),
        onTap: () => _onRowTap(track),
      ),
    );
  }

  /// 一首 iPod 曲目的右键菜单。
  ///
  /// 删除这条**仍然走完整的预览流程**（`_remove` 里会先拿预览再弹确认）——
  /// 在菜单里点了一次不等于确认过要从设备上删歌。
  List<SongAction> _menuFor(TrackRow track) => <SongAction>[
    SongAction(
      label: '从 iPod 删除',
      icon: Icons.delete_outline,
      danger: true,
      onTap: () => _remove(onlyIds: <String>[track.dbId]),
    ),
    SongAction(
      label: _selected.contains(track.dbId) ? '取消勾选' : '勾选这一首',
      icon: Icons.check_box_outlined,
      onTap: () => _toggle(track),
    ),
    SongAction(
      label: '复制歌名',
      icon: Icons.copy,
      dividerBefore: true,
      onTap: () => copyText(context, track.title, what: '歌名'),
    ),
    SongAction(
      label: '复制「歌名 - 艺人」',
      icon: Icons.link,
      onTap: () => copyText(
        context,
        track.artist.isEmpty ? track.title : '${track.title} - ${track.artist}',
        what: '曲目信息',
      ),
    ),
    SongAction(
      label: '按这个艺人筛选',
      icon: Icons.filter_alt_outlined,
      dividerBefore: true,
      onTap: track.artist.isEmpty
          ? null
          : () {
              _search.text = track.artist;
              unawaited(_load(page: 1));
            },
      unavailableReason: track.artist.isEmpty ? '这首歌没有艺人信息' : null,
    ),
  ];

  /// 吸顶操作条。选中之后一直贴着底部，不用滚回去找按钮。
  Widget _actionBar(TrackPage? page) {
    final scheme = Theme.of(context).colorScheme;
    final pageCount = page?.tracks.length ?? 0;
    final filtered = page?.filtered ?? 0;

    return Container(
      decoration: BoxDecoration(
        color: scheme.primaryContainer.withValues(alpha: 0.45),
        border: Border(top: BorderSide(color: scheme.outlineVariant)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      // 窄窗口下这一条会放不下（实测 768px 宽溢出 127px）。所以：
      // **主操作固定在最右**，选择信息和几个次要按钮可横向滚动。
      // 反过来（让删除按钮被挤出去）是绝对不能接受的——用户找不到删除，
      // 或者更糟：以为删了其实没删。
      child: Row(
        children: <Widget>[
          Flexible(
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Row(
                children: <Widget>[
                  Text(
                    '已选 ${_selected.length} 首 · $_humanBytes',
                    style: const TextStyle(
                      fontSize: 13.5,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(width: 16),
                  // ★ 两个按钮分开写数量。合成一个"全选"的话，
                  //   用户以为选了整个库、实际只选了当前页，代价是真删歌。
                  TextButton(
                    onPressed: pageCount == 0 ? null : _selectPage,
                    child: Text('全选本页（$pageCount 首）'),
                  ),
                  TextButton(
                    onPressed: filtered == 0 ? null : _selectAllFiltered,
                    child: Text('全选筛选结果（$filtered 首）'),
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
          // ★ 只放图标（带 tooltip）。带文字的版本会把左边的「全选筛选结果」
          //   挤出可视区——实测就是这个后果：用户在窄窗口里点不到它。
          //   这个文件里已经有一条同类教训（操作栏溢出 127px），原则一样：
          //   **宁可少一个字的标签，也不能让别的按钮够不着**。
          //   歌单模式那边空间够，用的是带文字的版本。
          IconButton(
            icon: const Icon(Icons.playlist_add, size: 19),
            tooltip: '把选中的 ${_selected.length} 首加入歌单',
            onPressed: _addSelectedToPlaylist,
          ),
          FilledButton.icon(
            icon: const Icon(Icons.delete_outline, size: 17),
            style: FilledButton.styleFrom(backgroundColor: scheme.error),
            label: Text('删除选中的 ${_selected.length} 首'),
            onPressed: _remove,
          ),
        ],
      ),
    );
  }

  void _switchMode(bool playlists) {
    if (_playlistMode == playlists) return;
    setState(() {
      _playlistMode = playlists;
      // 换视图必须清空勾选：两种模式下的曲目集合不同，带着走会误操作
      _selected.clear();
      _anchor = null;
    });
    if (playlists) unawaited(_loadPlaylists());
  }

  // ── 歌单视图 ──────────────────────────────────────────────────────

  Widget _playlistBody() {
    if (_plError != null) {
      return Padding(
        padding: const EdgeInsets.all(18),
        child: Notice(
          icon: Icons.error_outline,
          title: '读不到歌单',
          text: _plError!,
          color: StatusColors.error,
          action: TextButton(
            onPressed: () => _loadPlaylists(),
            child: const Text('重试'),
          ),
        ),
      );
    }
    if (_playlists == null) {
      return const Padding(
        padding: EdgeInsets.all(18),
        child: LoadingLine(text: '正在读取 iPod 上的歌单…'),
      );
    }

    final tracks = _playlistTracks;
    return Column(
      children: <Widget>[
        _playlistChips(),
        const Divider(height: 1),
        Expanded(
          child: tracks == null
              ? const Padding(
                  padding: EdgeInsets.all(18),
                  child: LoadingLine(text: '正在读取歌单里的曲目…'),
                )
              : tracks.tracks.isEmpty
                  ? Padding(
                      padding: const EdgeInsets.all(18),
                      child: Notice(
                        icon: Icons.queue_music_outlined,
                        title: '「${tracks.name}」还是空的',
                        text: _current?.editable == false
                            ? (_current?.readonlyReason ?? '')
                            : '切到「全部歌曲」勾选几首，再点下面的「加入歌单…」。',
                      ),
                    )
                  : ListView.builder(
                      itemCount: tracks.tracks.length,
                      itemBuilder: (context, index) =>
                          _trackTile(tracks.tracks[index]),
                    ),
        ),
      ],
    );
  }

  /// 横向的歌单选择条。一个歌单一个 chip，点一下切换。
  Widget _playlistChips() {
    final list = _playlists!;
    final scheme = Theme.of(context).colorScheme;
    final current = _current;

    return Container(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
      child: Row(
        children: <Widget>[
          Expanded(
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Row(
                children: <Widget>[
                  for (final item in list.playlists)
                    Padding(
                      padding: const EdgeInsets.only(right: 6),
                      child: Tooltip(
                        // 不能编辑的要把原因说出来，不能只是"点了没反应"
                        message: item.editable
                            ? '${item.datasetText}播放列表 · ${item.count} 首'
                            : item.readonlyReason,
                        child: ChoiceChip(
                          selected: item.playlistId == current?.playlistId,
                          onSelected: (_) => unawaited(_selectPlaylist(item)),
                          avatar: item.editable
                              ? null
                              : Icon(
                                  Icons.lock_outline,
                                  size: 14,
                                  color: scheme.onSurfaceVariant,
                                ),
                          label: Text(
                            '${item.dataset == 'mhlp' ? item.name : '${item.name} · ${item.datasetText}'}  ${item.count}',
                            style: const TextStyle(fontSize: 12.5),
                          ),
                        ),
                      ),
                    ),
                  Padding(
                    padding: const EdgeInsets.only(right: 6),
                    child: ActionChip(
                      avatar: const Icon(Icons.add, size: 15),
                      label: const Text('新建歌单', style: TextStyle(fontSize: 12.5)),
                      // 读歌单的过程中不让点：这些操作都要先知道"现在有哪些歌单"
                      // （比如新建要查重名），拿旧数据去写会撞车
                      onPressed: _plLoading ? null : _createPlaylist,
                    ),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(width: 8),
          // 改名/删除固定在最右：歌单多了以后横向滚动条会把它们挤出去，
          // 而"找不到删除按钮"比"看不到某个歌单"糟得多
          TextButton.icon(
            icon: const Icon(Icons.drive_file_rename_outline, size: 16),
            label: const Text('改名'),
            onPressed: (current?.editable ?? false) && !_plLoading
                ? _renamePlaylist
                : null,
          ),
          TextButton.icon(
            icon: const Icon(Icons.delete_outline, size: 16),
            style: TextButton.styleFrom(foregroundColor: scheme.error),
            label: const Text('删除歌单'),
            onPressed: (current?.editable ?? false) && !_plLoading
                ? _deletePlaylist
                : null,
          ),
        ],
      ),
    );
  }

  Widget _playlistActionBar() {
    final scheme = Theme.of(context).colorScheme;
    final current = _current;
    final editable = current?.editable ?? false;

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
                    '已选 ${_selected.length} 首 · $_humanBytes',
                    style: const TextStyle(
                      fontSize: 13.5,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(width: 16),
                  TextButton(
                    onPressed: _playlistTracks?.tracks.isEmpty ?? true
                        ? null
                        : () => setState(() {
                              _selected
                                ..clear()
                                ..addAll(
                                  _playlistTracks!.tracks.map((t) => t.dbId),
                                );
                            }),
                    child: Text('全选本歌单（${_playlistTracks?.tracks.length ?? 0} 首）'),
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
            icon: const Icon(Icons.playlist_add, size: 17),
            label: const Text('加入其它歌单…'),
            onPressed: _addSelectedToPlaylist,
          ),
          const SizedBox(width: 8),
          FilledButton.icon(
            icon: const Icon(Icons.playlist_remove, size: 17),
            label: Text('从「${current?.name ?? ''}」移出 ${_selected.length} 首'),
            onPressed: editable ? _removeSelectedFromPlaylist : null,
          ),
        ],
      ),
    );
  }

  Widget _pager(TrackPage page) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: scheme.outlineVariant)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: Row(
        children: <Widget>[
          Text(
            '第 ${page.page} / ${page.pages} 页 · 共 ${page.total} 首'
            '${page.filtered != page.total ? '（筛选出 ${page.filtered} 首）' : ''}',
            style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
          ),
          const Spacer(),
          IconButton(
            icon: const Icon(Icons.chevron_left, size: 20),
            onPressed: page.page <= 1 || _loading
                ? null
                : () => _load(page: page.page - 1),
          ),
          IconButton(
            icon: const Icon(Icons.chevron_right, size: 20),
            onPressed: page.page >= page.pages || _loading
                ? null
                : () => _load(page: page.page + 1),
          ),
        ],
      ),
    );
  }
}
