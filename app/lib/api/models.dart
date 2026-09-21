/// 后端返回的数据结构。
///
/// 全部按后端的 JSON 原样映射——**不在 Dart 侧做任何业务判断**
/// （哪些歌要下、能不能删，那些都在 Python 那边算好了）。
/// 这一层只负责"把 JSON 变成能用的对象"。
library;

import '../format.dart';

/// 顶部状态条用的综合状态。
class AppStatus {
  const AppStatus({
    required this.ipod,
    required this.account,
    required this.service,
    required this.jobs,
  });

  final IpodStatus ipod;
  final AccountStatus account;
  final ServiceStatus service;
  final JobsStatus jobs;

  factory AppStatus.fromJson(Map<String, dynamic> json) => AppStatus(
    ipod: IpodStatus.fromJson(_map(json['ipod'])),
    account: AccountStatus.fromJson(_map(json['account'])),
    service: ServiceStatus.fromJson(_map(json['netease_service'])),
    jobs: JobsStatus.fromJson(_map(json['jobs'])),
  );
}

class IpodStatus {
  const IpodStatus({
    required this.connected,
    required this.name,
    required this.tracks,
    required this.freeText,
    required this.hint,
  });

  final bool connected;
  final String name;
  final int tracks;
  final String freeText;
  final String hint;

  factory IpodStatus.fromJson(Map<String, dynamic> json) => IpodStatus(
    connected: json['connected'] == true,
    name: _str(json['name']),
    tracks: _int(json['tracks']),
    freeText: _str(json['free_text']),
    hint: _str(json['hint']),
  );

  /// 状态条上显示的一行字。
  String get summary {
    if (!connected) return '未连接';
    final parts = <String>[
      if (name.isNotEmpty) name,
      '$tracks 首',
      if (freeText.isNotEmpty) '剩余 $freeText',
    ];
    return parts.join(' · ');
  }
}

class AccountStatus {
  const AccountStatus({
    required this.loggedIn,
    required this.nickname,
    required this.uid,
    required this.vip,
    required this.accountCount,
  });

  final bool loggedIn;
  final String nickname;
  final int uid;
  final bool vip;
  final int accountCount;

  factory AccountStatus.fromJson(Map<String, dynamic> json) => AccountStatus(
    loggedIn: json['logged_in'] == true,
    nickname: _str(json['nickname']),
    uid: _int(json['uid']),
    vip: json['vip'] == true,
    accountCount: _int(json['account_count']),
  );

  String get summary {
    if (!loggedIn) return '未登录';
    return vip ? '$nickname（VIP）' : nickname;
  }
}

class ServiceStatus {
  const ServiceStatus({
    required this.reachable,
    required this.baseUrl,
    required this.detail,
  });

  final bool reachable;
  final String baseUrl;
  final String detail;

  factory ServiceStatus.fromJson(Map<String, dynamic> json) => ServiceStatus(
    reachable: json['reachable'] == true,
    baseUrl: _str(json['base_url']),
    detail: _str(json['detail']),
  );

  String get summary => reachable ? '服务正常' : '服务未启动';
}

class JobBrief {
  const JobBrief({
    required this.id,
    required this.title,
    required this.state,
    required this.stateText,
    required this.percent,
  });

  final String id;
  final String title;
  final String state;
  final String stateText;
  final double percent;

  factory JobBrief.fromJson(Map<String, dynamic> json) => JobBrief(
    id: _str(json['id']),
    title: _str(json['title']),
    state: _str(json['state']),
    stateText: _str(json['state_text']),
    percent: _double(json['percent']),
  );
}

class JobsStatus {
  const JobsStatus({
    required this.running,
    required this.queued,
    required this.hasWork,
  });

  final JobBrief? running;
  final int queued;
  final bool hasWork;

  factory JobsStatus.fromJson(Map<String, dynamic> json) => JobsStatus(
    running: json['running'] == null
        ? null
        : JobBrief.fromJson(_map(json['running'])),
    queued: _int(json['queued']),
    hasWork: json['has_work'] == true,
  );

  String get summary {
    final run = running;
    if (run == null) return queued > 0 ? '排队 $queued 个' : '空闲';
    return queued > 0 ? '${run.title}（+$queued 排队）' : run.title;
  }
}

/// 设置页「设备信息」那一组卡片。
class DeviceInfo {
  const DeviceInfo({
    required this.connected,
    required this.error,
    required this.hint,
    this.identity,
    this.storage,
    this.content,
    this.editableFields = const [],
  });

  final bool connected;
  final String error;
  final String hint;
  final DeviceIdentity? identity;
  final DeviceStorage? storage;
  final DeviceContent? content;

  /// 哪些字段可以改。**只有设备名**——序列号 / GUID 来自 Device/SysInfo，
  /// 是硬件信息。界面必须把这两类区分开，否则用户会以为都能改。
  final List<String> editableFields;

  bool canEdit(String field) => editableFields.contains(field);

  factory DeviceInfo.fromJson(Map<String, dynamic> json) => DeviceInfo(
    connected: json['connected'] == true,
    error: _str(json['error']),
    hint: _str(json['hint']),
    identity: json['identity'] == null
        ? null
        : DeviceIdentity.fromJson(_map(json['identity'])),
    storage: json['storage'] == null
        ? null
        : DeviceStorage.fromJson(_map(json['storage'])),
    content: json['content'] == null
        ? null
        : DeviceContent.fromJson(_map(json['content'])),
    editableFields: (json['editable_fields'] as List<dynamic>? ?? const [])
        .map((e) => e.toString())
        .toList(),
  );
}

class DeviceIdentity {
  const DeviceIdentity({
    required this.name,
    required this.modelNumber,
    required this.displayName,
    required this.family,
    required this.generation,
    required this.capacity,
    required this.color,
    required this.serial,
    required this.firewireGuid,
    required this.checksum,
    required this.mountPoint,
    required this.isSupported,
  });

  final String name;
  final String modelNumber;
  final String displayName;
  final String family;
  final String generation;
  final String capacity;
  final String color;
  final String serial;
  final String firewireGuid;
  final String checksum;
  final String mountPoint;
  final bool isSupported;

  factory DeviceIdentity.fromJson(Map<String, dynamic> json) => DeviceIdentity(
    name: _str(json['name']),
    modelNumber: _str(json['model_number']),
    displayName: _str(json['display_name']),
    family: _str(json['family']),
    generation: _str(json['generation']),
    capacity: _str(json['capacity']),
    color: _str(json['color']),
    serial: _str(json['serial']),
    firewireGuid: _str(json['firewire_guid']),
    checksum: _str(json['checksum']),
    mountPoint: _str(json['mount_point']),
    isSupported: json['is_supported'] == true,
  );
}

class DeviceStorage {
  const DeviceStorage({
    required this.totalBytes,
    required this.usedBytes,
    required this.freeBytes,
    required this.musicBytes,
    required this.totalText,
    required this.usedText,
    required this.freeText,
    required this.musicText,
    required this.usedPercent,
  });

  final int totalBytes;
  final int usedBytes;
  final int freeBytes;
  final int musicBytes;
  final String totalText;
  final String usedText;
  final String freeText;
  final String musicText;
  final double usedPercent;

  /// 占用比例，限制在 0~1（进度条用）。
  double get fraction => (usedPercent / 100).clamp(0.0, 1.0);

  factory DeviceStorage.fromJson(Map<String, dynamic> json) => DeviceStorage(
    totalBytes: _int(json['total_bytes']),
    usedBytes: _int(json['used_bytes']),
    freeBytes: _int(json['free_bytes']),
    musicBytes: _int(json['music_bytes']),
    totalText: _str(json['total_text']),
    usedText: _str(json['used_text']),
    freeText: _str(json['free_text']),
    musicText: _str(json['music_text']),
    usedPercent: _double(json['used_percent']),
  );
}

class DeviceContent {
  const DeviceContent({
    required this.tracks,
    required this.albums,
    required this.artists,
    required this.playlists,
    required this.playlistNames,
    required this.durationText,
  });

  final int tracks;
  final int albums;
  final int artists;
  final int playlists;
  final List<String> playlistNames;
  final String durationText;

  factory DeviceContent.fromJson(Map<String, dynamic> json) => DeviceContent(
    tracks: _int(json['tracks']),
    albums: _int(json['albums']),
    artists: _int(json['artists']),
    playlists: _int(json['playlists']),
    playlistNames: (json['playlist_names'] as List<dynamic>? ?? const [])
        .map((e) => e.toString())
        .toList(),
    durationText: _str(json['duration_text']),
  );
}

// ── 小工具：JSON 里拿到的东西类型不一定可靠，统一兜一下 ──────────────

Map<String, dynamic> _map(dynamic value) =>
    value is Map<String, dynamic> ? value : const {};

String _str(dynamic value) => value?.toString() ?? '';

int _int(dynamic value) {
  if (value is int) return value;
  if (value is num) return value.toInt();
  return int.tryParse(value?.toString() ?? '') ?? 0;
}

double _double(dynamic value) {
  if (value is num) return value.toDouble();
  return double.tryParse(value?.toString() ?? '') ?? 0.0;
}

bool _bool(dynamic value) {
  if (value is bool) return value;
  if (value is num) return value != 0;
  final text = value?.toString().toLowerCase();
  return text == 'true' || text == '1';
}

List<dynamic> _list(dynamic value) => value is List ? value : const <dynamic>[];

// ──────────────────────────────────────────────────────────────────────
// 账号 / 登录
// ──────────────────────────────────────────────────────────────────────

/// 一个登录过的账号。
class AccountItem {
  const AccountItem({
    required this.uid,
    required this.nickname,
    required this.vip,
    required this.active,
  });

  final int uid;
  final String nickname;
  final bool vip;
  final bool active;

  factory AccountItem.fromJson(Map<String, dynamic> json) => AccountItem(
    uid: _int(json['uid']),
    nickname: _str(json['nickname']),
    vip: _bool(json['vip']),
    active: _bool(json['active']),
  );
}

/// 扫码登录会话。
///
/// [state] 的取值：idle / starting / waiting / scanned / confirmed /
/// expired / failed / cancelled。界面按它决定显示二维码还是提示文案。
class LoginState {
  const LoginState({
    required this.sessionId,
    required this.state,
    required this.message,
    required this.finished,
    required this.imageBase64,
    this.account,
    this.requestCount = 0,
  });

  final String sessionId;
  final String state;
  final String message;
  final bool finished;
  final String imageBase64;
  final AccountItem? account;
  final int requestCount;

  static const LoginState idle = LoginState(
    sessionId: '',
    state: 'idle',
    message: '未在登录',
    finished: true,
    imageBase64: '',
  );

  bool get hasImage => imageBase64.isNotEmpty;

  bool get succeeded => state == 'confirmed';

  factory LoginState.fromJson(Map<String, dynamic> json) {
    final account = json['account'];
    return LoginState(
      sessionId: _str(json['session_id']),
      state: _str(json['state']),
      message: _str(json['message']),
      finished: _bool(json['finished']),
      imageBase64: _str(json['image_base64']),
      requestCount: _int(json['request_count']),
      account: account is Map ? AccountItem.fromJson(_map(account)) : null,
    );
  }
}

// ──────────────────────────────────────────────────────────────────────
// 设置 / 缓存 / 环境
// ──────────────────────────────────────────────────────────────────────

class QualityOption {
  const QualityOption({required this.value, required this.label});

  final String value;
  final String label;

  factory QualityOption.fromJson(Map<String, dynamic> json) =>
      QualityOption(value: _str(json['value']), label: _str(json['label']));
}

class AppSettings {
  const AppSettings({
    required this.quality,
    required this.qualityLabel,
    required this.qualityOptions,
    required this.minInterval,
    required this.minIntervalFloor,
  });

  final String quality;
  final String qualityLabel;
  final List<QualityOption> qualityOptions;
  final double minInterval;
  final double minIntervalFloor;

  factory AppSettings.fromJson(Map<String, dynamic> json) => AppSettings(
    quality: _str(json['quality']),
    qualityLabel: _str(json['quality_label']),
    qualityOptions: _list(json['quality_options'])
        .map((e) => QualityOption.fromJson(_map(e)))
        .toList(),
    minInterval: _double(json['min_interval']),
    minIntervalFloor: _double(json['min_interval_floor']),
  );
}

class CacheInfo {
  const CacheInfo({
    required this.dir,
    required this.files,
    required this.records,
    required this.sizeText,
    required this.consistent,
  });

  final String dir;
  final int files;
  final int records;
  final String sizeText;
  final bool consistent;

  factory CacheInfo.fromJson(Map<String, dynamic> json) => CacheInfo(
    dir: _str(json['dir']),
    files: _int(json['files']),
    records: _int(json['records']),
    sizeText: _str(json['size_text']),
    consistent: _bool(json['consistent']),
  );
}

class EnvInfo {
  const EnvInfo({
    required this.python,
    required this.ipodCli,
    required this.ffmpeg,
    required this.ffmpegOk,
    required this.dbPath,
    required this.cacheDir,
    required this.baseUrl,
  });

  final String python;
  final String ipodCli;
  final String ffmpeg;
  final bool ffmpegOk;
  final String dbPath;
  final String cacheDir;
  final String baseUrl;

  factory EnvInfo.fromJson(Map<String, dynamic> json) => EnvInfo(
    python: _str(json['python']),
    ipodCli: _str(json['ipod_cli']),
    ffmpeg: _str(json['ffmpeg']),
    ffmpegOk: _bool(json['ffmpeg_ok']),
    dbPath: _str(json['db_path']),
    cacheDir: _str(json['cache_dir']),
    baseUrl: _str(json['base_url']),
  );
}

// ──────────────────────────────────────────────────────────────────────
// 作业
// ──────────────────────────────────────────────────────────────────────

/// 导出诊断记录的结果。
class ExportResult {
  const ExportResult({
    required this.path,
    required this.bytes,
    required this.lines,
    required this.jobs,
  });

  /// 导出文件的绝对路径。界面要显示它——告诉了路径，用户才找得到文件。
  final String path;
  final int bytes;
  final int lines;
  final int jobs;

  String get sizeText => humanSize(bytes);
}

/// 下载作业里某一首歌的状态。
class JobItem {
  const JobItem({
    required this.index,
    required this.name,
    required this.artist,
    required this.status,
    required this.size,
    required this.reason,
  });

  /// 第几首（从 1 数起，跟界面上的序号一致）。
  final int index;
  final String name;
  final String artist;

  /// downloading / done / failed
  final String status;

  /// 字节数，只在 done 时有意义。
  final int size;

  /// 失败原因，只在 failed 时有意义。
  final String reason;

  bool get done => status == 'done';
  bool get failed => status == 'failed';
  bool get downloading => status == 'downloading';

  String get statusText => switch (status) {
    'done' => '已下载',
    'failed' => '失败',
    'downloading' => '下载中',
    _ => '等待',
  };

  String get sizeText => humanSize(size);

  factory JobItem.fromJson(Map<String, dynamic> json) => JobItem(
    index: _int(json['index']),
    name: _str(json['name']),
    artist: _str(json['artist']),
    status: _str(json['status']),
    size: _int(json['size']),
    reason: _str(json['reason']),
  );
}

class JobInfo {
  const JobInfo({
    required this.id,
    required this.kind,
    required this.title,
    required this.state,
    required this.stateText,
    required this.total,
    required this.done,
    required this.current,
    required this.percent,
    required this.error,
    required this.message,
    required this.items,
    required this.itemsDone,
    required this.itemsFailed,
    required this.etaSeconds,
    required this.elapsed,
  });

  final String id;
  final String kind;
  final String title;
  final String state;
  final String stateText;
  final int total;
  final int done;
  final String current;
  final double percent;
  final String error;
  final String message;

  /// 逐首歌的进度。只有下载/同步类作业会有。
  final List<JobItem> items;

  /// items 里成功/失败的数目。
  final int itemsDone;
  final int itemsFailed;

  /// 预计还要多久（秒）。后端只在能算出来的时候给。
  final int? etaSeconds;

  /// 已经跑了多久（秒）。
  final int elapsed;

  bool get running => state == 'running';
  bool get finished =>
      state == 'done' || state == 'failed' || state == 'cancelled';

  /// 是不是"下载歌"那一类作业。下载页只关心这类。
  bool get isDownload => kind == 'download' || kind == 'sync';

  /// 有逐首歌的记录可看。
  bool get hasItems => items.isNotEmpty;

  /// 单首失败的原因（作业整体成功但个别歌失败时，错误在这一层）。
  List<JobItem> get failures =>
      items.where((i) => i.failed).toList(growable: false);

  factory JobInfo.fromJson(Map<String, dynamic> json) {
    final result = _map(json['result']);
    return JobInfo(
      id: _str(json['id']),
      kind: _str(json['kind']),
      title: _str(json['title']),
      state: _str(json['state']),
      stateText: _str(json['state_text']),
      total: _int(json['total']),
      done: _int(json['done']),
      current: _str(json['current']),
      percent: _double(json['percent']),
      error: _str(json['error']),
      message: _str(result['message']),
      items: _list(json['items'])
          .map((e) => JobItem.fromJson(_map(e)))
          .where((i) => i.index > 0)
          .toList(growable: false),
      itemsDone: _int(json['items_done']),
      itemsFailed: _int(json['items_failed']),
      etaSeconds: json['eta_seconds'] == null
          ? null
          : (json['eta_seconds'] as num).toInt(),
      elapsed: _double(json['elapsed']).toInt(),
    );
  }
}

class JobsSnapshot {
  const JobsSnapshot({this.running, required this.queued, required this.jobs});

  final JobInfo? running;
  final int queued;
  final List<JobInfo> jobs;

  bool get hasWork => running != null || queued > 0;

  /// 该在下载页显示的那个作业。
  ///
  /// 下载页只关心"下载歌"这一件事，别的（备份、健康检查、导入）归别处。
  /// 挑选顺序：正在跑的 > 最近一个**有逐首歌记录**的。
  /// 后者是刻意的——一个刚提交还没开跑的作业 items 是空的，
  /// 显示它的话页面会突然从"上次的 246 首"变成一片空白。
  JobInfo? get showcase {
    final downloads = jobs.where((j) => j.isDownload).toList(growable: false);
    if (downloads.isEmpty) return null;

    for (final job in downloads) {
      if (!job.finished) return job;
    }
    for (final job in downloads) {
      if (job.hasItems) return job;
    }
    return downloads.first;
  }

  factory JobsSnapshot.fromJson(Map<String, dynamic> json) {
    final running = json['running'];
    return JobsSnapshot(
      running: running is Map ? JobInfo.fromJson(_map(running)) : null,
      queued: _int(json['queued']),
      jobs: _list(json['jobs']).map((e) => JobInfo.fromJson(_map(e))).toList(),
    );
  }
}

/// 日志的一行。`seq` 是增量拉取的游标。
class LogLine {
  const LogLine({
    required this.seq,
    required this.ts,
    required this.level,
    required this.text,
  });

  final int seq;
  final String ts;
  final String level;
  final String text;

  factory LogLine.fromJson(Map<String, dynamic> json) => LogLine(
    seq: _int(json['seq']),
    ts: _str(json['ts']),
    level: _str(json['level']),
    text: _str(json['text']),
  );
}

class LogPage {
  const LogPage({required this.lines, required this.latestSeq});

  final List<LogLine> lines;
  final int latestSeq;

  factory LogPage.fromJson(Map<String, dynamic> json) => LogPage(
    lines: _list(json['lines']).map((e) => LogLine.fromJson(_map(e))).toList(),
    latestSeq: _int(json['latest_seq']),
  );
}

// ──────────────────────────────────────────────────────────────────────
// 歌单
// ──────────────────────────────────────────────────────────────────────

class PlaylistSummary {
  const PlaylistSummary({
    required this.id,
    required this.name,
    required this.trackCount,
    required this.creator,
    required this.liked,
  });

  final int id;
  final String name;
  final int trackCount;
  final String creator;
  final bool liked;

  factory PlaylistSummary.fromJson(Map<String, dynamic> json) =>
      PlaylistSummary(
        id: _int(json['id']),
        name: _str(json['name']),
        trackCount: _int(json['track_count']),
        creator: _str(json['creator']),
        liked: _bool(json['liked']),
      );
}

class PlaylistList {
  const PlaylistList({
    required this.playlists,
    required this.cached,
    required this.loading,
    required this.message,
  });

  final List<PlaylistSummary> playlists;
  final bool cached;
  final bool loading;
  final String message;

  factory PlaylistList.fromJson(Map<String, dynamic> json) => PlaylistList(
    playlists: _list(json['playlists'])
        .map((e) => PlaylistSummary.fromJson(_map(e)))
        .toList(),
    cached: _bool(json['cached']),
    loading: _bool(json['loading']),
    message: _str(json['message']),
  );
}

/// 一首歌在歌单里的样子 + 它的同步状态。
class SongRow {
  const SongRow({
    required this.id,
    required this.name,
    required this.artist,
    required this.album,
    required this.durationText,
    required this.status,
    required this.onIpod,
    required this.local,
    this.device = 'unknown',
  });

  final int id;
  final String name;
  final String artist;
  final String album;
  final String durationText;
  final String status;

  /// 在 iPod 上。
  final bool onIpod;

  /// 本地那份下载文件还在（**核对过文件**，不是只看记录）。
  final bool local;

  /// 设备这一维：`on_ipod` / `off_ipod` / `unknown`（没插设备，判断不了）。
  final String device;

  /// 行上显示的文案。
  ///
  /// **两个维度分开说**：歌在 iPod 上、本地却已经删了，是再正常不过的状态，
  /// 但它对"下载到本地"来说是"有事可做"。只显示"已同步"会让用户
  /// 以为不用管——那正是"删了本地想重下却提示已就绪"的来源。
  /// 三态：**未下载 / 已下载 / 已同步**。
  ///
  /// 第四种情况（同步过、但本地那份已经删了）做成**后缀**而不是独立状态：
  /// 状态数保持三个，但不会把"本地没了"这件事藏起来——上一轮就因为
  /// 藏起来出过 bug（"删了本地想重下却提示已就绪"）。
  /// 本地这一维的说法。
  ///
  /// ★ 两个维度**分开显示**，不再压成一个词。以前合成"已同步/已下载/未下载"，
  /// 于是"设备上有、本地没留"被一个词盖住——界面上的「一键选中所有未下载的」
  /// 永远选不到它，用户以为点下载没反应。现在徽章各说各的。
  String get localText => local ? '本地已有' : '本地没有';

  /// 设备这一维的说法。
  ///
  /// `unknown`（没插设备）**不等于**"不在设备上"。不知道就说不知道——
  /// 以前没插设备时后端返回空集，界面把同步过的歌全标成未同步，
  /// 用户以为白同步了。
  String get deviceText => switch (device) {
        'on_ipod' => 'iPod 上已有',
        'off_ipod' => 'iPod 上没有',
        _ => '未插 iPod', // unknown：不知道
      };

  /// 合并成一行的说法（给不区分维度的老地方用）。
  String get statusText {
    if (device == 'unknown') {
      if (local) return '本地已有 · 未插 iPod';
      return '未下载 · 未插 iPod';
    }
    if (onIpod && !local) return '已同步 · 本地无';
    if (onIpod) return '已同步';
    if (local) return '已下载';
    return '未下载';
  }

  /// 设备状态可不可信（没插设备时为 false）。
  bool get deviceKnown => device != 'unknown';

  /// 能不能勾选：**有活干**就能勾。
  ///
  /// 不能只看"在不在 iPod 上"。歌在 iPod 上、本地那份已经删了，
  /// 对「下载到本地」来说是有事可做的——以前这种行直接不给勾，
  /// 用户想重新下都选不中（跟"提示已就绪"是同一个病根）。
  ///
  /// 两边都有才是真的没事可做，那种行才置灰。
  bool get selectable => !(onIpod && local);

  factory SongRow.fromJson(Map<String, dynamic> json) {
    final ms = _int(json['duration_ms']);
    final total = ms ~/ 1000;
    return SongRow(
      id: _int(json['id']),
      name: _str(json['name']),
      artist: _str(json['artist']),
      album: _str(json['album']),
      durationText: ms <= 0
          ? '--:--'
          : '${total ~/ 60}:${(total % 60).toString().padLeft(2, '0')}',
      status: _str(json['status']),
      onIpod: _bool(json['on_ipod']),
      local: _bool(json['local']),
      device: json['device'] == null ? 'unknown' : _str(json['device']),
    );
  }
}

/// 曲目列表的状态筛选。
///
/// ★ 标签要把**维度**说清楚：前两个只看本地，第三个只看设备。
/// 以前三个标签是"未下载 / 已下载 / 已同步"，看着像互斥的三态，
/// 实际上"设备上有、本地没留"的歌既该算未下载、又算已同步——
/// 用户就卡在这个自相矛盾上（"未下载已同步就不能下载"）。
///
/// `value` 保持不动——它是跟后端的协议，改标签不该动协议。
enum SongFilter {
  all('all', '全部'),
  pending('pending', '本地未下载'),
  downloaded('downloaded', '本地已下载'),
  onIpod('on_ipod', 'iPod 上已有');

  const SongFilter(this.value, this.label);

  /// 传给后端的值。
  final String value;

  /// 界面上显示的中文。
  final String label;
}

class PlaylistSongs {
  const PlaylistSongs({
    required this.playlist,
    required this.songs,
    required this.page,
    required this.pages,
    required this.total,
    required this.filtered,
    required this.filter,
    required this.onIpod,
    required this.downloaded,
    required this.pending,
    required this.loading,
    required this.message,
    this.localOk = 0,
    this.localMissing = 0,
    this.offIpod = 0,
    this.deviceUnknown = false,
    this.both = 0,
    this.onIpodButNoLocal = 0,
    this.localButNotOnIpod = 0,
  });

  final String playlist;
  final List<SongRow> songs;
  final int page;
  final int pages;

  /// 整单曲目数（不受筛选影响）。
  final int total;

  /// 当前筛选命中的曲目数（跨全部页）。
  final int filtered;

  final SongFilter filter;

  // 下面这些是**整单**的统计，不随筛选变——界面顶部要显示。

  // ── 本地这一维 ──
  /// 本地文件确实在（核对过文件的）。
  final int localOk;

  /// 本地没有 —— **这就是"点下载有事可做"的那批**，跟设备无关。
  final int localMissing;

  // ── 设备这一维 ──
  /// 真在设备上。
  final int onIpod;

  /// 查过了，不在设备上。
  final int offIpod;

  /// ★ 没插设备（或库读不出来）——这一维**判断不了**。
  ///
  /// 有了这个标志，界面才能说"未插 iPod"而不是假装"都没同步"。
  final bool deviceUnknown;

  /// 两边都齐的（真的没事可做）。
  final int both;

  /// 设备上有、本地没留（该重新下到本地）。
  final int onIpodButNoLocal;

  /// 本地有、还没进设备（该同步）。
  final int localButNotOnIpod;

  // ── 以下三个是旧字段，语义已修正，留作兼容 ──
  /// 未下载 = **本地没有**（跟设备无关）。
  final int pending;
  final int downloaded;

  final bool loading;
  final String message;

  factory PlaylistSongs.fromJson(Map<String, dynamic> json) {
    final counts = _map(json['counts']);
    final raw = _str(json['status']);
    return PlaylistSongs(
      playlist: _str(json['playlist']),
      songs: _list(json['songs'])
          .map((e) => SongRow.fromJson(_map(e)))
          .toList(),
      page: _int(json['page']),
      pages: _int(json['pages']),
      total: _int(json['total']),
      filtered: _int(json['filtered']),
      filter: SongFilter.values.firstWhere(
        (f) => f.value == raw,
        orElse: () => SongFilter.all,
      ),
      onIpod: _int(counts['on_ipod']),
      downloaded: _int(counts['downloaded']),
      pending: _int(counts['pending']),
      localOk: counts['local_ok'] == null
          ? _int(counts['downloaded'])
          : _int(counts['local_ok']),
      localMissing: counts['local_missing'] == null
          ? _int(counts['pending'])
          : _int(counts['local_missing']),
      offIpod: _int(counts['off_ipod']),
      deviceUnknown: _bool(counts['device_unknown']),
      both: _int(counts['both']),
      onIpodButNoLocal: _int(counts['on_ipod_but_no_local']),
      localButNotOnIpod: _int(counts['local_but_not_on_ipod']),
      loading: _bool(json['loading']),
      message: _str(json['message']),
    );
  }
}

/// 下载前的规划预览。
class PlanPreview {
  const PlanPreview({
    this.loading = false,
    this.message = '',
    required this.total,
    required this.toDownload,
    required this.needsFetch,
    required this.alreadyReady,
    required this.unavailable,
    required this.estimatedMb,
    required this.preview,
    required this.skipReasons,
  });

  final int total;
  final int toDownload;
  final int needsFetch;
  final int alreadyReady;
  final int unavailable;
  final double estimatedMb;
  final List<String> preview;

  /// 跳过的原因 → 数量。比如 {"已同步": 3, "已下载": 2}。
  ///
  /// 只说"已就绪 N 首"会让用户以为没事可做——但"已同步"和
  /// "本地已有"是两件不同的事，尤其在他点的是「下载到本地」的时候。
  final Map<String, int> skipReasons;

  /// 后端没能在等待窗口内算完（前面排着别的作业）。这时候数字都是 0，
  /// **不能当成"没什么要下的"**——那会误导用户以为不用下了。
  final bool loading;

  /// `loading` 为真时的说明。
  final String message;

  factory PlanPreview.fromJson(Map<String, dynamic> json) => PlanPreview(
    loading: _bool(json['loading']),
    message: _str(json['message']),
    total: _int(json['total']),
    toDownload: _int(json['to_download']),
    needsFetch: _int(json['needs_fetch']),
    alreadyReady: _int(json['already_ready']),
    unavailable: _int(json['unavailable']),
    estimatedMb: _double(json['estimated_mb']),
    preview: _list(json['preview']).map((e) {
      final item = _map(e);
      final artist = _str(item['artist']);
      final name = _str(item['name']);
      return artist.isEmpty ? name : '$name - $artist';
    }).toList(),
    skipReasons: _map(json['skip_reasons'])
        .map((key, value) => MapEntry(key, _int(value))),
  );
}

// ──────────────────────────────────────────────────────────────────────
// 本地音乐（iPod 上的曲目）
// ──────────────────────────────────────────────────────────────────────

class TrackRow {
  const TrackRow({
    required this.index,
    required this.dbId,
    required this.title,
    required this.artist,
    required this.album,
    required this.size,
    required this.sizeText,
    required this.lengthText,
    required this.bitrate,
  });

  final int index;

  /// iPod 持久 ID。**字符串**：它是无符号 64 位，超过 2^63-1 的
  /// 当成数字会静默溢出成负数（Dart 的 int 是有符号的）。
  final String dbId;
  final String title;
  final String artist;
  final String album;
  final int size;
  final String sizeText;
  final String lengthText;
  final int bitrate;

  factory TrackRow.fromJson(Map<String, dynamic> json) => TrackRow(
    index: _int(json['index']),
    dbId: _str(json['db_id']),
    title: _str(json['title']),
    artist: _str(json['artist']),
    album: _str(json['album']),
    size: _int(json['size']),
    sizeText: _str(json['size_text']),
    lengthText: _str(json['length_text']),
    bitrate: _int(json['bitrate']),
  );
}

class TrackPage {
  const TrackPage({
    required this.tracks,
    required this.total,
    required this.filtered,
    required this.page,
    required this.pages,
    required this.totalText,
    required this.filteredBytes,
    required this.allBytes,
    required this.ipodName,
    required this.sorts,
  });

  final List<TrackRow> tracks;
  final int total;
  final int filtered;
  final int page;
  final int pages;
  final String totalText;

  /// 筛选结果的总体积（字节）。"全选筛选结果"时用它显示已选体积。
  final int filteredBytes;

  /// 库里全部曲目的体积（字节）。
  final int allBytes;

  final String ipodName;
  final List<QualityOption> sorts;

  factory TrackPage.fromJson(Map<String, dynamic> json) => TrackPage(
    tracks: _list(json['tracks'])
        .map((e) => TrackRow.fromJson(_map(e)))
        .toList(),
    total: _int(json['total']),
    filtered: _int(json['filtered']),
    page: _int(json['page']),
    pages: _int(json['pages']),
    totalText: _str(json['total_text']),
    filteredBytes: _int(json['filtered_bytes']),
    allBytes: _int(json['all_bytes']),
    ipodName: _str(json['ipod_name']),
    sorts: _list(json['sorts'])
        .map((e) => QualityOption.fromJson(_map(e)))
        .toList(),
  );
}

class ImportPreview {
  const ImportPreview({
    required this.files,
    required this.toAdd,
    required this.transcode,
    required this.skipped,
    required this.errored,
    required this.fits,
    required this.sizeText,
    required this.freeText,
    required this.items,
  });

  final int files;
  final int toAdd;
  final int transcode;
  final int skipped;
  final int errored;
  final bool fits;
  final String sizeText;
  final String freeText;
  final List<PreviewItem> items;

  /// 一句话摘要，直接显示给用户看。
  String get summary {
    final parts = <String>['$files 个文件', sizeText];
    if (transcode > 0) parts.add('$transcode 个需转码 FLAC→ALAC');
    if (skipped > 0) parts.add('$skipped 个已存在将跳过');
    if (errored > 0) parts.add('$errored 个不支持将忽略');
    return parts.join(' · ');
  }

  factory ImportPreview.fromJson(Map<String, dynamic> json) => ImportPreview(
    files: _int(json['files']),
    toAdd: _int(json['to_add']),
    transcode: _int(json['transcode']),
    skipped: _int(json['skipped']),
    errored: _int(json['errored']),
    fits: _bool(json['fits']),
    sizeText: _str(json['size_text']),
    freeText: _str(json['free_text']),
    items: _list(json['items'])
        .map((e) => PreviewItem.fromJson(_map(e)))
        .toList(),
  );
}

class PreviewItem {
  const PreviewItem({
    required this.name,
    required this.title,
    required this.artist,
    required this.action,
    required this.reason,
    required this.sizeText,
  });

  final String name;
  final String title;

  /// 只有删除预览会带艺人（导入预览的对象是电脑上的文件，还没读元数据）
  final String artist;
  final String action;
  final String reason;
  final String sizeText;

  String get actionText => switch (action) {
    'add' => '拷入',
    'transcode' => '转码',
    'skip' => '跳过',
    'error' => '不支持',
    _ => action,
  };

  factory PreviewItem.fromJson(Map<String, dynamic> json) => PreviewItem(
    name: _str(json['name']),
    title: _str(json['title']),
    artist: _str(json['artist']),
    action: _str(json['action']),
    reason: _str(json['reason']),
    sizeText: _str(json['size_text']),
  );
}

class RemovePreview {
  const RemovePreview({
    required this.previewId,
    required this.count,
    required this.remaining,
    required this.sizeText,
    required this.items,
  });

  /// 执行删除时必须把它带回去。
  ///
  /// 后端要求"先预览、再删"，缺了这个令牌会直接 409——
  /// 这样"弹确认框"就成了结构性的保证，而不是界面自觉。
  final String previewId;

  final int count;
  final int remaining;
  final String sizeText;
  final List<PreviewItem> items;

  factory RemovePreview.fromJson(Map<String, dynamic> json) => RemovePreview(
    previewId: _str(json['preview_id']),
    count: _int(json['count']),
    remaining: _int(json['remaining']),
    sizeText: _str(json['size_text']),
    items: _list(json['items'])
        .map((e) => PreviewItem.fromJson(_map(e)))
        .toList(),
  );
}

class VerifyCheck {
  const VerifyCheck({
    required this.name,
    required this.status,
    required this.summary,
    required this.details,
  });

  final String name;
  final String status;
  final String summary;
  final List<String> details;

  factory VerifyCheck.fromJson(Map<String, dynamic> json) => VerifyCheck(
    name: _str(json['name']),
    status: _str(json['status']),
    summary: _str(json['summary']),
    details: _list(json['details']).map((e) => e.toString()).toList(),
  );
}


// ──────────────────────────────────────────────────────────────────────
// iPod 上的播放列表（跟「歌单」页那些**网易云在线歌单**不是一回事）
// ──────────────────────────────────────────────────────────────────────

/// iPod 上的一个播放列表。
///
/// `editable` 为 false 时 `readonlyReason` 一定有值——界面必须把原因显示出来，
/// 而不是让某些歌单"莫名不能点"。主列表（两个数据集各一个）和智能/播客列表
/// 都属于这一类。
class IpodPlaylist {
  const IpodPlaylist({
    required this.playlistId,
    required this.name,
    required this.count,
    required this.dataset,
    required this.datasetText,
    required this.isMaster,
    required this.isDeviceName,
    required this.editable,
    required this.readonlyReason,
  });

  /// 播放列表 id（**字符串**：64 位无符号，理由同 TrackRow.dbId）。
  final String playlistId;
  final String name;
  final int count;

  /// `mhlp` / `mhlp_podcast` / `mhlp_smart`
  final String dataset;

  /// 中文的数据集名（普通 / 播客 / 智能）。
  final String datasetText;

  /// 是不是本数据集的主列表。**普通和播客各有一个**，两个标题都是 iPod 的名字，
  /// 所以列表里看到两条同名是正常的（靠 datasetText 区分）。
  final bool isMaster;

  /// 是不是"iPod 名字的载体"（只有普通数据集的主列表是）。
  final bool isDeviceName;

  final bool editable;
  final String readonlyReason;

  factory IpodPlaylist.fromJson(Map<String, dynamic> json) => IpodPlaylist(
    playlistId: _str(json['playlist_id']),
    name: _str(json['name']),
    count: _int(json['count']),
    dataset: _str(json['dataset']),
    datasetText: _str(json['dataset_text']),
    isMaster: json['is_master'] == true,
    isDeviceName: json['is_device_name'] == true,
    editable: json['editable'] != false,
    readonlyReason: _str(json['readonly_reason']),
  );
}

class IpodPlaylistList {
  const IpodPlaylistList({
    required this.playlists,
    required this.masterId,
    required this.trackCount,
  });

  final List<IpodPlaylist> playlists;

  /// 普通数据集主列表的 id（设备名存在它标题里）。
  final String masterId;
  final int trackCount;

  factory IpodPlaylistList.fromJson(Map<String, dynamic> json) => IpodPlaylistList(
    playlists: _list(json['playlists'])
        .map((e) => IpodPlaylist.fromJson(e as Map<String, dynamic>))
        .toList(),
    masterId: _str(json['master_id']),
    trackCount: _int(json['track_count']),
  );
}

class IpodPlaylistTracks {
  const IpodPlaylistTracks({
    required this.playlistId,
    required this.name,
    required this.datasetText,
    required this.tracks,
    required this.editable,
    required this.readonlyReason,
  });

  final String playlistId;
  final String name;
  final String datasetText;
  final List<TrackRow> tracks;
  final bool editable;
  final String readonlyReason;

  factory IpodPlaylistTracks.fromJson(Map<String, dynamic> json) =>
      IpodPlaylistTracks(
        playlistId: _str(json['playlist_id']),
        name: _str(json['name']),
        datasetText: _str(json['dataset_text']),
        tracks: _list(json['tracks'])
            .map((e) => TrackRow.fromJson(e as Map<String, dynamic>))
            .toList(),
        editable: json['editable'] != false,
        readonlyReason: _str(json['readonly_reason']),
      );
}

/// 删除歌单的预览结果。
class IpodPlaylistDeletePreview {
  const IpodPlaylistDeletePreview({
    required this.previewId,
    required this.name,
    required this.count,
    required this.note,
  });

  /// 执行删除时必须带回它（后端会拒绝没有真实预览过的删除）。
  final String previewId;
  final String name;
  final int count;

  /// 后端写好的中文说明（含"歌本身不会被删"这句）。
  final String note;

  factory IpodPlaylistDeletePreview.fromJson(Map<String, dynamic> json) =>
      IpodPlaylistDeletePreview(
        previewId: _str(json['preview_id']),
        name: _str(json['name']),
        count: _int(json['count']),
        note: _str(json['note']),
      );
}
