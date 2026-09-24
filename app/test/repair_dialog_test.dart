/// 「数据库修复」对话框的测试。
///
/// ## 这一页的测试重点不是"能显示"，而是"**不乱删**"
///
/// 这套流程是破坏性的：删文件、重写数据库，都收不回来。所以用例集中守三件事：
///
/// 1. **危险的那一项默认不勾。** 「断链记录」要整库重写，默认勾上就等于
///    用户点两下就把库重写了。必须由用户显式勾。
/// 2. **请求体要如实反映勾选。** 界面勾了什么就发什么——勾了 A 却发 B，
///    删错东西不会有第二次机会。
/// 3. **干净设备要说"没东西可修"**，而不是给一堆 0 让用户以为坏了。
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:ipod_manager/api/client.dart';
import 'package:ipod_manager/theme.dart';
import 'package:ipod_manager/widgets/repair_dialog.dart';

// ──────────────────────────────────────────────────────────────────────
// 夹具
// ──────────────────────────────────────────────────────────────────────

Map<String, dynamic> bucket({
  int count = 0,
  int bytes = 0,
  String sizeText = '0 B',
  List<Map<String, dynamic>> items = const <Map<String, dynamic>>[],
}) => <String, dynamic>{
  'count': count,
  'bytes': bytes,
  'size_text': sizeText,
  'items': items,
};

Map<String, dynamic> orphan(String name, int size, String sizeText) =>
    <String, dynamic>{
      'name': name,
      'rel': 'iPod_Control/Music/F00/$name',
      'size': size,
      'size_text': sizeText,
    };

Map<String, dynamic> broken(String id, String title, String artist) =>
    <String, dynamic>{
      'id': id,
      'title': title,
      'artist': artist,
      'rel': 'iPod_Control/Music/F01/$id.m4a',
      'size_text': '7.2 MB',
    };

/// 一份"有东西可修"的扫描结果。
Map<String, dynamic> damagedScan({int orphanCount = 2}) => <String, dynamic>{
  'db_tracks': 148,
  'disk_files': 148 + orphanCount,
  'clean': false,
  'summary': '$orphanCount 个孤儿文件（162.9 MB）、1 个残留临时文件',
  'orphans': bucket(
    count: orphanCount,
    bytes: 162900000,
    sizeText: '155.4 MB',
    items: <Map<String, dynamic>>[
      orphan('i47q.m4a', 25000000, '23.8 MB'),
      orphan('aehb.m4a', 27000000, '25.7 MB'),
    ].take(orphanCount).toList(),
  ),
  'broken': bucket(
    count: 1,
    bytes: 7500000,
    sizeText: '7.2 MB',
    items: <Map<String, dynamic>>[broken('6471643225376754419', '十年', '陈奕迅')],
  ),
  'stray_temp': bucket(
    count: 1,
    items: <Map<String, dynamic>>[
      <String, dynamic>{
        'rel': 'iPod_Control/iTunes/iT.tmp',
        'name': 'iT.tmp',
      },
    ],
  ),
};

Map<String, dynamic> cleanScan() => <String, dynamic>{
  'db_tracks': 148,
  'disk_files': 148,
  'clean': true,
  'summary': '数据库和磁盘完全一致，没有需要修复的地方。',
  'orphans': bucket(),
  'broken': bucket(),
  'stray_temp': bucket(),
};

/// 一次清理的结果。
Map<String, dynamic> cleanResult({
  int cleaned = 3,
  int freed = 162900000,
  /// 放进第一类（孤儿）的报错条数。> 0 = 部分失败，完成页必须换警示色。
  int errors = 0,
}) =>
    <String, dynamic>{
      'cleaned': cleaned,
      'freed_bytes': freed,
      'freed_text': '155.4 MB',
      'kinds': <Map<String, dynamic>>[
        <String, dynamic>{
          'kind': 'orphans',
          'removed': 2,
          'bytes': 162000000,
          'note': errors > 0 ? '已删除 2 个孤儿文件（$errors 个删不掉）' : '已删除 2 个孤儿文件，释放 154.5 MB',
          'errors': errors,
        },
        <String, dynamic>{
          'kind': 'stray_temp',
          'removed': 1,
          'bytes': 900000,
          'note': '已删除 1 个临时文件，释放 878.9 KB',
          'errors': 0,
        },
      ],
    };

/// 造一个假后端。
///
/// `scanJobResult` / `cleanJobResult` 为作业的 result；作业一查就是 `done`
/// （真后端也常常这么快：扫描 150 个文件只要一两秒）。
ApiClient fakeApi({
  Map<String, dynamic>? scanJobResult,
  Map<String, dynamic>? cleanJobResult,
  bool scanFails = false,
  /// true = 扫描作业**一直显示"进行中"**。
  ///
  /// 用来测"忙碌时关得掉"：真后端扫一个几千首的设备确实要几十秒，
  /// 这一段时间里对话框必须留着一条退路。假后端一秒就返回的话，
  /// 根本进不到那个状态，测试也就测了个寂寞。
  bool scanStillRunning = false,
  List<({String path, Map<String, dynamic> body})>? calls,
}) {
  var submitted = 0;
  return ApiClient(
    baseUrl: 'http://test',
    httpClient: MockClient((request) async {
      final body = request.body.isEmpty
          ? <String, dynamic>{}
          : jsonDecode(request.body) as Map<String, dynamic>;
      // 完整 URL + body 都记下来：断言"勾了什么就发什么"必须看 body
      calls?.add((path: '${request.method} ${request.url.path}', body: body));

      dynamic data;
      if (request.url.path == '/api/repair/scan') {
        data = scanFails
            ? <String, dynamic>{'ok': true, 'job_id': 'scan-bad'}
            : <String, dynamic>{'ok': true, 'job_id': 'scan-1'};
      } else if (request.url.path == '/api/repair/clean') {
        submitted++;
        data = <String, dynamic>{'ok': true, 'job_id': 'clean-$submitted'};
      } else if (request.url.path == '/api/jobs/scan-1') {
        data = <String, dynamic>{
          'id': 'scan-1',
          'kind': 'repair_scan',
          'title': '扫描设备',
          'state': scanStillRunning ? 'running' : 'done',
          'state_text': scanStillRunning ? '进行中' : '已完成',
          'percent': 100.0,
          'total': 0,
          'done': 0,
          'current': '',
          'stage': '',
          'error': '',
          'elapsed': 1,
          'items': <Map<String, dynamic>>[],
          'items_done': 0,
          'items_failed': 0,
          'result': scanJobResult ?? damagedScan(),
        };
      } else if (request.url.path == '/api/jobs/scan-bad') {
        data = <String, dynamic>{
          'id': 'scan-bad',
          'kind': 'repair_scan',
          'title': '扫描设备',
          'state': 'failed',
          'state_text': '失败',
          'error': '读不出 iPod 的数据库，没法比对当前状态（InsufficientDataError）。\n'
              '  这通常意味着数据库文件损坏。先在电脑上用 iTunes / Finder 看一眼\n'
              '  这个 iPod 是否正常，必要时用备份恢复 iPod_Control 目录，再回来重试。',
          'percent': 0.0,
          'total': 0,
          'done': 0,
          'current': '',
          'stage': '',
          'elapsed': 1,
          'items': <Map<String, dynamic>>[],
          'items_done': 0,
          'items_failed': 0,
          'result': <String, dynamic>{},
        };
      } else if (request.url.path.startsWith('/api/jobs/clean-')) {
        data = <String, dynamic>{
          'id': request.url.path.split('/').last,
          'kind': 'repair_clean',
          'title': '修复设备',
          'state': 'done',
          'state_text': '已完成',
          'percent': 100.0,
          'total': 0,
          'done': 0,
          'current': '',
          'stage': '',
          'error': '',
          'elapsed': 3,
          'items': <Map<String, dynamic>>[],
          'items_done': 0,
          'items_failed': 0,
          'result': cleanJobResult ?? cleanResult(),
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

/// 打开对话框并等到扫描结果出来。
///
/// 用**有界 pump 循环**，不用 `pumpAndSettle`：对话框上有转圈（无限动画），
/// `pumpAndSettle` 永远等不到"稳定"。
Future<bool?> openDialog(WidgetTester tester, ApiClient api) async {
  tester.view.physicalSize = const Size(1400, 1100);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  bool? result;
  await tester.pumpWidget(
    MaterialApp(
      theme: buildTheme(),
      home: Builder(
        builder: (ctx) => Scaffold(
          body: Center(
            child: ElevatedButton(
              onPressed: () async {
                result = await showRepairDialog(ctx, api);
              },
              child: const Text('打开'),
            ),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('打开'));
  await settle(tester);
  return result;
}

/// 有界 pump：让轮询 Timer（400ms 一次）跑起来，又不至于永远等下去。
Future<void> settle(WidgetTester tester, {int rounds = 8}) async {
  for (var i = 0; i < rounds; i++) {
    await tester.pump(const Duration(milliseconds: 250));
  }
}

void main() {
  group('扫描结果', () {
    testWidgets('列出三类，各自写清后果（不是只给个数字）', (tester) async {
      await openDialog(tester, fakeApi());

      expect(find.text('数据库修复'), findsOneWidget);
      expect(find.textContaining('数据库 148 首 · 磁盘 150 个文件'), findsOneWidget);

      expect(find.text('孤儿文件'), findsOneWidget);
      expect(find.textContaining('iPod 上'), findsWidgets);
      expect(find.textContaining('看不见'), findsWidgets);

      expect(find.textContaining('断链记录'), findsOneWidget);
      expect(find.textContaining('显示得出来但播不了'), findsOneWidget);

      expect(find.text('残留临时文件'), findsOneWidget);
      expect(find.textContaining('iT.tmp'), findsOneWidget);
    });

    testWidgets('★ 断链记录那一项默认**不勾**（它要整库重写）', (tester) async {
      await openDialog(tester, fakeApi());

      final boxes = tester.widgetList<Checkbox>(find.byType(Checkbox)).toList();
      expect(boxes.length, 3, reason: '三个类别各一个勾选框');

      // 顺序：孤儿 / 临时文件 / 断链记录
      expect(boxes[0].value, isTrue, reason: '孤儿文件该默认勾上（纯赚）');
      expect(boxes[1].value, isTrue, reason: '临时文件该默认勾上（纯赚）');
      expect(
        boxes[2].value,
        isFalse,
        reason: '断链记录要重写整个数据库，默认勾上等于用户点两下就重写库',
      );
    });

    testWidgets('没有的类别置灰，且写"没有"', (tester) async {
      await openDialog(
        tester,
        fakeApi(
          scanJobResult: <String, dynamic>{
            ...damagedScan(),
            'broken': bucket(),
            'stray_temp': bucket(),
          },
        ),
      );

      final boxes = tester.widgetList<Checkbox>(find.byType(Checkbox)).toList();
      expect(boxes[0].value, isTrue);
      expect(boxes[1].onChanged, isNull, reason: '没有临时文件 → 不可勾');
      expect(boxes[2].onChanged, isNull, reason: '没有断链记录 → 不可勾');
      expect(find.text('没有'), findsNWidgets(2));
    });

    testWidgets('干净设备：直说没事可修，不给一堆 0', (tester) async {
      await openDialog(tester, fakeApi(scanJobResult: cleanScan()));

      expect(find.text('没有需要修复的地方'), findsOneWidget);
      expect(find.textContaining('完全一致'), findsOneWidget);
      // 干净的时候不该出现"开始修复"按钮
      expect(find.text('开始修复'), findsNothing);
      expect(find.text('知道了'), findsOneWidget);
    });

    testWidgets('数据库读不出来：给中文原因和下一步', (tester) async {
      await openDialog(tester, fakeApi(scanFails: true));

      expect(find.textContaining('读不出 iPod 的数据库'), findsOneWidget);
      expect(find.textContaining('备份恢复'), findsOneWidget);
      expect(find.text('重新扫描'), findsOneWidget);
    });
  });

  group('★ 勾了什么就发什么（删错不会有第二次机会）', () {
    testWidgets('默认勾的两项如实发出，危险那项是 false', (tester) async {
      final calls = <({String path, Map<String, dynamic> body})>[];
      await openDialog(tester, fakeApi(calls: calls));
      calls.clear();

      await tester.tap(find.text('开始修复'));
      await settle(tester);

      final clean = calls.firstWhere((c) => c.path.endsWith('/api/repair/clean'));
      expect(clean.body['orphans'], isTrue);
      expect(clean.body['stray_temp'], isTrue);
      expect(
        clean.body['broken_records'],
        isFalse,
        reason: '没勾的那项发 true 就等于擅自重写数据库',
      );
    });

    testWidgets('用户勾上断链记录 → 请求体里要变成 true', (tester) async {
      final calls = <({String path, Map<String, dynamic> body})>[];
      await openDialog(tester, fakeApi(calls: calls));
      calls.clear();

      // 勾上第三个（断链记录）
      await tester.tap(find.byType(Checkbox).at(2));
      await tester.pump();
      await tester.tap(find.text('开始修复'));
      await settle(tester);

      final clean = calls.firstWhere((c) => c.path.endsWith('/api/repair/clean'));
      expect(clean.body['broken_records'], isTrue);
    });

    testWidgets('全部取消勾选 → 按钮禁用，且不会发请求', (tester) async {
      final calls = <({String path, Map<String, dynamic> body})>[];
      await openDialog(tester, fakeApi(calls: calls));
      calls.clear();

      await tester.tap(find.byType(Checkbox).at(0));
      await tester.pump();
      await tester.tap(find.byType(Checkbox).at(1));
      await tester.pump();

      final button = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, '开始修复'),
      );
      expect(button.onPressed, isNull, reason: '一个都没勾还能点，等于发一个空请求');

      expect(
        calls.where((c) => c.path.endsWith('/api/repair/clean')),
        isEmpty,
      );
    });

    testWidgets('没动过手就关掉 → 不触发刷新', (tester) async {
      // 只是打开看一眼：返回值必须是 false，否则调用方会白读一遍全库
      final api = fakeApi();
      tester.view.physicalSize = const Size(1400, 1100);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      bool? result;
      await tester.pumpWidget(
        MaterialApp(
          theme: buildTheme(),
          home: Builder(
            builder: (ctx) => Scaffold(
              body: Center(
                child: ElevatedButton(
                  onPressed: () async {
                    result = await showRepairDialog(ctx, api);
                  },
                  child: const Text('打开'),
                ),
              ),
            ),
          ),
        ),
      );
      await tester.tap(find.text('打开'));
      await settle(tester);
      await tester.tap(find.text('取消'));
      await settle(tester);

      expect(result, isFalse, reason: '没动手却让调用方刷新，用户白等一次全量重读');
    });
  });

  group('清理结果', () {
    testWidgets('逐条说明做了什么 + 一共释放多少', (tester) async {
      await openDialog(tester, fakeApi());
      await tester.tap(find.text('开始修复'));
      await settle(tester);

      expect(find.text('修复完成'), findsOneWidget);
      expect(find.textContaining('已删除 2 个孤儿文件'), findsOneWidget);
      expect(find.textContaining('已删除 1 个临时文件'), findsOneWidget);
      expect(find.textContaining('共释放 155.4 MB'), findsOneWidget);
    });

    testWidgets('动过手后关闭 → 返回 true，调用方去刷新列表', (tester) async {
      final api = fakeApi();
      tester.view.physicalSize = const Size(1400, 1100);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      bool? result;
      await tester.pumpWidget(
        MaterialApp(
          theme: buildTheme(),
          home: Builder(
            builder: (ctx) => Scaffold(
              body: Center(
                child: ElevatedButton(
                  onPressed: () async {
                    result = await showRepairDialog(ctx, api);
                  },
                  child: const Text('打开'),
                ),
              ),
            ),
          ),
        ),
      );
      await tester.tap(find.text('打开'));
      await settle(tester);
      await tester.tap(find.text('开始修复'));
      await settle(tester);
      await tester.tap(find.text('完成'));
      await settle(tester);

      expect(result, isTrue);
    });

    testWidgets('什么都没清到（cleaned=0）→ 不该让调用方刷新', (tester) async {
      final api = fakeApi(
        cleanJobResult: cleanResult(cleaned: 0, freed: 0),
      );
      tester.view.physicalSize = const Size(1400, 1100);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      bool? result;
      await tester.pumpWidget(
        MaterialApp(
          theme: buildTheme(),
          home: Builder(
            builder: (ctx) => Scaffold(
              body: Center(
                child: ElevatedButton(
                  onPressed: () async {
                    result = await showRepairDialog(ctx, api);
                  },
                  child: const Text('打开'),
                ),
              ),
            ),
          ),
        ),
      );
      await tester.tap(find.text('打开'));
      await settle(tester);
      await tester.tap(find.text('开始修复'));
      await settle(tester);
      await tester.tap(find.text('完成'));
      await settle(tester);

      expect(result, isFalse, reason: '一项都没清到，刷新是白费');
    });

    testWidgets('★ 清理有失败项 → 不能给绿勾说「修复完成」', (tester) async {
      // 后端每类都回 errors（删文件被占用/没权限），断链那条还可能是
      // "重写后读回校验未通过"——最该被看见的失败，不能藏在绿勾底下。
      await openDialog(tester, fakeApi(cleanJobResult: cleanResult(errors: 2)));
      await tester.tap(find.text('开始修复'));
      await settle(tester);

      expect(find.text('修复完成，但有东西没做成'), findsOneWidget);
      expect(
        find.text('修复完成'),
        findsNothing,
        reason: '有失败项还报「修复完成」，用户会以为都清干净了',
      );
    });

    testWidgets('没有失败项时仍然是「修复完成」', (tester) async {
      await openDialog(tester, fakeApi());
      await tester.tap(find.text('开始修复'));
      await settle(tester);

      expect(find.text('修复完成'), findsOneWidget);
    });
  });

  group('文案', () {
    testWidgets('★ 界面上不出现 Markdown 星号', (tester) async {
      // 强调留在源码里（读代码看得见重点），但 Text 不渲染 Markdown，
      // 不过一道去标记就会被用户看到「勾选的会被清掉，**不能撤销**」。
      await openDialog(tester, fakeApi());

      expect(
        find.textContaining('**'),
        findsNothing,
        reason: '星号漏到界面上了 —— 每处显示前都要过 _plain()',
      );
      expect(find.textContaining('不能撤销'), findsOneWidget);
    });
  });

  group('★ 忙碌时关得掉（不然只能杀进程）', () {
    testWidgets('扫描中「取消扫描」可用、点了能关掉、且通知后端取消', (tester) async {
      final calls = <({String path, Map<String, dynamic> body})>[];
      // 作业一直"进行中" → 对话框停在扫描态
      final api = fakeApi(calls: calls, scanStillRunning: true);

      tester.view.physicalSize = const Size(1400, 1100);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      bool? result;
      await tester.pumpWidget(
        MaterialApp(
          theme: buildTheme(),
          home: Builder(
            builder: (ctx) => Scaffold(
              body: Center(
                child: ElevatedButton(
                  onPressed: () async {
                    result = await showRepairDialog(ctx, api);
                  },
                  child: const Text('打开'),
                ),
              ),
            ),
          ),
        ),
      );
      await tester.tap(find.text('打开'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 600));

      expect(
        find.textContaining('正在扫描 iPod'),
        findsOneWidget,
        reason: '没进到扫描态，这条测试就没意义',
      );

      // 扫描态的按钮必须是**活的**
      final button = tester.widget<TextButton>(
        find.widgetWithText(TextButton, '取消扫描'),
      );
      expect(
        button.onPressed,
        isNotNull,
        reason: '扫描中按钮禁用 = 用户关不掉窗口',
      );

      await tester.tap(find.text('取消扫描'));
      await settle(tester);

      expect(find.text('数据库修复'), findsNothing, reason: '对话框没关掉');
      expect(result, isFalse, reason: '只是取消，没动过手，不该让调用方刷新');
      expect(
        calls.where((c) => c.path.contains('/cancel')),
        isNotEmpty,
        reason: '没通知后端取消，作业还在白跑',
      );
    });
  });
}
