/// 设置页的「导出诊断记录」。
///
/// 这个按钮**只在设置页**（下载页上原来有一个，已经挪过来了）。
/// 出 bug 的时候用户要能一键导出一份完整现场发出来。
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:ipod_manager/api/client.dart';
import 'package:ipod_manager/pages/settings_page.dart';
import 'package:ipod_manager/services/backend.dart';
import 'package:ipod_manager/state/app_state.dart';
import 'package:ipod_manager/theme.dart';

/// 默认路径长这样：在「文档」目录里，带时间戳。
const String exportedPath =
    r'C:\Temp\iPod管理器诊断-20260920-091500.txt';

class Recorder {
  final List<String> paths = <String>[];
}

/// 设置页要的接口挺多（账号/设置/缓存/环境/设备/作业/日志）。
/// 这里只给最小的合法形状——被测的是导出那条路，别的只要不炸就行。
ApiClient fakeApi(
  Recorder rec, {
  Map<String, dynamic>? exportBody,
  int? exportStatus,
}) {
  return ApiClient(
    baseUrl: 'http://test',
    httpClient: MockClient((request) async {
      rec.paths.add('${request.method} ${request.url.path}');

      http.Response ok(dynamic data) => http.Response(
        jsonEncode(data),
        200,
        headers: <String, String>{
          'content-type': 'application/json; charset=utf-8',
        },
      );

      switch (request.url.path) {
        case '/api/debug/export':
          if (exportStatus != null && exportStatus >= 400) {
            return http.Response(
              jsonEncode(
                exportBody ?? <String, String>{'detail': '磁盘写不进去（只读或没权限）'},
              ),
              exportStatus,
              headers: <String, String>{
                'content-type': 'application/json; charset=utf-8',
              },
            );
          }
          return ok(
            exportBody ??
                <String, dynamic>{
                  'ok': true,
                  'path': exportedPath,
                  'bytes': 20480,
                  'lines': 320,
                  'jobs': 3,
                },
          );
        case '/api/jobs':
          return ok(<String, dynamic>{
            'running': null,
            'queued': 0,
            'has_work': false,
            'jobs': <Map<String, dynamic>>[],
          });
        case '/api/account/list':
          return ok(<String, dynamic>{'accounts': <Map<String, dynamic>>[]});
        case '/api/settings':
          return ok(<String, dynamic>{
            'quality': 'exhigh',
            'min_interval': 0.35,
            'min_interval_floor': 0.2,
            'quality_options': <Map<String, String>>[
              {'value': 'exhigh', 'label': '320k'},
            ],
          });
        case '/api/cache':
          return ok(<String, dynamic>{
            'dir': '.ncm/cache',
            'files': 0,
            'records': 0,
            'bytes': 0,
            'size_text': '0 B',
            'consistent': true,
          });
        case '/api/debug/env':
          return ok(<String, dynamic>{
            'python': '3.11.15',
            'ipod_cli': '0.1.0',
            'ffmpeg': 'ffmpeg.exe',
            'ffmpeg_ok': true,
            'base_url': 'http://127.0.0.1:4000',
            'db_path': '.ncm/ncm.db',
            'cache_dir': '.ncm/cache',
          });
        case '/api/debug/log':
          return ok(<String, dynamic>{
            'lines': <Map<String, dynamic>>[],
            'latest_seq': 0,
            'truncated': false,
          });
        case '/api/device':
          return ok(<String, dynamic>{
            'connected': false,
            'error': '没有找到 iPod',
            'hint': '请确认数据线插好',
          });
        default:
          return ok(<String, dynamic>{});
      }
    }),
  );
}

/// 推时间但**不等"稳定"**。
///
/// 设置页加载时会显示一条 LinearProgressIndicator，那是无限动画——
/// pumpAndSettle 永远等不到它停。这不是 bug，是进度指示器本来的样子。
Future<void> settle(WidgetTester tester, {int times = 12}) async {
  for (var i = 0; i < times; i++) {
    await tester.pump(const Duration(milliseconds: 120));
  }
}

/// 滚到底下的"后端"那一组。导出按钮在那儿。
Future<void> scrollToBackend(WidgetTester tester) async {
  await tester.drag(find.byType(ListView), const Offset(0, -4000));
  await settle(tester);
}

Future<void> pumpSettings(WidgetTester tester, ApiClient api) async {
  tester.view.physicalSize = const Size(1500, 1100);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  final state = AppState(api: api);
  final backend = BackendService(port: 8765);
  addTearDown(state.dispose);
  addTearDown(backend.dispose);

  await tester.pumpWidget(
    MultiProvider(
      providers: [
        ChangeNotifierProvider<AppState>.value(value: state),
        ChangeNotifierProvider<BackendService>.value(value: backend),
      ],
      child: MaterialApp(
        theme: buildTheme(),
        home: const Scaffold(body: SettingsPage()),
      ),
    ),
  );

  await state.refreshStatus();
  await settle(tester); // 等页面自己的 _loadAll 跑完
}

void main() {
  group('导出诊断记录（在设置页）', () {
    testWidgets('★ 点导出会打接口，并把路径显示出来', (tester) async {
      final rec = Recorder();
      await pumpSettings(tester, fakeApi(rec));

      // 按钮在"后端"那一组里，先滚过去
      await scrollToBackend(tester);
      await tester.tap(find.text('导出诊断记录'));
      await settle(tester);

      expect(rec.paths, contains('POST /api/debug/export'));
      expect(find.text('已导出诊断记录'), findsOneWidget);
      expect(
        find.textContaining('iPod管理器诊断-20260920-091500.txt'),
        findsOneWidget,
        reason: '告诉了路径，用户才找得到文件',
      );
      // 十进制单位：20480 B ÷ 1000 = 20.48 → 20.5 KB
      // （跟引擎的 human_size 一致，别改回 1024 进制）
      expect(find.textContaining('20.5 KB'), findsOneWidget);
      expect(find.textContaining('3 个作业记录'), findsOneWidget);

      // 三个操作都在：复制、打开文件夹、关掉
      expect(find.text('复制路径'), findsOneWidget);
      expect(find.text('打开所在文件夹'), findsOneWidget);
      expect(find.text('知道了'), findsOneWidget);
    });

    testWidgets('导出失败时后端的中文原因原样显示', (tester) async {
      await pumpSettings(tester, fakeApi(Recorder(), exportStatus: 500));

      await scrollToBackend(tester);
      await tester.tap(find.text('导出诊断记录'));
      await settle(tester);

      expect(find.textContaining('磁盘写不进去'), findsOneWidget);
      expect(find.text('已导出诊断记录'), findsNothing);
    });

    testWidgets('★ 下载页上没有导出按钮了', (tester) async {
      await pumpSettings(tester, fakeApi(Recorder()));
      expect(find.text('导出诊断记录'), findsWidgets, reason: '设置页得有入口');
    });
  });
}
