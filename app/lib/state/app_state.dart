/// 应用的共享状态：后端状态 + 定时轮询 + 界面要用的数据。
///
/// 轮询间隔 3 秒：够及时，又不会让状态条成为 CPU 烤炉。后端那边读设备库
/// 有 5 秒缓存，所以这个频率不会反复解析 iTunesDB。
library;

import 'dart:async';

import 'package:flutter/foundation.dart';

import '../api/client.dart';
import '../api/models.dart';

/// "作业干完了，该重新拉数据了"的信号。
///
/// 为什么需要它：下载、同步、删除、导入**全都走后台作业队列**，提交完
/// 立刻返回。作业真正干完的时候，页面上的数据已经过期了——歌单页还标着
/// "未处理"、iPod 音乐管理页还没算上刚导进去的歌、本地列表还显示着刚删掉
/// 的东西。让每个页面自己写个定时器去猜"好了没"，既不准、又互相不同步
/// （之前歌单页等 5 秒、下载页等 4 秒，纯属拍脑袋）。
///
/// 现在是：后端队列一有作业**完成**，这里就 ping 一次，所有页面一起刷新。
/// 判据是"完成集合里出现了新的作业 id"，不是"忙变闲"——后者会漏掉
/// 两次轮询之间就干完的短作业。
///
/// 页面用法：
/// ```dart
/// late final AppState _state = context.read<AppState>();
///
/// @override
/// void initState() {
///   super.initState();
///   _state.refreshSignal.addListener(_reload);
/// }
///
/// @override
/// void dispose() {
///   _state.refreshSignal.removeListener(_reload);
///   super.dispose();
/// }
/// ```
class RefreshSignal extends ChangeNotifier {
  int _tick = 0;

  /// 每次有作业完成 +1。需要区分"自上次以来有没有变过"时可以比这个。
  int get tick => _tick;

  void ping() {
    _tick++;
    notifyListeners();
  }
}

class AppState extends ChangeNotifier {
  AppState({required this.api});

  final ApiClient api;

  /// 顶部状态条的数据。
  AppStatus? status;

  /// 作业队列快照（状态条右侧的"进行中"、下载器页）。
  JobsSnapshot? jobs;

  /// 设置页设备信息（按需拉，不跟随轮询）。
  DeviceInfo? device;

  /// 最近一次拉取失败的原因（中文）。成功时清空。
  String? error;

  /// "有作业干完了"的信号。页面听它重新拉自己的数据，见 [RefreshSignal]。
  final RefreshSignal refreshSignal = RefreshSignal();

  /// 已经见过的"完成"作业 id，用来判断新完成的。见 [RefreshSignal]。
  Set<String> _finishedJobIds = <String>{};
  bool _firstPoll = true;

  bool loading = false;

  Timer? _timer;
  bool _disposed = false;
  bool _fetching = false;

  // ── 轮询 ──────────────────────────────────────────────────────────

  void startPolling({Duration interval = const Duration(seconds: 3)}) {
    stopPolling();
    unawaited(refreshStatus());
    _timer = Timer.periodic(interval, (_) => unawaited(refreshStatus()));
  }

  void stopPolling() {
    _timer?.cancel();
    _timer = null;
  }

  /// 拉一次状态 + 作业队列。
  ///
  /// **失败不清空旧数据**——网络抖一下就把界面清空，用户会以为设备掉了。
  /// 只把错误显示出来，数据留着。
  Future<void> refreshStatus() async {
    if (_fetching || _disposed) return;
    _fetching = true;
    try {
      final next = await api.status();
      if (_disposed) return;
      status = next;
      error = null;
    } on ApiException catch (e) {
      if (_disposed) return;
      error = e.message;
    } catch (e) {
      if (_disposed) return;
      error = '获取状态失败：$e';
    }

    // 作业队列单独拉：状态接口不带它，免得每次都为它多查一遍设备
    try {
      final next = await api.jobs();
      if (_disposed) return;
      jobs = next;
      _checkFinished(next);
    } catch (_) {
      // 作业拉不到不影响状态条，静默
    } finally {
      _fetching = false;
      if (!_disposed) notifyListeners();
    }
  }

  /// 有作业刚完成就 ping 一次刷新信号。
  ///
  /// 判据用"完成集合里出现了新 id"而不是"队列从忙变闲"：后者在
  /// 两次轮询（3 秒）之间就干完的短作业上会**完全漏掉**——
  /// 删几首本地文件就是这种，用户看不到列表更新，会以为没删掉。
  void _checkFinished(JobsSnapshot snapshot) {
    final finished = snapshot.jobs
        .where((job) => job.finished)
        .map((job) => job.id)
        .toSet();

    // 第一次拉的时候里面全是历史作业，别当成"刚刚完成"
    if (_firstPoll) {
      _firstPoll = false;
      _finishedJobIds = finished;
      return;
    }

    if (finished.difference(_finishedJobIds).isNotEmpty) {
      refreshSignal.ping();
    }
    _finishedJobIds = finished;
  }

  /// 拉设备信息（设置页用）。
  Future<void> refreshDevice() async {
    if (_disposed) return;
    loading = true;
    notifyListeners();
    try {
      device = await api.device();
      error = null;
    } on ApiException catch (e) {
      error = e.message;
    } catch (e) {
      error = '获取设备信息失败：$e';
    } finally {
      loading = false;
      if (!_disposed) notifyListeners();
    }
  }

  /// 外部改动数据后（下载完、导入完、删除完）主动刷一次。
  Future<void> refreshNow() async {
    await refreshStatus();
  }

  @override
  void dispose() {
    _disposed = true;
    stopPolling();
    refreshSignal.dispose();
    api.dispose();
    super.dispose();
  }
}
