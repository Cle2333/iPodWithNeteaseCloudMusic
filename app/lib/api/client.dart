/// 本地后端的 HTTP 客户端。
///
/// **全程序唯一碰网络的地方**（除了后端自己）。页面不直接调 http，
/// 都从这里走，这样超时、错误文案、URL 拼装只有一处。
///
/// 错误文案的规矩：后端返回的中文 `detail` / `error` **原样往上抛**。
/// 后端已经写了"请确认本地网易云 API 服务已启动"这种能照着做的说明，
/// 这里再包一层"请求失败"只会把有用的信息盖掉。
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'models.dart';

/// 默认端口。避开 3000（Remotion Studio）和 4000（网易云 API）。
const int kDefaultBackendPort = 8765;

/// 后端地址只可能是本机——这个后端能改 iPod 数据库，
/// 绑到局域网就是灾难。所以地址写死回环，不给配置项。
String backendBaseUrl({int port = kDefaultBackendPort}) =>
    'http://127.0.0.1:$port';

/// 调用后端失败。消息是**中文**，可以直接显示给用户。
class ApiException implements Exception {
  ApiException(this.message, {this.statusCode});

  final String message;
  final int? statusCode;

  @override
  String toString() => message;
}

class ApiClient {
  ApiClient({
    String? baseUrl,
    http.Client? httpClient,
    this.timeout = const Duration(seconds: 15),
  }) : baseUrl = baseUrl ?? backendBaseUrl(),
       _http = httpClient ?? http.Client();

  final String baseUrl;
  final Duration timeout;
  final http.Client _http;

  void dispose() => _http.close();

  // ── 状态 / 设备 ────────────────────────────────────────────────────

  /// 就绪探测。启动时轮询它，通了才进主界面。
  Future<bool> health() async {
    try {
      final json = await _get(
        '/api/health',
        timeout: const Duration(seconds: 3),
      );
      return json['status'] == 'ok';
    } on ApiException {
      return false;
    }
  }

  Future<AppStatus> status() async =>
      AppStatus.fromJson(await _get('/api/status'));

  Future<DeviceInfo> device() async =>
      DeviceInfo.fromJson(await _get('/api/device'));

  // ── 账号 ──────────────────────────────────────────────────────────

  Future<LoginState> loginState({bool withImage = false}) async =>
      LoginState.fromJson(
        await _get('/api/account/login?with_image=$withImage'),
      );

  /// 开始扫码登录。已经在登录中时会拿到**同一个**会话（不会顶掉）。
  ///
  /// 这个调用会等到后端把二维码取回来才返回，所以超时给得比一般的久。
  Future<LoginState> startLogin() async => LoginState.fromJson(
    await _post('/api/account/login', timeout: const Duration(seconds: 40)),
  );

  Future<void> cancelLogin() async => _post('/api/account/login/cancel');

  Future<List<AccountItem>> accounts() async {
    final json = await _get('/api/account/list');
    final list = json['accounts'];
    if (list is! List) return const <AccountItem>[];
    return list
        .map((e) => AccountItem.fromJson(Map<String, dynamic>.from(e as Map)))
        .toList();
  }

  Future<String> useAccount(int uid) async {
    final json = await _post('/api/account/use', body: {'uid': uid});
    return json['message']?.toString() ?? '已切换';
  }

  Future<String> removeAccount(int uid) async {
    final json = await _post('/api/account/remove', body: {'uid': uid});
    return json['message']?.toString() ?? '已移除';
  }

  // ── 设置 / 缓存 ────────────────────────────────────────────────────

  Future<AppSettings> settings() async =>
      AppSettings.fromJson(await _get('/api/settings'));

  Future<String> updateSettings({String? quality, double? minInterval}) async {
    final json = await _put(
      '/api/settings',
      body: {
        // 没传的字段整条不出现，后端才能区分"没改"和"改成空"
        'quality': ?quality,
        'min_interval': ?minInterval,
      },
    );
    return json['message']?.toString() ?? '已保存';
  }

  Future<CacheInfo> cache() async =>
      CacheInfo.fromJson(await _get('/api/cache'));

  Future<String> clearCache() async {
    final json = await _post('/api/cache/clear');
    return json['message']?.toString() ?? '已清空';
  }

  // ── 调试 ──────────────────────────────────────────────────────────

  /// 增量拉日志。`since` 是上次拿到的最大 seq——只取新行。
  Future<LogPage> debugLog({int since = 0, int limit = 400}) async =>
      LogPage.fromJson(await _get('/api/debug/log?since=$since&limit=$limit'));

  Future<void> clearDebugLog() async => _post('/api/debug/log/clear');

  Future<EnvInfo> env() async => EnvInfo.fromJson(await _get('/api/debug/env'));

  Future<String> runDoctor() async {
    final json = await _post('/api/debug/doctor');
    return json['job_id']?.toString() ?? '';
  }

  /// 导出诊断记录。返回导出文件的绝对路径。
  ///
  /// 后端会写一份完整的现场（环境 / 设备 / 设置 / 每个作业干了什么 /
  /// 逐首歌的结果 / 完整日志），用户直接把这个文件发出来就能排查。
  ///
  /// **不走作业队列**——用户往往正是卡在某个作业上才来导出的，
  /// 排在它后面就永远导不出来。
  Future<ExportResult> exportDiagnostics() async {
    final json = await _post(
      '/api/debug/export',
      timeout: const Duration(seconds: 60),
    );
    return ExportResult(
      path: json['path']?.toString() ?? '',
      bytes: (json['bytes'] as num?)?.toInt() ?? 0,
      lines: (json['lines'] as num?)?.toInt() ?? 0,
      jobs: (json['jobs'] as num?)?.toInt() ?? 0,
    );
  }

  // ── 作业 ──────────────────────────────────────────────────────────

  Future<JobsSnapshot> jobs() async =>
      JobsSnapshot.fromJson(await _get('/api/jobs'));

  Future<JobInfo> job(String id) async =>
      JobInfo.fromJson(await _get('/api/jobs/$id'));

  Future<LogPage> jobLog(String id, {int since = 0}) async {
    final json = await _get('/api/jobs/$id?since=$since');
    return LogPage.fromJson(json);
  }

  Future<String> cancelJob(String id) async {
    final json = await _post('/api/jobs/$id/cancel');
    return json['message']?.toString() ?? '已请求取消';
  }

  // ── 歌单 ──────────────────────────────────────────────────────────

  Future<PlaylistList> playlists({bool refresh = false}) async =>
      PlaylistList.fromJson(
        await _get(
          '/api/playlists?refresh=$refresh',
          timeout: const Duration(seconds: 40),
        ),
      );

  Future<PlaylistSongs> playlistSongs(
    int playlistId, {
    int page = 1,
    int size = 50,
    String status = 'all',
    String search = '',
  }) async => PlaylistSongs.fromJson(
    await _get(
      '/api/playlists/$playlistId/songs'
      '?page=$page&size=$size&status=$status'
      '&search=${Uri.encodeQueryComponent(search)}',
      timeout: const Duration(seconds: 40),
    ),
  );

  /// 只要 ID 的轻量版，给「一键选中所有未下载的（N 首）」用。
  ///
  /// 曲目本来就缓存在后端，这个调用不产生任何网易云请求。
  Future<List<int>> playlistIds(
    int playlistId, {
    String status = 'pending',
  }) async {
    final json = await _get(
      '/api/playlists/$playlistId/ids?status=$status',
      timeout: const Duration(seconds: 40),
    );
    final raw = json['ids'];
    if (raw is! List) return <int>[];
    return raw.whereType<num>().map((e) => e.toInt()).toList();
  }

  /// 预览这一下会处理多少首。
  ///
  /// [songIds] 为空集合时后端会报错（"没有选中任何歌曲"）——
  /// 这是有意的：把空集当成"整个歌单"会让几百首在用户没打算下的情况下跑起来。
  /// [songIds] 传 `null` 才是"整个歌单"。
  Future<PlanPreview> playlistPlan(
    int playlistId, {
    int limit = 0,
    bool push = false,
    Set<int>? songIds,
  }) async => PlanPreview.fromJson(
    await _post(
      '/api/playlists/$playlistId/plan',
      body: <String, dynamic>{
        'limit': limit,
        'push': push,
        'song_ids': songIds?.toList(),
      },
      timeout: const Duration(seconds: 60),
    ),
  );

  /// 开始下载（`push=true` 时顺带写进 iPod）。返回作业号。
  ///
  /// [songIds] 的语义同上：`null` = 整个歌单，空集合 = 报错。
  Future<String> downloadPlaylist(
    int playlistId, {
    int limit = 0,
    bool push = false,
    Set<int>? songIds,
  }) async {
    final json = await _post(
      '/api/playlists/$playlistId/download',
      body: <String, dynamic>{
        'limit': limit,
        'push': push,
        'song_ids': songIds?.toList(),
      },
    );
    return json['job_id']?.toString() ?? '';
  }

  // ── 本地缓存（电脑上那份下载）────────────────────────────────────

  /// 删掉本地缓存里的**指定几首**（记录 + 文件）。
  ///
  /// 歌单页的右键「删除本地那份」和多选操作条都打这个。
  /// 它只动电脑上那份，**不影响 iPod 上已经同步进去的歌**——界面上必须
  /// 说清楚，不然用户会以为"删了就没了"而不敢清。
  ///
  /// [songIds] 为空会**报错**（"没有选中任何歌曲"）——这是有意的：
  /// 把空集当"全删"的话，用户点了个没勾选的删除就把整份缓存清了。
  /// 清空全部走 [clearCache]。
  Future<String> removeCacheEntries(
    List<int> songIds, {
    bool deleteFiles = true,
  }) async {
    final json = await _post(
      '/api/cache/remove',
      body: <String, dynamic>{'song_ids': songIds, 'delete_files': deleteFiles},
    );
    return json['job_id']?.toString() ?? '';
  }

  // ── 本地音乐 ──────────────────────────────────────────────────────

  Future<TrackPage> tracks({
    String search = '',
    String sort = 'title',
    int page = 1,
    int size = 100,
  }) async => TrackPage.fromJson(
    await _get(
      '/api/library/tracks?search=${Uri.encodeQueryComponent(search)}'
      '&sort=$sort&page=$page&size=$size',
      timeout: const Duration(seconds: 30),
    ),
  );

  Future<ImportPreview> importPreview(List<String> paths) async =>
      ImportPreview.fromJson(
        await _post(
          '/api/library/import/preview',
          body: {'paths': paths},
          timeout: const Duration(seconds: 60),
        ),
      );

  Future<String> importFiles(List<String> paths) async {
    final json = await _post('/api/library/import', body: {'paths': paths});
    return json['job_id']?.toString() ?? '';
  }

  Future<RemovePreview> removePreview(List<String> ids) async =>
      RemovePreview.fromJson(
        await _post(
          '/api/library/remove/preview',
          body: {'ids': ids},
          timeout: const Duration(seconds: 60),
        ),
      );

  /// 执行删除。**必须带预览拿到的 [previewId]**——后端没有它就直接拒绝。
  Future<String> removeTracks(
    List<String> ids, {
    required String previewId,
  }) async {
    final json = await _post(
      '/api/library/remove',
      body: {'ids': ids, 'preview_id': previewId},
    );
    return json['job_id']?.toString() ?? '';
  }

  // ── 设备修复 ──────────────────────────────────────────────────────
  //
  // 扫描和执行是**两个独立的作业**：界面必须先把扫描结果摆给用户看、
  // 用户勾了哪几类，才发第二个请求。清理请求里带的是**类别开关**，
  // 不是"刚扫到的那批文件"——后端执行时会重新扫一遍再删，中间设备被
  // 外部改动也不会删错。

  /// 扫描设备。返回作业 id；结果在作业的 `result` 里（用 [job] 取）。
  Future<String> repairScan() async {
    final json = await _post('/api/repair/scan');
    // 与 repairClean 保持一致：后端可能用「200 + ok:false」拒绝。
    // 不校验的话，取不到 job_id 会静默返回空串，而调用方拿着空 jobId 的
    // _watch 永远不轮询 —— 界面停在「正在扫描 iPod…」且不给任何提示，
    // 只能靠「取消扫描」退出。
    if (json['ok'] == false) {
      throw ApiException(json['message']?.toString() ?? '扫描请求被拒绝');
    }
    return json['job_id']?.toString() ?? '';
  }

  /// 清掉选中的类别。返回作业 id。
  ///
  /// 三个开关默认全 false —— 清理不可逆，必须调用方显式勾选。
  Future<String> repairClean({
    bool orphans = false,
    bool strayTemp = false,
    bool brokenRecords = false,
  }) async {
    final json = await _post(
      '/api/repair/clean',
      body: <String, dynamic>{
        'orphans': orphans,
        'stray_temp': strayTemp,
        'broken_records': brokenRecords,
      },
    );
    if (json['ok'] == false) {
      throw ApiException(json['message']?.toString() ?? '修复请求被拒绝');
    }
    return json['job_id']?.toString() ?? '';
  }

  // ── iPod 上的歌单管理 ──────────────────────────────────────────────
  //
  // 注意跟「歌单」页那些方法区分：这里动的是 **iPod 设备上**的播放列表结构，
  // 不是网易云的在线歌单。写操作全部走作业队列，返回 job_id。

  Future<IpodPlaylistList> ipodPlaylists() async => IpodPlaylistList.fromJson(
    await _get('/api/library/playlists', timeout: const Duration(seconds: 30)),
  );

  Future<IpodPlaylistTracks> ipodPlaylistTracks(String playlistId) async =>
      IpodPlaylistTracks.fromJson(
        await _get(
          '/api/library/playlists/$playlistId/tracks',
          timeout: const Duration(seconds: 30),
        ),
      );

  Future<String> createIpodPlaylist(
    String name, {
    List<String> trackIds = const <String>[],
  }) async {
    final json = await _post(
      '/api/library/playlists/create',
      body: <String, dynamic>{'name': name, 'track_ids': trackIds},
    );
    return json['job_id']?.toString() ?? '';
  }

  Future<String> renameIpodPlaylist(String playlistId, String name) async {
    final json = await _post(
      '/api/library/playlists/$playlistId/rename',
      body: <String, dynamic>{'name': name},
    );
    return json['job_id']?.toString() ?? '';
  }

  /// 删除预览。**必须走这一步**——执行删除时要带回 [IpodPlaylistDeletePreview.previewId]。
  Future<IpodPlaylistDeletePreview> ipodPlaylistDeletePreview(
    String playlistId,
  ) async => IpodPlaylistDeletePreview.fromJson(
    await _post(
      '/api/library/playlists/delete/preview',
      body: <String, dynamic>{'playlist_id': playlistId},
    ),
  );

  Future<String> deleteIpodPlaylist(
    String playlistId, {
    required String previewId,
  }) async {
    final json = await _post(
      '/api/library/playlists/delete',
      body: <String, dynamic>{
        'playlist_id': playlistId,
        'preview_id': previewId,
      },
    );
    return json['job_id']?.toString() ?? '';
  }

  /// 往歌单里加 / 从歌单里移。传的是**增量**，不是整份替换。
  Future<String> editIpodPlaylistTracks(
    String playlistId, {
    List<String> add = const <String>[],
    List<String> remove = const <String>[],
  }) async {
    final json = await _post(
      '/api/library/playlists/$playlistId/tracks',
      body: <String, dynamic>{'add': add, 'remove': remove},
    );
    return json['job_id']?.toString() ?? '';
  }

  // ── 内部 ──────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> _get(String path, {Duration? timeout}) =>
      _send('GET', path, timeout: timeout);

  Future<Map<String, dynamic>> _post(
    String path, {
    Map<String, dynamic>? body,
    Duration? timeout,
  }) => _send('POST', path, body: body, timeout: timeout);

  Future<Map<String, dynamic>> _put(
    String path, {
    Map<String, dynamic>? body,
    Duration? timeout,
  }) => _send('PUT', path, body: body, timeout: timeout);

  Future<Map<String, dynamic>> _send(
    String method,
    String path, {
    Map<String, dynamic>? body,
    Duration? timeout,
  }) async {
    final uri = Uri.parse('$baseUrl$path');
    final limit = timeout ?? this.timeout;

    http.Response response;
    try {
      final request = http.Request(method, uri);
      if (body != null) {
        request.headers['Content-Type'] = 'application/json; charset=utf-8';
        request.bodyBytes = utf8.encode(jsonEncode(body));
      }
      final streamed = await _http.send(request).timeout(limit);
      response = await http.Response.fromStream(streamed).timeout(limit);
    } on TimeoutException {
      throw ApiException(
        '后端响应超时（$path）——它可能正忙。'
        '如果队列里有下载在跑，歌单读取要排在它后面。',
      );
    } catch (error) {
      throw ApiException('连不上后端（$baseUrl）：${_short(error)}');
    }

    return _decode(response, path);
  }

  Map<String, dynamic> _decode(http.Response response, String path) {
    Map<String, dynamic>? decoded;
    try {
      final raw = jsonDecode(utf8.decode(response.bodyBytes));
      if (raw is Map<String, dynamic>) decoded = raw;
    } catch (_) {
      // 解析失败继续往下走：非 200 的话真正有用的信息在状态码里
    }

    if (response.statusCode >= 400) {
      // FastAPI 的 HTTPException 给的是 detail；兜底处理器给的是 error。
      // 两者都是**已经写好的中文**，原样抛出去，别再包一层。
      final detail = decoded?['detail'] ?? decoded?['error'];
      final text = detail?.toString();
      throw ApiException(
        (text == null || text.isEmpty)
            ? '后端返回 ${response.statusCode}（$path）'
            : text,
        statusCode: response.statusCode,
      );
    }

    if (decoded == null) {
      throw ApiException('后端返回的内容解析不了（$path）');
    }
    return decoded;
  }

  static String _short(Object error) {
    final text = error.toString();
    return text.length > 120 ? '${text.substring(0, 120)}…' : text;
  }
}
