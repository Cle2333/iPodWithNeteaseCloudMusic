/// iPod 音乐管理器 —— 桌面端入口。
///
/// 启动流程：
///   拉起后端 → 等就绪（期间显示实时日志）→ 进主界面
///
/// 退出流程：
///   **杀掉后端进程树**。不杀的话下次启动会撞端口，
///   而且报错信息看起来跟"后端坏了"一模一样，很难查。
library;

import 'dart:async';
// AppExitResponse 定义在 dart:ui（不是 material/services）。
// 用 show 窄导入，免得 dart:ui 的其他符号跟 material 撞名。
import 'dart:ui' show AppExitResponse;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'api/client.dart';
import 'pages/shell.dart';
import 'services/backend.dart';
import 'state/app_state.dart';
import 'theme.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const IpodManagerApp());
}

class IpodManagerApp extends StatefulWidget {
  const IpodManagerApp({super.key});

  @override
  State<IpodManagerApp> createState() => _IpodManagerAppState();
}

class _IpodManagerAppState extends State<IpodManagerApp> {
  late final BackendService _backend;
  late final AppState _appState;
  AppLifecycleListener? _lifecycle;

  @override
  void initState() {
    super.initState();
    _backend = BackendService(port: kDefaultBackendPort);
    _appState = AppState(api: ApiClient());

    // 关窗口时先把后端收干净。用 AppLifecycleListener 而不是
    // State.dispose —— dispose 在桌面端不一定来得及跑完异步操作。
    _lifecycle = AppLifecycleListener(
      onExitRequested: () async {
        await _backend.stop();
        return AppExitResponse.exit;
      },
    );
  }

  @override
  void dispose() {
    _lifecycle?.dispose();
    _appState.dispose();
    _backend.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider<BackendService>.value(value: _backend),
        ChangeNotifierProvider<AppState>.value(value: _appState),
      ],
      child: MaterialApp(
        title: 'iPod 音乐管理器',
        debugShowCheckedModeBanner: false,
        theme: buildTheme(),
        home: _StartupGate(onReady: () => _appState.startPolling()),
      ),
    );
  }
}

/// 启动闸门：后端没就绪就先给一个能看日志的启动页。
class _StartupGate extends StatefulWidget {
  const _StartupGate({required this.onReady});

  final VoidCallback onReady;

  @override
  State<_StartupGate> createState() => _StartupGateState();
}

class _StartupGateState extends State<_StartupGate> {
  bool _started = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _boot());
  }

  Future<void> _boot() async {
    if (_started) return;
    _started = true;
    final backend = context.read<BackendService>();
    final ok = await backend.start();
    if (!mounted) return;
    if (ok) {
      widget.onReady();
      setState(() {});
    }
  }

  @override
  Widget build(BuildContext context) {
    final backend = context.watch<BackendService>();

    switch (backend.phase) {
      case BackendPhase.ready:
        return const Shell();
      case BackendPhase.idle:
      case BackendPhase.starting:
        return _StartupScreen(backend: backend, failed: false);
      case BackendPhase.failed:
      case BackendPhase.stopped:
        return _StartupScreen(backend: backend, failed: true);
    }
  }
}

class _StartupScreen extends StatelessWidget {
  const _StartupScreen({required this.backend, required this.failed});

  final BackendService backend;
  final bool failed;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;

    return Scaffold(
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 720),
          child: Padding(
            padding: const EdgeInsets.all(28),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Row(
                  children: <Widget>[
                    if (!failed)
                      const SizedBox(
                        width: 20,
                        height: 20,
                        child: CircularProgressIndicator(strokeWidth: 2.4),
                      )
                    else
                      const Icon(
                        Icons.error_outline,
                        color: StatusColors.error,
                        size: 22,
                      ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Text(
                        failed ? '后端启动失败' : '正在启动后端…',
                        style: const TextStyle(
                          fontSize: 17,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  backend.message,
                  style: TextStyle(
                    fontSize: 12.5,
                    color: scheme.onSurfaceVariant,
                  ),
                ),
                if (failed) ...<Widget>[
                  const SizedBox(height: 12),
                  Text(
                    '下面就是后端的原始输出——原因通常就在里面。',
                    style: TextStyle(
                      fontSize: 12.5,
                      color: scheme.onSurfaceVariant,
                    ),
                  ),
                ],
                const SizedBox(height: 16),
                _LogBox(lines: backend.logLines),
                const SizedBox(height: 16),
                Row(
                  children: <Widget>[
                    FilledButton.icon(
                      icon: const Icon(Icons.refresh, size: 18),
                      label: Text(failed ? '重试' : '重新启动'),
                      onPressed: () => backend.start(),
                    ),
                    const SizedBox(width: 10),
                    if (failed)
                      OutlinedButton.icon(
                        icon: const Icon(Icons.copy_all, size: 18),
                        label: const Text('清空日志'),
                        onPressed: backend.clearLog,
                      ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _LogBox extends StatelessWidget {
  const _LogBox({required this.lines});

  final List<String> lines;

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 260,
      width: double.infinity,
      decoration: BoxDecoration(
        color: const Color(0xFF1E1E1E),
        borderRadius: BorderRadius.circular(8),
      ),
      padding: const EdgeInsets.all(12),
      child: lines.isEmpty
          ? Text('（等待后端输出…）', style: monoStyle(color: Colors.white38))
          : ListView.builder(
              itemCount: lines.length,
              itemBuilder: (context, index) => SelectableText(
                lines[index],
                style: monoStyle(color: const Color(0xFFD4D4D4)),
              ),
            ),
    );
  }
}
