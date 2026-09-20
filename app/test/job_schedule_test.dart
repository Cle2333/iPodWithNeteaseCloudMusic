/// 作业日程（调试面板里那个）。
///
/// 它存在的意义是"出 bug 的时候看一眼、导出给开发"。所以守两件事：
/// 一，日程**真的显示了**（之前调试面板只有日志，没有日程）；
/// 二，展开能看到逐首歌的结果——"有 3 首失败"这句话对排查没用，
/// 得知道是哪 3 首、为什么。
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:ipod_manager/api/client.dart';
import 'package:ipod_manager/api/models.dart';
import 'package:ipod_manager/theme.dart';
import 'package:ipod_manager/widgets/common.dart';
import 'package:ipod_manager/widgets/job_schedule.dart';

Map<String, dynamic> jobJson({
  required String id,
  required String title,
  String kind = 'download',
  String state = 'done',
  int total = 3,
  int done = 3,
  String error = '',
  List<Map<String, dynamic>>? items,
  double elapsed = 12.0,
}) => <String, dynamic>{
  'id': id,
  'kind': kind,
  'title': title,
  'state': state,
  'state_text': const <String, String>{
    'queued': '排队中',
    'running': '进行中',
    'done': '已完成',
    'failed': '失败',
    'cancelled': '已取消',
  }[state],
  'total': total,
  'done': done,
  'current': '',
  'percent': total == 0 ? 0 : done * 100 / total,
  'error': error,
  'result': <String, dynamic>{},
  'elapsed': elapsed,
  'speed': 0.0,
  'eta_seconds': null,
  'items': items ?? <Map<String, dynamic>>[],
  'items_done': (items ?? <Map<String, dynamic>>[])
      .where((i) => i['status'] == 'done')
      .length,
  'items_failed': (items ?? <Map<String, dynamic>>[])
      .where((i) => i['status'] == 'failed')
      .length,
};

Map<String, dynamic> itemJson({
  required int index,
  required String name,
  String artist = '',
  String status = 'done',
  int size = 7000000,
  String reason = '',
}) => <String, dynamic>{
  'index': index,
  'name': name,
  'artist': artist,
  'status': status,
  'size': size,
  'reason': reason,
};

Map<String, dynamic> logJson(int seq, String text) => <String, dynamic>{
  'seq': seq,
  'ts': '11:18:20',
  'level': 'info',
  'text': text,
};

JobsSnapshot snapshotOf(List<Map<String, dynamic>> jobs) =>
    JobsSnapshot.fromJson(<String, dynamic>{
      'running': null,
      'queued': 0,
      'has_work': false,
      'jobs': jobs,
    });

ApiClient fakeApi({Map<String, List<Map<String, dynamic>>>? logs}) {
  return ApiClient(
    baseUrl: 'http://test',
    httpClient: MockClient((request) async {
      final match = RegExp(r'^/api/jobs/(\w+)$').firstMatch(request.url.path);
      dynamic data = <String, dynamic>{'lines': <Map<String, dynamic>>[]};
      if (match != null) {
        data = <String, dynamic>{
          'lines': logs?[match.group(1)] ?? <Map<String, dynamic>>[],
        };
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

Future<void> pump(
  WidgetTester tester,
  JobsSnapshot? snapshot,
  ApiClient api,
) async {
  tester.view.physicalSize = const Size(1200, 900);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  await tester.pumpWidget(
    MaterialApp(
      theme: buildTheme(),
      home: Scaffold(
        body: JobSchedule(api: api, snapshot: snapshot),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  group('作业日程', () {
    testWidgets('★ 没有作业时说清楚，不是空白', (tester) async {
      await pump(tester, snapshotOf(<Map<String, dynamic>>[]), fakeApi());
      expect(find.textContaining('还没有任何作业记录'), findsOneWidget);
    });

    testWidgets('snapshot 为 null 也不炸', (tester) async {
      await pump(tester, null, fakeApi());
      expect(find.byType(Notice), findsOneWidget);
    });

    testWidgets('★ 列出作业，带状态和进度', (tester) async {
      await pump(
        tester,
        snapshotOf(<Map<String, dynamic>>[
          jobJson(id: 'j1', title: '下载「DJ」选中的 2 首', total: 2, done: 2),
          jobJson(
            id: 'j2',
            title: '同步「老歌」到 iPod',
            kind: 'sync',
            state: 'failed',
            error: 'DeviceNotFoundError: 没有找到 iPod',
          ),
        ]),
        fakeApi(),
      );

      expect(find.text('下载「DJ」选中的 2 首'), findsOneWidget);
      expect(find.text('同步「老歌」到 iPod'), findsOneWidget);
      expect(find.textContaining('已完成'), findsOneWidget);
      // 失败原因要直接露出来，不用展开就看得见
      expect(find.textContaining('没有找到 iPod'), findsOneWidget);
    });

    testWidgets('★ 展开才拉日志（50 个作业一次全拉会白等）', (tester) async {
      final calls = <String>[];
      final api = ApiClient(
        baseUrl: 'http://test',
        httpClient: MockClient((request) async {
          calls.add(request.url.path);
          return http.Response(
            jsonEncode(<String, dynamic>{'lines': <Map<String, dynamic>>[]}),
            200,
            headers: <String, String>{
              'content-type': 'application/json; charset=utf-8',
            },
          );
        }),
      );

      await pump(
        tester,
        snapshotOf(<Map<String, dynamic>>[jobJson(id: 'j1', title: '下载歌单')]),
        api,
      );

      expect(calls, isEmpty, reason: '没展开就去拉日志了');

      await tester.tap(find.text('下载歌单'));
      await tester.pumpAndSettle();

      expect(calls, contains('/api/jobs/j1'));
    });

    testWidgets('★ 展开后能看到逐首歌的结果和失败原因', (tester) async {
      await pump(
        tester,
        snapshotOf(<Map<String, dynamic>>[
          jobJson(
            id: 'j1',
            title: '下载「DJ」',
            total: 3,
            done: 3,
            items: <Map<String, dynamic>>[
              itemJson(index: 1, name: '夏天的风', artist: '周政'),
              itemJson(
                index: 2,
                name: '花桥',
                artist: '回春丹',
                status: 'failed',
                reason: '这首歌没有版权，拿不到下载链接',
              ),
              itemJson(index: 3, name: '十年', artist: '陈奕迅'),
            ],
          ),
        ]),
        fakeApi(),
      );

      await tester.tap(find.text('下载「DJ」'));
      await tester.pumpAndSettle();

      expect(find.textContaining('共 3 首'), findsOneWidget);
      // "失败 1"在摘要和详情里各有一处，所以断言到详情那句
      expect(find.textContaining('成功 2，失败 1'), findsOneWidget);
      expect(find.textContaining('夏天的风'), findsOneWidget);
      // "有 1 首失败"这句话对排查没用，得知道是哪首、为什么
      expect(find.textContaining('没有版权'), findsOneWidget);
    });

    testWidgets('展开后显示日志', (tester) async {
      await pump(
        tester,
        snapshotOf(<Map<String, dynamic>>[jobJson(id: 'j1', title: '下载歌单')]),
        fakeApi(
          logs: <String, List<Map<String, dynamic>>>{
            'j1': <Map<String, dynamic>>[
              logJson(0, '下载歌单 —— 已进入队列'),
              logJson(1, '完成'),
            ],
          },
        ),
      );

      await tester.tap(find.text('下载歌单'));
      await tester.pumpAndSettle();

      expect(find.textContaining('日志（2 行）'), findsOneWidget);
      expect(find.textContaining('已进入队列'), findsOneWidget);
    });

    testWidgets('再点一次收起', (tester) async {
      await pump(
        tester,
        snapshotOf(<Map<String, dynamic>>[
          jobJson(
            id: 'j1',
            title: '下载歌单',
            items: <Map<String, dynamic>>[itemJson(index: 1, name: '夏天的风')],
          ),
        ]),
        fakeApi(),
      );

      await tester.tap(find.text('下载歌单'));
      await tester.pumpAndSettle();
      expect(find.textContaining('夏天的风'), findsOneWidget);

      await tester.tap(find.text('下载歌单'));
      await tester.pumpAndSettle();
      expect(find.textContaining('夏天的风'), findsNothing);
    });

    testWidgets('日志拉取失败也不炸，把原因显示出来', (tester) async {
      final api = ApiClient(
        baseUrl: 'http://test',
        httpClient: MockClient((request) async {
          return http.Response(
            jsonEncode(<String, String>{'detail': '后端没起来'}),
            503,
            headers: <String, String>{
              'content-type': 'application/json; charset=utf-8',
            },
          );
        }),
      );

      await pump(
        tester,
        snapshotOf(<Map<String, dynamic>>[jobJson(id: 'j1', title: '下载歌单')]),
        api,
      );

      await tester.tap(find.text('下载歌单'));
      await tester.pumpAndSettle();

      expect(find.textContaining('日志拉取失败'), findsOneWidget);
    });
  });
}
