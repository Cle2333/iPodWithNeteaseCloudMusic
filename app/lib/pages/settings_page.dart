/// 设置页：账号、同步参数、缓存、设备信息、后端调试。
///
/// 这一页承担了两件容易被忽略但很要紧的事：
///
/// * **设备信息要分清"可改"和"只读"**。设备名存在 iTunesDB 里（能改），
///   序列号和 FireWire GUID 来自 Device/SysInfo（硬件信息，改不了）。
///   不标出来的话用户会以为都能改，然后到处找不到入口。
/// * **后端是自动拉起的子进程，用户看不见它的控制台**。所以这里有完整
///   的调试面板：实时日志、环境信息、环境自检、重启。没有它，出问题只能猜。
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../services/backend.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/common.dart';
import '../widgets/log_view.dart';
import '../services/shell.dart';
import '../widgets/job_schedule.dart';
import '../widgets/qr_login_dialog.dart';

class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key});

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  List<AccountItem>? _accounts;
  AppSettings? _settings;
  CacheInfo? _cache;
  EnvInfo? _env;
  String? _error;
  bool _busy = false;

  final GlobalKey<LogViewState> _logKey = GlobalKey<LogViewState>();

  ApiClient get _api => context.read<AppState>().api;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      unawaited(_loadAll());
    });
  }

  Future<void> _loadAll() async {
    if (!mounted) return;
    setState(() => _busy = true);
    final api = _api;
    try {
      final results = await Future.wait(<Future<Object>>[
        api.accounts(),
        api.settings(),
        api.cache(),
        api.env(),
      ]);
      if (!mounted) return;
      setState(() {
        _accounts = results[0] as List<AccountItem>;
        _settings = results[1] as AppSettings;
        _cache = results[2] as CacheInfo;
        _env = results[3] as EnvInfo;
        _error = null;
      });
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _busy = false);
    }
    // 设备信息走自己的加载（比较重，失败也不该拖垮整页）
    if (mounted) await context.read<AppState>().refreshDevice();
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

  Future<void> _run(Future<String> Function() action) async {
    setState(() => _busy = true);
    try {
      final message = await action();
      _toast(message);
      await _loadAll();
      if (mounted) await context.read<AppState>().refreshNow();
    } catch (e) {
      _toast(e.toString(), error: true);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();

    return Stack(
      children: <Widget>[
        ListView(
          padding: const EdgeInsets.all(18),
          children: <Widget>[
            if (_error != null) ...<Widget>[
              Notice(
                icon: Icons.error_outline,
                title: '有些设置没读出来',
                text: _error!,
                color: StatusColors.error,
                action: TextButton(
                  onPressed: _loadAll,
                  child: const Text('重试'),
                ),
              ),
              const SizedBox(height: 18),
            ],
            const SectionTitle(
              title: '网易云账号',
              subtitle: 'cookie 按账号分开存，切换不会互相覆盖',
            ),
            const SizedBox(height: 4),
            _AccountSection(
              accounts: _accounts,
              busy: _busy,
              onLogin: () async {
                final ok = await showLoginDialog(context, _api);
                if (ok) {
                  _toast('登录成功');
                  await _loadAll();
                }
              },
              onUse: (uid) => _run(() => _api.useAccount(uid)),
              onRemove: (uid, nickname) async {
                final confirmed = await _confirm(
                  title: '移除账号',
                  body:
                      '确定要移除「$nickname」的登录信息吗？\n'
                      '只是删掉本机存的 cookie，不影响你的网易云账号本身。',
                  confirmText: '移除',
                );
                if (confirmed) await _run(() => _api.removeAccount(uid));
              },
            ),
            const SizedBox(height: 26),
            const SectionTitle(title: '同步设置', subtitle: '这两项有安全含义，后端也会再兜一道'),
            const SizedBox(height: 4),
            _SyncSection(
              settings: _settings,
              busy: _busy,
              onQuality: (value) =>
                  _run(() => _api.updateSettings(quality: value)),
              onInterval: (value) =>
                  _run(() => _api.updateSettings(minInterval: value)),
            ),
            const SizedBox(height: 26),
            const SectionTitle(
              title: '本地缓存',
              subtitle: '下载到本地的文件；清掉不会影响 iPod 上已有的歌',
            ),
            const SizedBox(height: 4),
            _CacheSection(
              cache: _cache,
              busy: _busy,
              onClear: () async {
                final confirmed = await _confirm(
                  title: '清空缓存',
                  body:
                      '会删掉本地下好的文件（${_cache?.sizeText ?? ''}）'
                      '和对应的下载记录。\n'
                      'iPod 上已有的歌不受影响，但下次同步要重新下载。',
                  confirmText: '清空',
                );
                if (confirmed) await _run(() => _api.clearCache());
              },
            ),
            const SizedBox(height: 26),
            SectionTitle(
              title: '设备信息',
              subtitle: '带 ✏️ 的可以改；带 🔒 的是硬件信息，只读',
              action: IconButton(
                icon: const Icon(Icons.refresh, size: 20),
                tooltip: '重新读取',
                onPressed: state.loading ? null : state.refreshDevice,
              ),
            ),
            const SizedBox(height: 4),
            if (state.loading && state.device == null)
              const LoadingLine(text: '正在读取设备信息…')
            else
              _DeviceSection(info: state.device),
            const SizedBox(height: 26),
            _BackendSection(
              env: _env,
              logKey: _logKey,
              api: _api,
              jobs: context.watch<AppState>().jobs,
              onExport: _export,
              onDoctor: () async {
                // 先把 AppState 取出来：await 之后再碰 context 会踩
                // "BuildContext across async gaps"，而且这时候页面可能已经不在树上了
                final appState = context.read<AppState>();
                try {
                  await _api.runDoctor();
                  _toast('已开始环境自检，看下面的日志');
                  _logKey.currentState?.refreshNow();
                  await appState.refreshNow();
                } catch (e) {
                  _toast(e.toString(), error: true);
                }
              },
            ),
            const SizedBox(height: 30),
          ],
        ),
        if (_busy)
          const Positioned(
            top: 0,
            left: 0,
            right: 0,
            child: LinearProgressIndicator(minHeight: 2),
          ),
      ],
    );
  }

  /// 导出诊断记录。跟下载页那个是同一个接口——出问题时手边哪个页面
  /// 方便就从哪个导。
  Future<void> _export() async {
    setState(() => _busy = true);
    try {
      final result = await _api.exportDiagnostics();
      if (!mounted) return;
      await showDialog<void>(
        context: context,
        builder: (ctx) => AlertDialog(
          title: const Text('已导出诊断记录'),
          content: SizedBox(
            width: 520,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                const Text(
                  '把这个文件发出来就能定位问题——里面有环境、设备、设置、'
                  '每个作业干了什么、逐首歌的结果，还有完整日志。',
                  style: TextStyle(fontSize: 12.5, height: 1.6),
                ),
                const SizedBox(height: 14),
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: Theme.of(ctx).colorScheme.surfaceContainerHighest,
                    borderRadius: BorderRadius.circular(6),
                  ),
                  child: SelectableText(
                    result.path,
                    style: const TextStyle(
                      fontSize: 12,
                      fontFamily: 'Consolas',
                    ),
                  ),
                ),
                const SizedBox(height: 10),
                Text(
                  '${result.sizeText} · ${result.lines} 行'
                  ' · 含 ${result.jobs} 个作业记录',
                  style: TextStyle(
                    fontSize: 12,
                    color: Theme.of(ctx).colorScheme.onSurfaceVariant,
                  ),
                ),
              ],
            ),
          ),
          actions: <Widget>[
            TextButton.icon(
              icon: const Icon(Icons.copy, size: 16),
              label: const Text('复制路径'),
              onPressed: () async {
                await Clipboard.setData(ClipboardData(text: result.path));
                if (ctx.mounted) Navigator.of(ctx).pop();
                _toast('路径已复制到剪贴板');
              },
            ),
            TextButton.icon(
              icon: const Icon(Icons.folder_open, size: 16),
              label: const Text('打开所在文件夹'),
              onPressed: () async {
                final ok = await revealInExplorer(result.path);
                if (!ctx.mounted) return;
                Navigator.of(ctx).pop();
                if (!ok) _toast('打不开文件夹', error: true);
              },
            ),
            FilledButton(
              onPressed: () => Navigator.of(ctx).pop(),
              child: const Text('知道了'),
            ),
          ],
        ),
      );
    } catch (e) {
      _toast(e.toString(), error: true);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<bool> _confirm({
    required String title,
    required String body,
    required String confirmText,
  }) async {
    final result = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: Text(body, style: const TextStyle(height: 1.6)),
        actions: <Widget>[
          // 取消放在前面、默认不聚焦：手滑按回车不该执行危险操作
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: Theme.of(ctx).colorScheme.error,
            ),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: Text(confirmText),
          ),
        ],
      ),
    );
    return result ?? false;
  }
}

// ──────────────────────────────────────────────────────────────────────
// 账号
// ──────────────────────────────────────────────────────────────────────

class _AccountSection extends StatelessWidget {
  const _AccountSection({
    required this.accounts,
    required this.busy,
    required this.onLogin,
    required this.onUse,
    required this.onRemove,
  });

  final List<AccountItem>? accounts;
  final bool busy;
  final VoidCallback onLogin;
  final void Function(int uid) onUse;
  final void Function(int uid, String nickname) onRemove;

  @override
  Widget build(BuildContext context) {
    final list = accounts;
    if (list == null) return const LoadingLine(text: '正在读取账号…');

    final active = list.where((a) => a.active).firstOrNull;

    return InfoCard(
      title: active == null ? '未登录' : active.nickname,
      subtitle: active == null
          ? '还没有登录网易云账号，很多功能用不了'
          : 'uid ${active.uid}${active.vip ? ' · VIP' : ' · 普通用户'}',
      trailing: FilledButton.icon(
        icon: const Icon(Icons.qr_code_2, size: 17),
        label: Text(active == null ? '扫码登录' : '登录新账号'),
        onPressed: busy ? null : onLogin,
      ),
      child: list.isEmpty
          ? const SizedBox.shrink()
          : Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                const SizedBox(height: 4),
                const Text('已登录的账号', style: TextStyle(fontSize: 12.5)),
                const SizedBox(height: 8),
                for (final account in list)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 4),
                    child: Row(
                      children: <Widget>[
                        Dot(
                          account.active ? StatusColors.ok : StatusColors.idle,
                        ),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            '${account.nickname}'
                            '${account.vip ? '（VIP）' : ''}'
                            '${account.active ? '  ← 当前' : ''}',
                            style: const TextStyle(fontSize: 13),
                          ),
                        ),
                        if (!account.active)
                          TextButton(
                            onPressed: busy ? null : () => onUse(account.uid),
                            child: const Text('切换'),
                          ),
                        IconButton(
                          icon: const Icon(Icons.close, size: 16),
                          tooltip: '移除这个账号',
                          onPressed: busy
                              ? null
                              : () => onRemove(account.uid, account.nickname),
                        ),
                      ],
                    ),
                  ),
              ],
            ),
    );
  }
}

// ──────────────────────────────────────────────────────────────────────
// 同步设置
// ──────────────────────────────────────────────────────────────────────

class _SyncSection extends StatelessWidget {
  const _SyncSection({
    required this.settings,
    required this.busy,
    required this.onQuality,
    required this.onInterval,
  });

  final AppSettings? settings;
  final bool busy;
  final ValueChanged<String> onQuality;
  final ValueChanged<double> onInterval;

  @override
  Widget build(BuildContext context) {
    final data = settings;
    if (data == null) return const LoadingLine(text: '正在读取设置…');

    final scheme = Theme.of(context).colorScheme;

    return InfoCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          const Text(
            '音质档位',
            style: TextStyle(fontSize: 13.5, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 4),
          Text(
            '只提供能进 iPod 的档位。无损会转成 16bit ALAC；'
            '全库无损约 144 GB，装不下这台 80 GB 的。',
            style: TextStyle(
              fontSize: 11.5,
              color: scheme.onSurfaceVariant,
              height: 1.5,
            ),
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 10,
            children: <Widget>[
              for (final option in data.qualityOptions)
                ChoiceChip(
                  label: Text(option.label),
                  selected: option.value == data.quality,
                  onSelected: busy ? null : (_) => onQuality(option.value),
                ),
            ],
          ),
          const Divider(height: 30),
          Row(
            children: <Widget>[
              const Text(
                '请求间隔',
                style: TextStyle(fontSize: 13.5, fontWeight: FontWeight.w600),
              ),
              const SizedBox(width: 10),
              Text(
                '${data.minInterval.toStringAsFixed(2)} 秒',
                style: TextStyle(
                  fontSize: 13,
                  color: scheme.primary,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
          const SizedBox(height: 4),
          Text(
            '两次请求之间至少等这么久。**这是防封号的安全阀，不是性能选项**——'
            '调得越小越快，但越快越容易被网易云风控。下限 '
            '${data.minIntervalFloor.toStringAsFixed(2)} 秒。',
            style: TextStyle(
              fontSize: 11.5,
              color: scheme.onSurfaceVariant,
              height: 1.5,
            ),
          ),
          const SizedBox(height: 6),
          Slider(
            value: data.minInterval.clamp(data.minIntervalFloor, 1.5),
            min: data.minIntervalFloor,
            max: 1.5,
            divisions: ((1.5 - data.minIntervalFloor) / 0.05).floor(),
            label: '${data.minInterval.toStringAsFixed(2)} 秒',
            onChanged: busy ? null : onInterval,
          ),
        ],
      ),
    );
  }
}

// ──────────────────────────────────────────────────────────────────────
// 缓存
// ──────────────────────────────────────────────────────────────────────

class _CacheSection extends StatelessWidget {
  const _CacheSection({
    required this.cache,
    required this.busy,
    required this.onClear,
  });

  final CacheInfo? cache;
  final bool busy;
  final VoidCallback onClear;

  @override
  Widget build(BuildContext context) {
    final data = cache;
    if (data == null) return const LoadingLine(text: '正在统计缓存…');

    final scheme = Theme.of(context).colorScheme;

    return InfoCard(
      trailing: OutlinedButton.icon(
        icon: const Icon(Icons.delete_sweep_outlined, size: 17),
        label: const Text('清空缓存'),
        onPressed: busy || data.files == 0 ? null : onClear,
      ),
      title: '${data.files} 个文件 · ${data.sizeText}',
      subtitle: data.dir,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          if (!data.consistent) ...<Widget>[
            const SizedBox(height: 4),
            Notice(
              icon: Icons.warning_amber_outlined,
              title: '缓存和记录对不上',
              text:
                  '磁盘上有 ${data.files} 个文件，下载记录有 ${data.records} 条。\n'
                  '通常是手动删过缓存、或者从别处拷来的文件。'
                  '点「清空缓存」可以一次抹平。',
              color: StatusColors.warn,
            ),
          ] else ...<Widget>[
            const SizedBox(height: 4),
            Text(
              '下载记录 ${data.records} 条，与磁盘一致。',
              style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
            ),
          ],
        ],
      ),
    );
  }
}

// ──────────────────────────────────────────────────────────────────────
// 设备信息
// ──────────────────────────────────────────────────────────────────────

class _DeviceSection extends StatelessWidget {
  const _DeviceSection({required this.info});

  final DeviceInfo? info;

  @override
  Widget build(BuildContext context) {
    final data = info;
    if (data == null) {
      return const Notice(icon: Icons.info_outline, text: '还没有设备信息。点右上角刷新试试。');
    }
    if (!data.connected) {
      return Notice(
        icon: Icons.usb_off,
        title: data.error.isEmpty ? '未检测到 iPod' : data.error,
        text: data.hint.isEmpty
            ? '请确认数据线插好、iPod 已解锁，然后在「此电脑」里能看到它的盘符。'
            : data.hint,
      );
    }

    return Column(
      children: <Widget>[
        _IdentityCard(info: data),
        const SizedBox(height: 14),
        if (data.storage != null) ...<Widget>[
          _StorageCard(storage: data.storage!),
          const SizedBox(height: 14),
        ],
        if (data.content != null) _ContentCard(content: data.content!),
      ],
    );
  }
}

class _IdentityCard extends StatelessWidget {
  const _IdentityCard({required this.info});

  final DeviceInfo info;

  @override
  Widget build(BuildContext context) {
    final id = info.identity!;
    return InfoCard(
      title: '身份',
      rows: <InfoRow>[
        InfoRow(
          '设备名',
          id.name,
          editable: info.canEdit('name'),
          note: info.canEdit('name') ? '存在数据库里，可改' : null,
        ),
        InfoRow('型号', id.modelNumber),
        InfoRow('型号全称', id.displayName),
        InfoRow('系列', id.family),
        InfoRow('世代', id.generation),
        InfoRow('颜色', id.color),
        InfoRow('序列号', id.serial, note: '硬件信息，只读'),
        InfoRow('FireWire GUID', id.firewireGuid, note: '硬件信息，只读', mono: true),
        InfoRow(
          '签名方案',
          id.checksum,
          note: id.isSupported ? '本工具已验证支持' : '方案未验证',
        ),
        InfoRow('挂载点', id.mountPoint, mono: true),
      ],
    );
  }
}

class _StorageCard extends StatelessWidget {
  const _StorageCard({required this.storage});

  final DeviceStorage storage;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return InfoCard(
      title: '存储空间',
      subtitle: '音乐占用之外的部分是系统文件、封面库等',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Row(
            children: <Widget>[
              Text(
                storage.totalText,
                style: const TextStyle(
                  fontSize: 22,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(width: 8),
              Text(
                '总量',
                style: TextStyle(
                  fontSize: 12.5,
                  color: scheme.onSurfaceVariant,
                ),
              ),
              const Spacer(),
              Text(
                '已用 ${storage.usedText} · 可用 ${storage.freeText}',
                style: const TextStyle(fontSize: 13),
              ),
            ],
          ),
          const SizedBox(height: 10),
          ClipRRect(
            borderRadius: BorderRadius.circular(4),
            child: LinearProgressIndicator(
              value: storage.fraction,
              minHeight: 8,
              backgroundColor: scheme.surfaceContainerHighest,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            '已用 ${storage.usedPercent.toStringAsFixed(1)}%',
            style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
          ),
          const SizedBox(height: 14),
          Row(
            children: <Widget>[
              const Icon(Icons.library_music_outlined, size: 16),
              const SizedBox(width: 6),
              const Text('音乐占用', style: TextStyle(fontSize: 13)),
              const Spacer(),
              Text(
                storage.musicText,
                style: const TextStyle(
                  fontSize: 13,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _ContentCard extends StatelessWidget {
  const _ContentCard({required this.content});

  final DeviceContent content;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return InfoCard(
      title: '内容统计',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Wrap(
            spacing: 26,
            runSpacing: 12,
            children: <Widget>[
              Stat('曲目', '${content.tracks}', unit: '首'),
              Stat('专辑', '${content.albums}', unit: '张'),
              Stat('艺人', '${content.artists}', unit: '位'),
              Stat('总时长', content.durationText),
              Stat('播放列表', '${content.playlists}', unit: '个'),
            ],
          ),
          if (content.playlistNames.isNotEmpty) ...<Widget>[
            const SizedBox(height: 14),
            Text(
              content.playlistNames.join('、'),
              style: TextStyle(fontSize: 12.5, color: scheme.onSurfaceVariant),
            ),
          ],
        ],
      ),
    );
  }
}

// ──────────────────────────────────────────────────────────────────────
// 后端 / 调试
// ──────────────────────────────────────────────────────────────────────

class _BackendSection extends StatelessWidget {
  const _BackendSection({
    required this.env,
    required this.logKey,
    required this.api,
    required this.jobs,
    required this.onDoctor,
    required this.onExport,
  });

  final EnvInfo? env;
  final GlobalKey<LogViewState> logKey;
  final ApiClient api;
  final JobsSnapshot? jobs;
  final VoidCallback onDoctor;
  final VoidCallback onExport;

  @override
  Widget build(BuildContext context) {
    final backend = context.watch<BackendService>();

    final (String phaseText, Color phaseColor) = switch (backend.phase) {
      BackendPhase.idle => ('未启动', StatusColors.idle),
      BackendPhase.starting => ('启动中', StatusColors.warn),
      BackendPhase.ready => ('就绪', StatusColors.ok),
      BackendPhase.failed => ('启动失败', StatusColors.error),
      BackendPhase.stopped => ('已停止', StatusColors.idle),
    };

    final info = env;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        const SectionTitle(title: '后端', subtitle: '由本程序自动拉起；这里的日志就是它的原始输出'),
        const SizedBox(height: 4),
        InfoCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Wrap(
                spacing: 26,
                runSpacing: 12,
                children: <Widget>[
                  Stat('状态', phaseText, color: phaseColor),
                  Stat('端口', '${backend.port}'),
                  Stat('PID', backend.pid?.toString() ?? '—'),
                ],
              ),
              const SizedBox(height: 12),
              Row(
                children: <Widget>[
                  Dot(phaseColor),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      backend.message,
                      style: const TextStyle(fontSize: 12.5),
                    ),
                  ),
                ],
              ),
              if (backend.reused) ...<Widget>[
                const SizedBox(height: 10),
                const Notice(
                  icon: Icons.link,
                  text:
                      '这个后端是之前就在跑的（上一次没退干净、或者你手动起的），'
                      '本程序直接复用了它，没有另起一个。',
                ),
              ],
              const Divider(height: 28),
              Row(
                children: <Widget>[
                  OutlinedButton.icon(
                    icon: const Icon(Icons.restart_alt, size: 17),
                    label: const Text('重启后端'),
                    onPressed: backend.phase == BackendPhase.starting
                        ? null
                        : () => backend.restart(),
                  ),
                  const SizedBox(width: 10),
                  OutlinedButton.icon(
                    icon: const Icon(Icons.monitor_heart_outlined, size: 17),
                    label: const Text('环境自检'),
                    onPressed: onDoctor,
                  ),
                  const SizedBox(width: 10),
                  OutlinedButton.icon(
                    icon: const Icon(Icons.ios_share, size: 17),
                    label: const Text('导出诊断记录'),
                    onPressed: onExport,
                  ),
                ],
              ),
            ],
          ),
        ),
        if (info != null) ...<Widget>[
          const SizedBox(height: 14),
          InfoCard(
            title: '环境',
            rows: <InfoRow>[
              InfoRow('Python', info.python),
              InfoRow('ipod-cli', info.ipodCli),
              InfoRow(
                'ffmpeg',
                info.ffmpegOk ? info.ffmpeg : '✗ 未找到（无法转码 FLAC 等格式）',
                mono: info.ffmpegOk,
              ),
              InfoRow('网易云服务', info.baseUrl, mono: true),
              InfoRow('状态库', info.dbPath, mono: true),
              InfoRow('缓存目录', info.cacheDir, mono: true),
            ],
          ),
        ],
        const SizedBox(height: 14),
        InfoCard(
          title: '作业日程',
          subtitle:
              '后台队列都干过什么。日程是落盘的，重启应用不会丢。'
              '点一行展开看逐首歌的结果和日志；要发给开发就点上面的「导出诊断记录」',
          child: JobSchedule(api: api, snapshot: jobs),
        ),
        const SizedBox(height: 14),
        InfoCard(
          title: '后端日志',
          subtitle: '日志用 seq 游标增量拉取，不会越用越卡',
          child: LogView(
            key: logKey,
            fetcher: ({int since = 0}) => api.debugLog(since: since),
            onClear: api.clearDebugLog,
            height: 300,
          ),
        ),
      ],
    );
  }
}
