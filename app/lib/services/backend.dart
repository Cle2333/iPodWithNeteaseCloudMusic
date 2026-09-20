/// 后端进程的生命周期管理。
///
/// 自动拉起后端的好处是"点开就能用"，代价是**它变成了黑箱**——
/// 所以这里把子进程的 stdout/stderr 全收下来喂给调试面板。
///
/// 两个从实测里学来的教训，都写进代码了：
///
/// 1. **杀进程必须杀整棵树**。`uv run ipod-web` 是两层的
///    （uv 启动器 → python/uvicorn），杀启动器不会杀掉真正在监听的
///    那个。症状是端口仍被占、日志文件被锁、下次启动失败，
///    而表面上"进程已经杀掉了"。
///
/// 2. **启动前先探测**。上一次没清干净的后端可能还活着，
///    盲目再起一个只会撞端口。能复用就复用。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../api/client.dart';

enum BackendPhase {
  /// 还没动
  idle,

  /// 正在拉起 / 等就绪
  starting,

  /// 可以用了
  ready,

  /// 起不来
  failed,

  /// 已停止
  stopped,
}

/// 日志缓冲上限。长跑的程序不能让它无限涨。
const int kMaxLogLines = 800;

class BackendService extends ChangeNotifier {
  BackendService({required this.port});

  final int port;

  BackendPhase phase = BackendPhase.idle;
  String message = '尚未启动';
  int? pid;

  /// 是否复用了本来就跑着的后端（而不是自己拉起来的）。
  bool reused = false;

  final List<String> _logLines = <String>[];
  List<String> get logLines => List.unmodifiable(_logLines);

  Process? _process;
  StreamSubscription<String>? _outSub;
  StreamSubscription<String>? _errSub;
  bool _stopping = false;

  ApiClient get _probe => ApiClient(baseUrl: backendBaseUrl(port: port));

  /// 已经销毁了吗。
  ///
  /// dispose() 会去停子进程，而停的过程中还会写日志、发通知——
  /// 在一个已经销毁的 ChangeNotifier 上 notifyListeners() 会直接抛断言。
  bool _disposed = false;

  /// 发通知，但**销毁之后就不发**。
  ///
  /// 直接调 notifyListeners() 的话，退出应用那一刻（dispose → stop → 记日志）
  /// 就会踩 "A BackendService was used after being disposed"。
  void _notify() {
    if (_disposed) return;
    // ★ 这里必须是 notifyListeners()，不是 _notify()。
    //   自递归会一路爆栈——真机上表现为"应用起不来、后端没被拉起"，
    //   而测试里没跑到这条路径所以全绿。
    notifyListeners();
  }

  void _log(String line) {
    if (line.trim().isEmpty) return;
    for (final part in line.split('\n')) {
      final text = part.trimRight();
      if (text.isEmpty) continue;
      _logLines.add(text);
    }
    if (_logLines.length > kMaxLogLines) {
      _logLines.removeRange(0, _logLines.length - kMaxLogLines);
    }
    _notify();
  }

  void clearLog() {
    _logLines.clear();
    _notify();
  }

  // ── 启动 ──────────────────────────────────────────────────────────

  /// 拉起后端；已经有活着的就直接复用。
  Future<bool> start({Duration timeout = const Duration(seconds: 90)}) async {
    if (phase == BackendPhase.starting) return false;

    phase = BackendPhase.starting;
    message = '正在检查后端…';
    _notify();

    // ① 先看是不是本来就有一个在跑。
    //    盲目再起一个只会撞端口——而端口被占的报错很难看懂。
    final probe = _probe;
    if (await probe.health()) {
      probe.dispose();
      reused = true;
      phase = BackendPhase.ready;
      message = '后端已在运行（复用现有的）';
      _log('[启动器] 检测到已有后端在 $port 端口，直接复用');
      _notify();
      return true;
    }
    probe.dispose();

    // ② 找工程根目录和 uv
    final repo = _findRepoRoot();
    if (repo == null) {
      phase = BackendPhase.failed;
      message = '找不到项目目录（没找到 pyproject.toml）';
      _log('[启动器] 从 ${Directory.current.path} 往上都没找到 pyproject.toml');
      _notify();
      return false;
    }
    _log('[启动器] 项目目录：${repo.path}');

    final uv = await _resolveUv();
    if (uv == null) {
      phase = BackendPhase.failed;
      message = '找不到 uv 命令';
      _log('[启动器] 找不到 uv。装好 uv 后重启，或手动执行 uv run ipod-web');
      _notify();
      return false;
    }

    // ③ 拉起来
    message = '正在启动后端…';
    _notify();

    final logDir = Directory(
      '${repo.path}${Platform.pathSeparator}.ncm'
      '${Platform.pathSeparator}logs',
    );
    final logFile =
        '${logDir.path}${Platform.pathSeparator}'
        'ipod-web-${_stamp()}.log';

    try {
      final process = await Process.start(
        uv,
        ['run', 'ipod-web', '--port', '$port', '--log-file', logFile],
        workingDirectory: repo.path,
        environment: _childEnvironment(),
        runInShell: false,
      );
      _process = process;
      pid = process.pid;
      _log('[启动器] 后端进程已拉起，PID = ${process.pid}');

      _outSub = process.stdout
          .transform(utf8.decoder)
          .transform(const LineSplitter())
          .listen(_log);
      _errSub = process.stderr
          .transform(utf8.decoder)
          .transform(const LineSplitter())
          .listen(_log);

      // 进程自己退了也要知道（比如端口被占、依赖坏了）
      unawaited(
        process.exitCode.then((code) {
          if (_stopping) return;
          _log('[启动器] 后端进程退出，退出码 $code');
          if (phase != BackendPhase.ready) {
            phase = BackendPhase.failed;
            message = code == 0 ? '后端已退出' : '后端启动失败（退出码 $code）';
          } else {
            phase = BackendPhase.stopped;
            message = '后端已停止';
          }
          _notify();
        }),
      );
    } catch (error) {
      phase = BackendPhase.failed;
      message = '拉起后端失败：$error';
      _log('[启动器] 拉起失败：$error');
      _notify();
      return false;
    }

    // ④ 等它就绪（就绪探测是极轻量的接口，设备没插也能通）
    final ready = await _waitReady(timeout);
    if (ready) {
      phase = BackendPhase.ready;
      message = '后端已就绪';
      _log('[启动器] 后端就绪');
    } else if (phase != BackendPhase.failed) {
      phase = BackendPhase.failed;
      message = '后端启动超时（${timeout.inSeconds} 秒）';
      _log('[启动器] 等待就绪超时。上面的输出就是后端的日志，可以先看那里。');
    }
    _notify();
    return phase == BackendPhase.ready;
  }

  Future<bool> _waitReady(Duration timeout) async {
    final deadline = DateTime.now().add(timeout);
    while (DateTime.now().isBefore(deadline)) {
      if (phase == BackendPhase.failed) return false;
      final client = _probe;
      final ok = await client.health();
      client.dispose();
      if (ok) return true;
      await Future<void>.delayed(const Duration(milliseconds: 400));
    }
    return false;
  }

  // ── 停止 ──────────────────────────────────────────────────────────

  /// 停掉后端。**杀整棵树**，不是杀单个 PID——见文件开头的说明。
  Future<void> stop() async {
    _stopping = true;

    // 复用的那个不是我们拉起来的，不该由我们杀掉
    if (reused && _process == null) {
      phase = BackendPhase.stopped;
      message = '已断开（后端是外部启动的，没有关掉它）';
      _notify();
      return;
    }

    final process = _process;
    if (process != null) {
      _log('[启动器] 正在停止后端（PID ${process.pid}，连子进程一起）…');
      await _killTree(process.pid);
      _process = null;
    }

    await _outSub?.cancel();
    await _errSub?.cancel();
    _outSub = null;
    _errSub = null;

    // 确认端口真的释放了——"杀掉了"和"真的没了"不是一回事
    final freed = await _waitPortFree(const Duration(seconds: 5));
    _log(freed ? '[启动器] 端口 $port 已释放' : '[启动器] ⚠ 端口 $port 仍被占用，可能有别的进程占着');

    phase = BackendPhase.stopped;
    message = freed ? '后端已停止' : '后端已停止，但端口仍被占用';
    pid = null;
    _notify();
  }

  /// 重启后端。设置页的调试面板用——改了设置、或者后端卡住时，一键重来。
  ///
  /// **复用来的后端会先"断开"再重新探测**：如果它是外部启动的，
  /// stop() 不会去杀它，于是 restart 会重新探测到它并复用（这是对的，
  /// 我们本来就不该杀别人的进程）。
  Future<bool> restart() async {
    _log('[启动器] 用户请求重启后端');
    await stop();
    reused = false;
    return start();
  }

  Future<void> _killTree(int targetPid) async {
    if (Platform.isWindows) {
      // /T 杀整棵树 —— 只杀单个 PID 的话，uv 死了 python 还活着
      try {
        final result = await Process.run('taskkill', [
          '/PID',
          '$targetPid',
          '/T',
          '/F',
        ], runInShell: false);
        final text = _decode(result.stdout) + _decode(result.stderr);
        if (text.trim().isNotEmpty) _log('[启动器] $text'.trim());
      } catch (error) {
        _log('[启动器] taskkill 失败：$error');
      }
    } else {
      try {
        await Process.run('kill', ['-TERM', '-$targetPid'], runInShell: false);
      } catch (error) {
        _log('[启动器] kill 失败：$error');
      }
    }
  }

  Future<bool> _waitPortFree(Duration timeout) async {
    final deadline = DateTime.now().add(timeout);
    while (DateTime.now().isBefore(deadline)) {
      final client = _probe;
      final alive = await client.health();
      client.dispose();
      if (!alive) return true;
      await Future<void>.delayed(const Duration(milliseconds: 300));
    }
    return false;
  }

  // ── 环境与路径 ────────────────────────────────────────────────────

  /// 子进程的环境变量。
  ///
  /// **把 PYTHONPATH 摘掉**：本机 PYTHONPATH 常被设成别的 venv 的
  /// site-packages，会排在项目 venv 前面把依赖遮蔽掉——典型症状是
  /// "Python 版本对但装的是另一个版本的 numpy"。uv 自己管 venv，
  /// 不需要外面塞路径进来。
  Map<String, String> _childEnvironment() {
    final env = Map<String, String>.from(Platform.environment);
    env.remove('PYTHONPATH');
    env['PYTHONUNBUFFERED'] = '1'; // 否则日志会攒着，调试面板看不到实时输出
    return env;
  }

  /// 从当前目录往上找 pyproject.toml。
  ///
  /// 开发时工作目录是 `app/`，发布后可能是 exe 所在目录——往上找比写死
  /// 相对路径稳。也支持用环境变量显式指定。
  Directory? _findRepoRoot() {
    final override = Platform.environment['IPOD_MANAGER_REPO'];
    if (override != null && override.isNotEmpty) {
      final dir = Directory(override);
      if (dir.existsSync()) return dir;
    }

    var dir = Directory.current;
    // 往上找 10 层。看着多，但真需要：直接跑编译出来的 exe 时，工作目录是
    // app/build/windows/x64/runner/Debug，往上要经过
    // Debug→runner→x64→windows→build→app 才到项目根（**第 6 层**）。
    // 只找 6 层的话刚好差一步，症状是"找不到项目目录"。
    for (var depth = 0; depth < 10; depth++) {
      if (File('${dir.path}${Platform.pathSeparator}pyproject.toml')
          .existsSync()) {
        return dir;
      }
      final parent = dir.parent;
      if (parent.path == dir.path) break;
      dir = parent;
    }
    return null;
  }

  /// 找到 uv 可执行文件。
  ///
  /// **先问系统的 PATH**（`where`/`which`），再退回到几个常见安装位置。
  /// 不能只写死候选路径：uv 的安装位置五花八门（官方脚本装到
  /// `~/.local/bin`、cargo 装到 `~/.cargo/bin`、winget 装到
  /// `%LOCALAPPDATA%\Microsoft\WinGet\Links`……），漏一个就变成
  /// "应用起来了但后端拉不起来"，而且报错很难指向真正的原因。
  Future<String?> _resolveUv() async {
    final override = Platform.environment['IPOD_MANAGER_UV'];
    if (override != null && override.isNotEmpty) {
      _log('[启动器] 用环境变量指定的 uv：$override');
      return override;
    }

    // ① PATH
    try {
      final finder = Platform.isWindows ? 'where' : 'which';
      final result = await Process.run(finder, ['uv'], runInShell: false);
      if (result.exitCode == 0) {
        final lines = const LineSplitter()
            .convert(_decode(result.stdout))
            .map((l) => l.trim())
            .where((l) => l.isNotEmpty)
            .toList();
        if (lines.isNotEmpty) {
          _log('[启动器] 从 PATH 找到 uv：${lines.first}');
          return lines.first;
        }
      }
    } catch (error) {
      _log('[启动器] 查 PATH 失败：$error');
    }

    // ② 常见安装位置
    final candidates = <String>[];
    if (Platform.isWindows) {
      final home = Platform.environment['USERPROFILE'] ?? '';
      final local = Platform.environment['LOCALAPPDATA'] ?? '';
      candidates.addAll(<String>[
        '$home\\.local\\bin\\uv.exe',
        '$home\\.cargo\\bin\\uv.exe',
        '$local\\Microsoft\\WinGet\\Links\\uv.exe',
        '$local\\Programs\\uv\\uv.exe',
      ]);
    } else {
      final home = Platform.environment['HOME'] ?? '';
      candidates.addAll(<String>[
        '$home/.local/bin/uv',
        '$home/.cargo/bin/uv',
        '/opt/homebrew/bin/uv',
        '/usr/local/bin/uv',
      ]);
    }
    for (final candidate in candidates) {
      if (File(candidate).existsSync()) {
        _log('[启动器] 从常见位置找到 uv：$candidate');
        return candidate;
      }
    }

    // ③ 交给系统再试一次（PATH 可能有延迟生效的情况）
    return Platform.isWindows ? 'uv.exe' : 'uv';
  }

  static String _decode(List<int> bytes) {
    // Windows 上 taskkill 吐 GBK；直接 UTF-8 解会抛。
    try {
      return utf8.decode(bytes);
    } catch (_) {
      return latin1.decode(bytes, allowInvalid: true);
    }
  }

  static String _stamp() {
    final now = DateTime.now();
    String two(int v) => v.toString().padLeft(2, '0');
    return '${now.year}${two(now.month)}${two(now.day)}';
  }

  @override
  void dispose() {
    // 先把标志立起来再停子进程：stop() 是异步的（要杀进程树），
    // 它跑的过程中还会写日志发通知，而那些通知已经没人要听了，
    // 在已销毁的对象上发还会直接抛断言。
    _disposed = true;
    unawaited(stop());
    super.dispose();
  }
}
