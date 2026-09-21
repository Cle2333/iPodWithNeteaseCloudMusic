/// 歌单页的逐个勾选 / 筛选 / 批量下载。
///
/// 这个文件守两件容易出错、代价又大的事：
///
/// 1. **「一键选中所有未下载的」必须覆盖整单，不是当前页**。
///    246 首的歌单 50 首一页，只勾当前页的话用户以为全选上了，
///    实际任务只跑 50 首。
/// 2. **下发时要带对 `song_ids`**。忘了带 = 整个歌单开始下载（几百首、
///    几个 G）；带错了 = 下错歌。所以这里连请求体都断言。
library;

import 'dart:convert';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:ipod_manager/api/client.dart';
import 'package:ipod_manager/api/models.dart';
import 'package:ipod_manager/pages/playlist_page.dart';
import 'package:ipod_manager/state/app_state.dart';
import 'package:ipod_manager/theme.dart';

/// 造一个歌单：`total` 首，其中前 `onIpod` 首状态是"已同步"、
/// 中间 `downloaded` 首是"已下载"，其余"未下载"。
///
/// 分页按 [size] 切，status 筛选也在服务端做——跟真后端一致。
/// 第 i 首本地有没有文件。
///
/// 在 iPod 上的前 [onIpodNoLocal] 首**本地没留**（这种是"有事可做"的），
/// 其余本地也留着；不在 iPod 上的按 [downloaded] 算本地有没有。
bool _localOf(int i, int onIpod, int onIpodNoLocal, int downloaded) =>
    i < onIpod ? (i >= onIpodNoLocal) : (i < onIpod + downloaded);

Map<String, dynamic> songsPayload({
  int total = 5,
  int onIpod = 1,
  int downloaded = 1,

  /// 其中"在 iPod 上但**本地没留**"的有几首。
  ///
  /// 这种行**是可以勾的**——用户点「下载到本地」时它有事可做。
  /// 以前这种行直接被置灰，用户想重新下都选不中。
  int onIpodNoLocal = 0,

  /// 没插设备（或库读不出来）。这时设备这一维**判断不了**——
  /// 后端会回 device_unknown=true、每首歌 device='unknown'。
  bool deviceUnknown = false,
  int page = 1,
  int size = 50,
  String status = 'all',
  String search = '',
}) {
  final all = <Map<String, dynamic>>[
    for (var i = 0; i < total; i++)
      <String, dynamic>{
        'id': 1000 + i,
        'name': '歌曲$i',
        'artist': '歌手$i',
        'album': '专辑$i',
        'duration_ms': 200000,
        // ★ 现在跟真后端一样，**两个维度各给一份**：
        //   status 只说本地（pending / downloaded），device 只说设备。
        //   以前 status 是合成三态，夹具也就跟着合成——测不出"设备上有、
        //   本地没留"到底算哪一档。
        'status': _localOf(i, onIpod, onIpodNoLocal, downloaded) 
            ? 'downloaded'
            : 'pending',
        'on_ipod': !deviceUnknown && i < onIpod,
        'local': _localOf(i, onIpod, onIpodNoLocal, downloaded),
        'device': deviceUnknown
            ? 'unknown'
            : (i < onIpod ? 'on_ipod' : 'off_ipod'),
      },
  ];
  var matched = status == 'all'
      ? all
      : all.where((s) => s['status'] == status).toList();

  // 搜索是**服务端**筛的（客户端只能筛当前页）——假实现必须照这个语义，
  // 否则测出来的"能搜到"是假象
  final query = search.trim().toLowerCase();
  if (query.isNotEmpty) {
    matched = matched
        .where(
          (s) => '${s['name']}\n${s['artist']}'.toLowerCase().contains(query),
        )
        .toList();
  }

  final start = (page - 1) * size;
  final window = matched.skip(start).take(size).toList();

  // 从完整列表算，不看筛选/分页——真后端的 counts 也是整单的
  final localOk = all.where((s) => s['local'] == true).length;
  final onIpodCount = all.where((s) => s['on_ipod'] == true).length;
  final both = all
      .where((s) => s['local'] == true && s['on_ipod'] == true)
      .length;

  return <String, dynamic>{
    'playlist': '通勤歌单',
    'loading': false,
    'page': page,
    'size': size,
    'status': status,
    'total': total,
    'filtered': matched.length,
    'pages': matched.isEmpty ? 1 : ((matched.length + size - 1) ~/ size),
    // ★ 计数**从曲目列表算出来**，不是另抄一份参数。
    //   另抄一份的话，夹具自己就能跟 songs 打架（改了一处忘另一处），
    //   测出来的东西跟真后端不一定是一回事。
    'counts': <String, dynamic>{
      'local_ok': localOk,
      'local_missing': total - localOk,
      'on_ipod': deviceUnknown ? 0 : onIpodCount,
      'off_ipod': deviceUnknown ? 0 : (total - onIpodCount),
      'device_unknown': deviceUnknown,
      'both': both,
      'on_ipod_but_no_local': onIpodCount - both,
      'local_but_not_on_ipod': localOk - both,
      // 旧键名保留，语义跟真后端一致
      'downloaded': localOk,
      'pending': total - localOk,
      'all': total,
    },
    'songs': window,
  };
}

Map<String, dynamic> playlistsPayload() => <String, dynamic>{
  'cached': true,
  'loading': false,
  'playlists': <Map<String, dynamic>>[
    {
      'id': 100,
      'name': '通勤歌单',
      'track_count': 5,
      'creator': '我',
      'subscribed': false,
      'liked': false,
    },
  ],
};

/// 记录每一次请求，方便断言"发了什么"。
class Recorder {
  final List<({String path, Map<String, dynamic> body})> calls =
      <({String path, Map<String, dynamic> body})>[];

  List<String> get paths => calls.map((c) => c.path).toList();

  Map<String, dynamic>? bodyFor(String fragment) => calls
      .where((c) => c.path.contains(fragment))
      .map((c) => c.body)
      .lastOrNull;
}

ApiClient fakeApi(
  Recorder rec, {
  int total = 5,
  int onIpod = 1,
  int downloaded = 1,

  /// 其中"在 iPod 上、但**本地没留**"的有几首。
  ///
  /// 这种行**是可勾的**——用户点「下载到本地」时它有事可做。
  /// 以前它被并进"已同步"里直接置灰，用户想重下都选不中。
  int onIpodNoLocal = 0,
  bool deviceUnknown = false,
  int pageSize = 50,
  List<int>? pendingIds,
}) {
  return ApiClient(
    baseUrl: 'http://test',
    httpClient: MockClient((request) async {
      final uri = request.url;
      final body = request.body.isEmpty
          ? <String, dynamic>{}
          : jsonDecode(request.body) as Map<String, dynamic>;
      rec.calls.add((path: '${request.method} ${uri.path}', body: body));

      dynamic data;
      if (uri.path == '/api/playlists') {
        data = playlistsPayload();
      } else if (uri.path.endsWith('/songs')) {
        data = songsPayload(
          total: total,
          onIpod: onIpod,
          downloaded: downloaded,
          onIpodNoLocal: onIpodNoLocal,
          deviceUnknown: deviceUnknown,
          page: int.tryParse(uri.queryParameters['page'] ?? '1') ?? 1,
          size: int.tryParse(uri.queryParameters['size'] ?? '50') ?? 50,
          status: uri.queryParameters['status'] ?? 'all',
          search: uri.queryParameters['search'] ?? '',
        );
      } else if (uri.path == '/api/cache/remove') {
        // 删本地那份。真正的删除在后端作业里做，这里只回执。
        data = <String, dynamic>{
          'ok': true,
          'job_id': 'job-remove-local',
          'message': '已加入队列：删除本地 ${body['song_ids']?.length ?? 0} 首',
        };
      } else if (uri.path.endsWith('/ids')) {
        // 后端返回**整单**的 ID，不是当前页
        final ids =
            pendingIds ??
            <int>[for (var i = onIpod + downloaded; i < total; i++) 1000 + i];
        data = <String, dynamic>{
          'playlist': '通勤歌单',
          'status': uri.queryParameters['status'] ?? 'all',
          'ids': ids,
          'count': ids.length,
        };
      } else if (uri.path.endsWith('/plan')) {
        // 预览的数字要跟请求里带的 song_ids 一致——不然就是假预览
        final selected = body['song_ids'];
        final count = selected is List
            ? selected.length
            : (body['push'] == true ? total : total);
        data = <String, dynamic>{
          'loading': false,
          'playlist': '通勤歌单',
          'total': count,
          'to_download': count,
          'needs_fetch': count,
          'already_ready': 0,
          'unavailable': 0,
          'estimated_mb': count * 7.0,
          'preview': <Map<String, String>>[
            for (var i = 0; i < count && i < 10; i++)
              {'name': '歌曲$i', 'artist': '歌手$i'},
          ],
        };
      } else if (uri.path.endsWith('/download')) {
        data = <String, dynamic>{'ok': true, 'job_id': 'job-1'};
      } else {
        data = <String, dynamic>{};
      }

      return http.Response(
        jsonEncode(data),
        200,
        headers: <String, String>{
          'content-type': 'application/json; charset=utf-8',
        },
      );
    }),
  );
}

/// 点操作条上的按钮。
///
/// 操作条在窄窗口下是**横向可滚动**的（主操作固定最右、次要控件能滑），
/// 所以直接 tap 一个被挤出可视区的按钮会点空——测试里得先滚过去。
Future<void> tapInActionBar(WidgetTester tester, String label) async {
  final finder = find.text(label);
  await tester.ensureVisible(finder);
  await tester.pumpAndSettle();
  await tester.tap(finder);
  await tester.pumpAndSettle();
}

/// 把测试视口放大到接近真实窗口。
///
/// 默认的 800×600 太窄：歌单页是"左列表 300 + 右详情"的布局，
/// 右边再放一条带三个按钮的操作条，800 宽下次要按钮会被裁进横向
/// 滚动区里，`tap` 会点空。真实窗口是 1280 逻辑宽，这里按真实尺寸来。
/// （窄屏的可滚性由 library_selection_test 覆盖——那边主操作是固定最右的。）
void useRealisticWindow(WidgetTester tester) {
  tester.view.physicalSize = const Size(1600, 1100);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
}

/// 右键点一下（打开右键菜单）。
///
/// `ContextMenuRegion` 靠 `onSecondaryTapDown` 触发，所以要带**次要键**的
/// 点击；普通 `tap` 是主键，不会弹菜单。
Future<void> rightClick(WidgetTester tester, Finder finder) async {
  await tester.tap(finder, buttons: kSecondaryMouseButton);
  await tester.pumpAndSettle();
}

Future<void> pumpPlaylists(WidgetTester tester, ApiClient api) async {
  useRealisticWindow(tester);
  await tester.pumpWidget(
    ChangeNotifierProvider<AppState>(
      create: (_) => AppState(api: api),
      child: MaterialApp(
        theme: buildTheme(),
        home: const Scaffold(body: PlaylistPage()),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  group('勾选', () {
    testWidgets('点行可以勾上，再点取消', (tester) async {
      await pumpPlaylists(tester, fakeApi(Recorder()));

      expect(find.textContaining('已选'), findsNothing, reason: '还没勾呢，操作条不该出现');

      await tester.tap(find.text('歌曲1'));
      await tester.pumpAndSettle();
      expect(find.text('已选 1 首'), findsOneWidget);

      await tester.tap(find.text('歌曲1'));
      await tester.pumpAndSettle();
      expect(find.textContaining('已选'), findsNothing);
    });

    testWidgets('★ 已经在 iPod 上的勾不动', (tester) async {
      // 歌曲0 是 on_ipod
      await pumpPlaylists(tester, fakeApi(Recorder()));

      await tester.tap(find.text('歌曲0'));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('已选'),
        findsNothing,
        reason: '已同步的歌勾了也没事可做，不该进选择集',
      );
    });

    testWidgets('多首可以累加', (tester) async {
      await pumpPlaylists(tester, fakeApi(Recorder()));

      await tester.tap(find.text('歌曲1'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('歌曲2'));
      await tester.pumpAndSettle();

      expect(find.text('已选 2 首'), findsOneWidget);
    });

    testWidgets('Esc 清空选择', (tester) async {
      await pumpPlaylists(tester, fakeApi(Recorder()));

      await tester.tap(find.text('歌曲1'));
      await tester.pumpAndSettle();
      expect(find.text('已选 1 首'), findsOneWidget);

      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await tester.pumpAndSettle();

      expect(find.textContaining('已选'), findsNothing);
    });

    testWidgets('全选本页只勾能勾的（不含已同步的）', (tester) async {
      await pumpPlaylists(tester, fakeApi(Recorder(), total: 5, onIpod: 1));

      await tester.tap(find.text('歌曲1'));
      await tester.pumpAndSettle();
      // 本页 5 首里有 1 首已同步、1 首已下载、3 首未下载；
      // 能勾的是后 4 首
      await tapInActionBar(tester, '全选本页（4 首）');

      expect(find.text('已选 4 首'), findsOneWidget);
    });
  });

  group('状态筛选', () {
    testWidgets('切筛选会重新请求，并且**不清空**已勾的', (tester) async {
      final rec = Recorder();
      await pumpPlaylists(tester, fakeApi(rec, total: 6, onIpod: 1));

      await tester.tap(find.text('歌曲1'));
      await tester.pumpAndSettle();
      expect(find.text('已选 1 首'), findsOneWidget);

      // 切到"未下载"
      await tester.tap(find.byType(DropdownButton<SongFilter>).first);
      await tester.pumpAndSettle();
      await tester.tap(find.text('本地未下载').last);
      await tester.pumpAndSettle();

      final songsCall = rec.calls.where((c) => c.path.endsWith('/songs')).last;
      expect(songsCall.path, contains('GET'));

      // 勾选跨筛选保持
      expect(find.text('已选 1 首'), findsOneWidget, reason: '切筛选把勾好的清掉了');
    });

    testWidgets('筛选后空结果给的是筛选提示，不是"歌单是空的"', (tester) async {
      await pumpPlaylists(
        tester,
        fakeApi(Recorder(), total: 2, onIpod: 2, downloaded: 0),
      );

      await tester.tap(find.byType(DropdownButton<SongFilter>).first);
      await tester.pumpAndSettle();
      await tester.tap(find.text('本地未下载').last);
      await tester.pumpAndSettle();

      expect(
        find.textContaining('下没有曲目'),
        findsOneWidget,
        reason: '空结果该说"筛选没命中"，不能让人以为整个歌单是空的',
      );
    });
  });

  group('★ 一键选中所有未下载的', () {
    testWidgets('按钮上的数量是**整单**的未下载数，不是当前页', (tester) async {
      // 100 首、页大小 50：当前页只有 50 首，但未下载的按钮要写 97 首
      await pumpPlaylists(
        tester,
        fakeApi(Recorder(), total: 100, onIpod: 2, downloaded: 1, pageSize: 50),
      );

      await tester.tap(find.text('歌曲3')); // 先勾一首让操作条出现
      await tester.pumpAndSettle();

      expect(find.text('一键选中所有未下载的（97 首）'), findsOneWidget);
    });

    testWidgets('点了之后选中**整单**的未下载曲目，跨页都算上', (tester) async {
      final rec = Recorder();
      await pumpPlaylists(
        tester,
        fakeApi(rec, total: 100, onIpod: 2, downloaded: 1, pageSize: 50),
      );

      // 点一首"未下载"的（歌曲3 起才是未下载）——它本来就包含在那 97 首里，
      // 这样断言的就是"一键选中确实覆盖了整单"，不会掺进别的东西
      await tester.tap(find.text('歌曲3'));
      await tester.pumpAndSettle();
      await tapInActionBar(tester, '一键选中所有未下载的（97 首）');

      // 走了 /ids 接口，而且拿的是 pending
      final idsCall = rec.calls
          .where((c) => c.path.contains('/ids'))
          .lastOrNull;
      expect(idsCall, isNotNull, reason: '应该走后端 /ids 接口');

      // 97 = 100 - 2(已iPod) - 1(已下载)
      expect(find.text('已选 97 首'), findsOneWidget, reason: '只选中了当前页的话这里不会到 97');
    });
  });

  group('★ 下发的范围', () {
    testWidgets('选中的几首 → 请求体里必须带这些 song_ids', (tester) async {
      final rec = Recorder();
      await pumpPlaylists(tester, fakeApi(rec));

      await tester.tap(find.text('歌曲1'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('歌曲2'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('下载选中的 2 首'));
      await tester.pumpAndSettle();

      // 预览对话框：数字要跟选中的两首一致
      expect(find.textContaining('选中的 2 首'), findsWidgets);
      expect(find.textContaining('需要下载'), findsOneWidget);

      await tester.tap(find.textContaining('开始下载'));
      await tester.pumpAndSettle();

      final body = rec.bodyFor('/download');
      expect(body, isNotNull, reason: '没发下载请求');
      expect(body!['song_ids'], <int>[
        1001,
        1002,
      ], reason: '下单范围跟勾选不一致——这会下错歌或者下整个歌单');
      expect(body['push'], false);
    });

    testWidgets('「同步整个歌单」→ song_ids 必须是 null（不是空列表）', (tester) async {
      final rec = Recorder();
      await pumpPlaylists(tester, fakeApi(rec));

      await tester.tap(find.text('同步整个歌单'));
      await tester.pumpAndSettle();
      await tester.tap(find.textContaining('开始同步'));
      await tester.pumpAndSettle();

      final body = rec.bodyFor('/download');
      expect(body, isNotNull);
      expect(body!['song_ids'], isNull, reason: '空列表在后端是"一首都没选"会报错，整单必须传 null');
      expect(body['push'], true);
    });

    testWidgets('★ 预览也是按选中的算，不是整个歌单', (tester) async {
      final rec = Recorder();
      await pumpPlaylists(tester, fakeApi(rec));

      await tester.tap(find.text('歌曲1'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('下载选中的 1 首'));
      await tester.pumpAndSettle();

      final planBody = rec.bodyFor('/plan');
      expect(planBody, isNotNull);
      expect(planBody!['song_ids'], <int>[
        1001,
      ], reason: '预览不带选中集的话，"要下 5 首"就是假的');

      await tester.tap(find.text('取消'));
      await tester.pumpAndSettle();
    });

    testWidgets('取消预览就什么都不发', (tester) async {
      final rec = Recorder();
      await pumpPlaylists(tester, fakeApi(rec));

      await tester.tap(find.text('歌曲1'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('下载选中的 1 首'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('取消'));
      await tester.pumpAndSettle();

      expect(
        rec.paths.where((p) => p.contains('/download')),
        isEmpty,
        reason: '取消了却还是提交了下载',
      );
    });

    testWidgets('后端拒绝时错误原样显示', (tester) async {
      final api = ApiClient(
        baseUrl: 'http://test',
        httpClient: MockClient((request) async {
          if (request.url.path == '/api/playlists') {
            return http.Response(
              jsonEncode(playlistsPayload()),
              200,
              headers: <String, String>{
                'content-type': 'application/json; charset=utf-8',
              },
            );
          }
          if (request.url.path.endsWith('/songs')) {
            return http.Response(
              jsonEncode(songsPayload()),
              200,
              headers: <String, String>{
                'content-type': 'application/json; charset=utf-8',
              },
            );
          }
          return http.Response(
            jsonEncode(<String, String>{
              'detail': '没有选中任何歌曲。要处理整个歌单请用「同步整个歌单」。',
            }),
            400,
            headers: <String, String>{
              'content-type': 'application/json; charset=utf-8',
            },
          );
        }),
      );
      await pumpPlaylists(tester, api);

      await tester.tap(find.text('歌曲1'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('下载选中的 1 首'));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('没有选中任何歌曲'),
        findsOneWidget,
        reason: '后端的中文原因被吞掉了',
      );
    });
  });

  group('★ 在 iPod 上但本地没留（用户报的那个 bug）', () {
    testWidgets('这种行**可以勾**，而且行上写清两个维度', (tester) async {
      // 用户原话："我把本地的歌删了重新下载，它提示已准备就绪。"
      // 以前这种行被并进"已就绪"直接置灰，连勾都勾不了。
      final rec = Recorder();
      await pumpPlaylists(
        tester,
        fakeApi(rec, total: 3, onIpod: 2, downloaded: 0, onIpodNoLocal: 2),
      );

      // ★ 现在两个维度各一个徽章，不再合成一个词：
    //   "本地没有" + "iPod 上已有" —— 只写"已同步"会让人以为不用管
    expect(find.text('本地没有'), findsWidgets);
    expect(find.text('iPod 上已有'), findsWidgets);

      await tester.tap(find.text('歌曲0'));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('已选 1 首'),
        findsOneWidget,
        reason: '在 iPod 上但本地没留 = 有事可做，必须能勾',
      );
    });

    testWidgets('两边都有的才不给勾（那才是真没事可做）', (tester) async {
      final rec = Recorder();
      await pumpPlaylists(
        tester,
        fakeApi(rec, total: 3, onIpod: 1, downloaded: 0),
      );

      expect(find.text('iPod 上已有'), findsWidgets);
      await tester.tap(find.text('歌曲0'));
      await tester.pumpAndSettle();

      expect(find.textContaining('已选'), findsNothing);
    });

    testWidgets('★ 下发时这些歌要真的带上（不能被静默丢掉）', (tester) async {
      final rec = Recorder();
      await pumpPlaylists(
        tester,
        fakeApi(rec, total: 3, onIpod: 2, downloaded: 0, onIpodNoLocal: 2),
      );

      await tester.tap(find.text('歌曲0'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('歌曲1'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('下载选中的 2 首'));
      await tester.pumpAndSettle();
      await tester.tap(find.textContaining('开始下载'));
      await tester.pumpAndSettle();

      final body = rec.bodyFor('/download');
      expect(body, isNotNull, reason: '没发下载请求');
      expect(body!['song_ids'], <int>[1000, 1001]);
      expect(body['push'], false);
    });
  });

  group('★ 搜索（服务端筛，不是本地筛当前页）', () {
    testWidgets('搜索词发给后端', (tester) async {
      // 本地筛只能筛当前页的 50 首，246 首的歌单"搜了跟没搜一样"。
      final rec = Recorder();
      await pumpPlaylists(tester, fakeApi(rec));

      await tester.enterText(find.byType(TextField), '歌曲3');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      final path = rec.calls.last.path;
      expect(path, contains('/songs'));
      expect(
        rec.calls.last.body,
        isEmpty,
        reason: '搜索走 GET，参数在 query 里不是 body',
      );
      // 结果被筛过：只剩匹配的那一首。
      // 注意搜索框里也写着"歌曲3"，所以不能断言 findsOneWidget——
      // 匹配的那首会出现两次（输入框 + 曲目行）。
      expect(find.text('歌曲0'), findsNothing);
      expect(find.text('歌曲4'), findsNothing);
      expect(find.text('歌曲3'), findsNWidgets(2));
    });

    testWidgets('清空搜索框后回到全部', (tester) async {
      final rec = Recorder();
      await pumpPlaylists(tester, fakeApi(rec));

      await tester.enterText(find.byType(TextField), '歌曲3');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(find.text('歌曲4'), findsNothing);

      await tester.enterText(find.byType(TextField), '');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(find.text('歌曲4'), findsOneWidget);
    });
  });

  group('★ 删除本地那份（从本地下载管理器搬过来的）', () {
    testWidgets('右键菜单里有这一项（本地有文件的行可用）', (tester) async {
      final rec = Recorder();
      // 3 首：第 0 首已同步且本地有、第 1 首已下载、第 2 首未下载
      await pumpPlaylists(
        tester,
        fakeApi(rec, total: 3, onIpod: 1, downloaded: 1),
      );

      await rightClick(tester, find.text('歌曲1'));

      expect(find.text('删除本地那份'), findsOneWidget);
      expect(find.text('本地没有这份文件'), findsNothing, reason: '这一首本地有文件');
    });

    testWidgets('本地没文件的行置灰，并写明原因（不隐藏）', (tester) async {
      final rec = Recorder();
      await pumpPlaylists(
        tester,
        fakeApi(rec, total: 3, onIpod: 1, downloaded: 1),
      );

      // 第 2 首：未下载 → 本地没文件
      await rightClick(tester, find.text('歌曲2'));

      expect(
        find.text('本地没有这份文件'),
        findsOneWidget,
        reason: '不可用的项要说明为什么，别默默藏掉',
      );
    });

    testWidgets('★ 必须确认，且确认前不发请求', (tester) async {
      // 删本地文件是破坏性的（想再听要重新下载、又碰网易云接口），
      // 所以跟别的破坏性操作一样：无确认不得执行。
      final rec = Recorder();
      await pumpPlaylists(
        tester,
        fakeApi(rec, total: 3, onIpod: 1, downloaded: 1),
      );

      await tester.tap(find.text('歌曲1'));
      await tester.pump();
      await tester.tap(find.text('删除本地那份'));
      await tester.pumpAndSettle();

      expect(rec.bodyFor('/api/cache/remove'), isNull, reason: '还没确认就发了请求');
      expect(
        find.textContaining('不受影响'),
        findsOneWidget,
        reason: '必须写明只删电脑上那份，不然用户不敢清',
      );

      await tester.tap(find.textContaining('删除 1 首').last);
      await tester.pumpAndSettle();

      final body = rec.bodyFor('/api/cache/remove');
      expect(body, isNotNull, reason: '确认后没发请求');
      expect(body!['song_ids'], <int>[1001]);
    });

    testWidgets('取消就什么都不做', (tester) async {
      final rec = Recorder();
      await pumpPlaylists(
        tester,
        fakeApi(rec, total: 3, onIpod: 1, downloaded: 1),
      );

      await tester.tap(find.text('歌曲1'));
      await tester.pump();
      await tester.tap(find.text('删除本地那份'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('取消'));
      await tester.pumpAndSettle();

      expect(rec.bodyFor('/api/cache/remove'), isNull);
    });
  });

  group('★ 两个维度分开显示（本地 / 设备）', () {
    testWidgets('没插设备时说「未插设备」，不假装设备上没有', (tester) async {
      await pumpPlaylists(
        tester,
        fakeApi(Recorder(), onIpod: 1, downloaded: 1, deviceUnknown: true),
      );

      // ★ 以前后端没插设备时返回空集，界面把同步过的歌全标成"未同步"，
      //   用户以为白同步了。现在明说判断不了。
      expect(
        find.textContaining('未插设备'),
        findsWidgets,
        reason: '判断不了就说不知道——假装"设备上没有"是假话',
      );
      expect(find.text('iPod 上已有'), findsNothing);
      expect(find.text('iPod 上没有'), findsNothing);
    });

    testWidgets('设备上有、本地没留的行：两个徽章各说各的', (tester) async {
      await pumpPlaylists(
        tester,
        fakeApi(
          Recorder(),
          total: 3,
          onIpod: 1,
          downloaded: 0,
          onIpodNoLocal: 1,
        ),
      );

      // 歌曲0 在 iPod 上、本地没留 → "本地没有" + "iPod 上已有" 同时出现。
      // 合成一个词的话，这件事就被藏起来了——而那正是"该重新下载"的那批。
      expect(find.text('本地没有'), findsWidgets);
      expect(find.text('iPod 上已有'), findsWidgets);
    });

    testWidgets('汇总行把两维分开报，且不把"判断不了"算成"缺"', (tester) async {
      await pumpPlaylists(
        tester,
        fakeApi(Recorder(), total: 5, onIpod: 2, downloaded: 1, deviceUnknown: true),
      );

      expect(find.textContaining('本地：已下'), findsOneWidget);
      expect(
        find.textContaining('未插设备（状态未知）'),
        findsOneWidget,
        reason: '不能报成"iPod：已有 0 / 缺 5"——那是我们不知道的事',
      );
    });
  });
}
