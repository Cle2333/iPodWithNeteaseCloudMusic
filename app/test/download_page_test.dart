/// 下载页：正在下什么、下过哪些歌、哪几首失败了。
///
/// 这一页**不显示作业队列、不显示日志**——那是设置页调试面板的事。
/// 测试也顺带把这个约定钉住：作业类型不是"下载歌"的，这里不显示。
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:ipod_manager/api/client.dart';
import 'package:ipod_manager/pages/download_page.dart';
import 'package:ipod_manager/state/app_state.dart';
import 'package:ipod_manager/theme.dart';

Map<String, dynamic> item(
  int index,
  String name,
  String status, {
  String artist = '歌手',
  int size = 0,
  String reason = '',
}) => <String, dynamic>{
  'index': index,
  'name': name,
  'artist': artist,
  'status': status,
  'size': size,
  'reason': reason,
};

Map<String, dynamic> job({
  String id = 'j1',
  String kind = 'download',
  String title = '下载「通勤歌单」',
  String state = 'done',
  String stateText = '已完成',
  int total = 0,
  int done = 0,
  String current = '',
  String stage = '',
  double percent = 0,
  String error = '',
  int elapsed = 0,
  int? eta,
  List<Map<String, dynamic>> items = const <Map<String, dynamic>>[],
}) {
  final ok = items.where((i) => i['status'] == 'done').length;
  final bad = items.where((i) => i['status'] == 'failed').length;
  return <String, dynamic>{
    'id': id,
    'kind': kind,
    'title': title,
    'state': state,
    'state_text': stateText,
    'total': total,
    'done': done,
    'current': current,
    'stage': stage,
    'percent': percent,
    'error': error,
    'result': <String, dynamic>{},
    'elapsed': elapsed,
    'speed': 0,
    'eta_seconds': eta,
    'items': items,
    'items_done': ok,
    'items_failed': bad,
  };
}

/// 一个跑完的下载作业：2 首成功、1 首失败（失败要带原因）。
List<Map<String, dynamic>> get finishedJobs => <Map<String, dynamic>>[
  job(
    total: 3,
    done: 3,
    percent: 100,
    elapsed: 95,
    items: <Map<String, dynamic>>[
      item(1, '清明上河图', 'done', artist: '赧然', size: 7500000),
      item(2, '夏天的风', 'done', artist: '周政', size: 7200000),
      item(3, '十年', 'failed', artist: '陈奕迅', reason: '拿不到下载链接（多半无版权）'),
    ],
  ),
];

/// 造一条"本地已下载"的记录。
Map<String, dynamic> local({
  required int songId,
  required String name,
  String artist = '歌手',
  int size = 7000000,
  bool onIpod = false,
  bool missing = false,
  String level = 'exhigh',
  String source = 'unknown',
  String sourceName = '未记录来源',
}) => <String, dynamic>{
  'song_id': songId,
  'name': name,
  'artist': artist,
  'level': level,
  'size': size,
  'size_text': '${(size / 1048576).toStringAsFixed(1)} MB',
  'created_at': '2026-09-20T09:00:00',
  'on_ipod': onIpod,
  'missing': missing,
  'path': 'C:/cache/$songId.mp3',
};

/// 造一个来源分组。
///
/// 不能叫 `group`——那是 flutter_test 的函数，重名会把它盖掉。
Map<String, dynamic> sourceGroup({
  required String source,
  required String name,
  required int count,
  String sizeText = '7.0 MB',
  String kind = 'playlist',
  int playlistId = 0,
}) => <String, dynamic>{
  'source': source,
  'name': name,
  'kind': kind,
  'playlist_id': playlistId,
  'count': count,
  'bytes': count * 7000000,
  'size_text': sizeText,
};

ApiClient fakeApi({
  List<Map<String, dynamic>>? jobs,
  List<Map<String, dynamic>>? locals,
  List<Map<String, dynamic>>? groups,
  List<int>? allLocalIds,
  int queued = 0,
  Map<String, dynamic>? exportResult,
  List<({String path, Map<String, dynamic> body})>? calls,
}) {
  return ApiClient(
    baseUrl: 'http://test',
    httpClient: MockClient((request) async {
      final body = request.body.isEmpty
          ? <String, dynamic>{}
          : jsonDecode(request.body) as Map<String, dynamic>;
      // 记完整 URL（含 query）——只看 path 的话断言不了参数，
      // 而"请求有没有带上 source/song_ids"正是要守的东西
      calls?.add((
        path:
            '${request.method} '
            '${request.url.path}'
            '${request.url.query.isEmpty ? '' : '?${request.url.query}'}',
        body: body,
      ));

      dynamic data;
      if (request.url.path == '/api/downloads') {
        final rows = locals ?? <Map<String, dynamic>>[];
        // total 是**筛选结果**的总数，不是当前页的行数。
        // 传了 allLocalIds 就说明"筛选出这么多、这一页只有几首"——
        // 真实后端就是这个形状（分页 200 一页）。
        final filtered = allLocalIds?.length ?? rows.length;
        data = <String, dynamic>{
          'total': filtered,
          'page': 1,
          'size': 200,
          'pages': 1,
          'sort': 'recent',
          'sorts': <Map<String, String>>[
            {'value': 'recent', 'label': '最近下载'},
            {'value': 'name', 'label': '曲名'},
          ],
          'search': '',
          'bytes': rows.fold<int>(0, (s, r) => s + (r['size'] as int)),
          'size_text': '14.0 MB',
          'on_ipod': rows.where((r) => r['on_ipod'] == true).length,
          'only_local':
              filtered - rows.where((r) => r['on_ipod'] == true).length,
          'source': request.url.queryParameters['source'] ?? '',
          'groups': groups ?? <Map<String, dynamic>>[],
          'all_count': filtered,
          'all_size_text': '14.0 MB',
          'songs': rows,
        };
      } else if (request.url.path == '/api/downloads/ids') {
        data = <String, dynamic>{
          'ids':
              allLocalIds ??
              (locals ?? <Map<String, dynamic>>[])
                  .map((r) => r['song_id'])
                  .toList(),
          'count': 0,
          'search': '',
          'sort': 'recent',
        };
      } else if (request.url.path == '/api/downloads/remove') {
        data = <String, dynamic>{'ok': true, 'job_id': 'job-r1'};
      } else if (request.url.path == '/api/jobs') {
        final list = jobs ?? <Map<String, dynamic>>[];
        final running = list.where((j) => j['state'] == 'running').toList();
        data = <String, dynamic>{
          'running': running.isEmpty ? null : running.first,
          'queued': queued,
          'has_work': running.isNotEmpty || queued > 0,
          'jobs': list,
        };
      } else if (request.url.path == '/api/debug/export') {
        data =
            exportResult ??
            <String, dynamic>{
              'ok': true,
              'path': r'C:\Temp\iPod管理器诊断-20260920-091500.txt',
              'bytes': 20480,
              'lines': 320,
              'jobs': 2,
            };
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

/// 返回 AppState，测试里可以拿它 ping 刷新信号来模拟"作业干完了"。
Future<AppState> pumpDownload(WidgetTester tester, ApiClient api) async {
  tester.view.physicalSize = const Size(1400, 1000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  final state = AppState(api: api);
  addTearDown(state.dispose);

  await tester.pumpWidget(
    ChangeNotifierProvider<AppState>.value(
      value: state,
      child: MaterialApp(
        theme: buildTheme(),
        home: const Scaffold(body: DownloadPage()),
      ),
    ),
  );

  // 真实应用是在 main() 里 startPolling() 把数据拉起来的。测试里直接
  // 拉一次就好——起定时器的话测试结束时会留下未触发的 Timer。
  await state.refreshStatus();

  // 这里用 pump 而不是 pumpAndSettle：作业正在跑的时候页面上有个转圈，
  // 那是无限动画，pumpAndSettle 永远等不到"稳定"（真实 UI 上它本来就
  // 该一直转，不是 bug）。
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 50));
  return state;
}

void main() {
  group('空闲', () {
    testWidgets('没下过东西给引导，而不是空白', (tester) async {
      await pumpDownload(tester, fakeApi());
      expect(find.textContaining('还没有跑过下载或同步'), findsOneWidget);
      expect(find.textContaining('歌单'), findsWidgets);
    });

    testWidgets('★ 非下载类作业不显示（备份/健康检查不该混进来）', (tester) async {
      await pumpDownload(
        tester,
        fakeApi(
          jobs: <Map<String, dynamic>>[
            job(
              kind: 'verify',
              title: '健康检查',
              items: <Map<String, dynamic>>[item(1, '随便', 'done')],
            ),
            job(kind: 'doctor', title: '环境自检'),
          ],
        ),
      );
      expect(
        find.textContaining('还没有跑过下载或同步'),
        findsOneWidget,
        reason: '健康检查/环境自检混进下载页了',
      );
      expect(find.text('随便'), findsNothing);
    });
  });

  group('下完了', () {
    testWidgets('列出每一首，成功的显示大小', (tester) async {
      await pumpDownload(tester, fakeApi(jobs: finishedJobs));

      expect(find.text('清明上河图'), findsOneWidget);
      expect(find.text('夏天的风'), findsOneWidget);
      expect(find.text('7.2 MB'), findsOneWidget);
    });

    testWidgets('★ 失败的那首必须连原因一起显示', (tester) async {
      await pumpDownload(tester, fakeApi(jobs: finishedJobs));

      expect(find.text('十年'), findsOneWidget);
      expect(
        find.textContaining('拿不到下载链接'),
        findsOneWidget,
        reason: '"有 1 首失败"这种话对排查毫无帮助，得说清为什么',
      );
    });

    testWidgets('顶部汇总写清成功/失败数量', (tester) async {
      await pumpDownload(tester, fakeApi(jobs: finishedJobs));

      // 汇总在"本次歌单"的副标题上：共 N 首 · 成功 X · 失败 Y
      expect(find.textContaining('共 3 首 · 成功 2 · 失败 1'), findsOneWidget);
    });

    testWidgets('★ 页面上不该有导出按钮（它在设置里）', (tester) async {
      await pumpDownload(tester, fakeApi(jobs: finishedJobs));
      expect(find.text('导出记录'), findsNothing);
      expect(find.text('导出诊断记录'), findsNothing);
      expect(
        find.textContaining('设置'),
        findsWidgets,
        reason: '不在这页放按钮，但得告诉用户去哪儿找',
      );
    });

    testWidgets('跑完了就不该有取消按钮', (tester) async {
      await pumpDownload(tester, fakeApi(jobs: finishedJobs));
      expect(find.text('取消下载'), findsNothing);
    });

    testWidgets('★ 不显示作业队列，也不显示日志区', (tester) async {
      await pumpDownload(
        tester,
        fakeApi(
          jobs: <Map<String, dynamic>>[
            ...finishedJobs,
            job(id: 'old', title: '下载「老歌单」', state: 'done'),
          ],
        ),
      );
      // 老作业的标题不该出现在页面上——那是队列视图干的事
      expect(find.text('下载「老歌单」'), findsNothing);
      // 日志那套控件也不该在这
      expect(find.byType(TextField), findsNothing);
    });
  });

  group('正在下', () {
    testWidgets('显示进度、正在下哪首、取消按钮', (tester) async {
      await pumpDownload(
        tester,
        fakeApi(
          jobs: <Map<String, dynamic>>[
            job(
              state: 'running',
              stateText: '进行中',
              total: 246,
              done: 42,
              percent: 17.1,
              current: '清明上河图',
              eta: 240,
              items: <Map<String, dynamic>>[
                item(1, '第一首', 'done', size: 7000000),
                item(2, '清明上河图', 'downloading'),
              ],
            ),
          ],
        ),
      );

      expect(find.text('42 / 246'), findsOneWidget);
      expect(find.text('17%'), findsOneWidget);
      expect(find.textContaining('正在下载：清明上河图'), findsOneWidget);
      expect(find.textContaining('还需约'), findsOneWidget);
      expect(find.text('取消下载'), findsOneWidget);
    });

    testWidgets('等待中的曲目标"等待"', (tester) async {
      await pumpDownload(
        tester,
        fakeApi(
          jobs: <Map<String, dynamic>>[
            job(
              state: 'running',
              total: 3,
              done: 1,
              items: <Map<String, dynamic>>[
                item(1, '已完成', 'done', size: 1000000),
                item(2, '排队中', 'downloading'),
                item(3, '还没轮到', ''),
              ],
            ),
          ],
        ),
      );
      expect(find.text('等待'), findsOneWidget);
    });

    testWidgets('排队时说明"前面还有几个"（但不出列表）', (tester) async {
      await pumpDownload(
        tester,
        fakeApi(
          queued: 2,
          jobs: <Map<String, dynamic>>[job(state: 'queued', stateText: '排队中')],
        ),
      );
      expect(find.textContaining('前面还有 2 个任务在排队'), findsOneWidget);
    });
  });

  // ────────────────────────────────────────────────────────────────────
  // 阶段 + 进度条
  //
  // ★ 这一组守的是用户那条投诉：同步 147 首时"以为被卡住了"。
  //   写入 iPod 那一段要转码 + 拷 3.3 GB，实测 6 分钟，而这段以前
  //   一个进度数字都不报 —— 界面上整整 6 分钟什么都不动。
  //   现在的判据：**只要还在干活，界面上就必须有东西在动。**
  // ────────────────────────────────────────────────────────────────────

  group('★ 阶段与进度条（"以为被卡住了"那条投诉）', () {
    testWidgets('正在跑的时候显示阶段名', (tester) async {
      await pumpDownload(
        tester,
        fakeApi(
          jobs: <Map<String, dynamic>>[
            job(
              kind: 'sync',
              title: '同步「喜欢的音乐」',
              state: 'running',
              stateText: '进行中',
              stage: '写入 iPod',
              total: 147,
              done: 60,
              percent: 40.8,
              current: '阴天',
            ),
          ],
        ),
      );

      expect(
        find.text('写入 iPod'),
        findsWidgets,
        reason: '不知道在干什么，用户就只能猜自己是不是卡住了',
      );
    });

    testWidgets('★ 写进 iPod 的那一段：进度条要跟着首歌走', (tester) async {
      await pumpDownload(
        tester,
        fakeApi(
          jobs: <Map<String, dynamic>>[
            job(
              kind: 'sync',
              state: 'running',
              stage: '写入 iPod',
              total: 147,
              done: 60,
              percent: 40.8,
              current: '阴天',
            ),
          ],
        ),
      );

      expect(find.text('60 / 147'), findsOneWidget);
      expect(find.text('41%'), findsOneWidget);

      final bar = tester.widget<LinearProgressIndicator>(
        find.byType(LinearProgressIndicator),
      );
      expect(
        bar.value,
        closeTo(0.408, 0.001),
        reason: '有总数就该画确定进度条，并且值要跟着走',
      );
    });

    testWidgets('★★ 切不出等份的阶段：画滚动的条，不画停着不动的空条', (tester) async {
      // 刷盘 / 整库重写这种阶段没有"几分之几"，后端给的 total 是 0。
      // 以前界面会画一根停在 0% 的条 —— 那看起来和死了没区别，
      // 而这恰恰是最耗时的几步之一。
      await pumpDownload(
        tester,
        fakeApi(
          jobs: <Map<String, dynamic>>[
            job(
              kind: 'sync',
              state: 'running',
              stage: '把 3300 MB 刷到设备（别拔线）',
              total: 0,
              done: 0,
              current: '',
            ),
          ],
        ),
      );

      expect(find.textContaining('把 3300 MB 刷到设备'), findsWidgets);

      final bar = tester.widget<LinearProgressIndicator>(
        find.byType(LinearProgressIndicator),
      );
      expect(
        bar.value,
        isNull,
        reason: 'value 为 null 才是"不确定进度条"（一直在滚）；'
            '给个 0 就变成一根死条，用户看到的还是卡住',
      );
      expect(
        find.textContaining('它在滚就说明没卡住'),
        findsOneWidget,
        reason: '得跟用户说清这根条为什么没有百分比',
      );
    });

    testWidgets('跑的时候要显示已用时间', (tester) async {
      await pumpDownload(
        tester,
        fakeApi(
          jobs: <Map<String, dynamic>>[
            job(
              kind: 'sync',
              state: 'running',
              stage: '写入 iPod',
              total: 147,
              done: 10,
              percent: 6.8,
              elapsed: 200,
            ),
          ],
        ),
      );
      expect(find.textContaining('已用 3 分 20 秒'), findsOneWidget);
    });

    testWidgets('★ 写入阶段的"正在处理"不该说成"正在下载"', (tester) async {
      await pumpDownload(
        tester,
        fakeApi(
          jobs: <Map<String, dynamic>>[
            job(
              kind: 'sync',
              state: 'running',
              stage: '写入 iPod',
              total: 147,
              done: 5,
              percent: 3.4,
              current: '年少有为',
            ),
          ],
        ),
      );
      expect(find.textContaining('正在处理：年少有为'), findsOneWidget);
      expect(find.textContaining('正在下载：年少有为'), findsNothing);
    });

    testWidgets('没插设备也没在跑：不编一个阶段出来', (tester) async {
      await pumpDownload(tester, fakeApi(jobs: finishedJobs));
      expect(find.text('写入 iPod'), findsNothing);
      expect(find.textContaining('没有进度信息'), findsNothing);
    });

    testWidgets('★ 跑完了但没逐首记录：不能说"还没开始下歌"', (tester) async {
      // 主路径就是这样：歌早就下到本地了，这次同步只做"写入 iPod"，
      // 一首都不下载 → items 为空。以前这里一律显示"还没开始下歌，
      // 稍等一下就有清单了"——对一条**已经跑完**的同步说这句话，
      // 用户会以为它卡住了或者根本没干活。
      await pumpDownload(
        tester,
        fakeApi(
          jobs: <Map<String, dynamic>>[
            job(
              kind: 'sync',
              title: '同步「喜欢的音乐」',
              state: 'done',
              stateText: '已完成',
              stage: '重建数据库并签名',
              elapsed: 96,
            ),
          ],
        ),
      );

      expect(find.textContaining('这次同步没有下载任何歌'), findsOneWidget);
      expect(
        find.textContaining('还没开始下歌'),
        findsNothing,
        reason: '已经跑完的作业不该被说成"还没开始"',
      );
    });

    testWidgets('真还没开始：保留"稍等一下"的说法', (tester) async {
      await pumpDownload(
        tester,
        fakeApi(
          jobs: <Map<String, dynamic>>[
            job(kind: 'sync', state: 'running', stateText: '进行中', stage: '读取歌单'),
          ],
        ),
      );
      expect(find.textContaining('还没开始下歌'), findsOneWidget);
    });
  });
}
