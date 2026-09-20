/// iPod 音乐管理页的多选与删除确认。
///
/// 这个文件守的是**最危险的那段逻辑**：这一页真的会删歌。
/// 多选算错一个边界（比如"全选"实际选了整个库而不是当前页），
/// 代价就是用户的歌没了。所以这里连按钮文案里的数量都断言。
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:ipod_manager/api/client.dart';
import 'package:ipod_manager/pages/library_page.dart';
import 'package:ipod_manager/state/app_state.dart';
import 'package:ipod_manager/theme.dart';

/// 真机上一个曲目的持久 ID 长这样：随机 64 位**无符号**数。
///
/// ★ 这里必须用字符串拼，不能写字面量——`18446744073709551000` 在 Dart 里
/// **根本表示不出来**（int 是有符号 64 位，编译期就报
/// "can't be represented in 64 bits"）。这正是后端把 `db_id` 当字符串传的
/// 原因：真机 118 首里有 61 首超过 2^63-1，当数字传会静默溢出成负数，
/// 然后删除操作就会删错歌。
String hugeDbId(int i) {
  final tail = 1000 + i;
  return '1844674407370955$tail';
}

/// 造一个假的 iPod 曲库：[count] 首，搜索时全部命中。
Map<String, dynamic> tracksPayload({
  int count = 5,
  int page = 1,
  int size = 100,
  int? filtered,
  String search = '',
}) {
  final effective = filtered ?? count;
  final start = (page - 1) * size;
  final windowCount = (effective - start).clamp(0, size);
  return <String, dynamic>{
    'total': count,
    'filtered': effective,
    'page': page,
    'size': size,
    'pages': effective == 0 ? 1 : ((effective + size - 1) ~/ size),
    'filtered_bytes': effective * 1000000,
    'all_bytes': count * 1000000,
    'total_text': '$effective MB',
    'ipod_name': '我的iPod',
    'sorts': <Map<String, String>>[
      {'value': 'title', 'label': '标题'},
    ],
    'tracks': <Map<String, dynamic>>[
      for (var i = start; i < start + windowCount; i++)
        <String, dynamic>{
          'index': i,
          'db_id': hugeDbId(i),
          'title': '歌曲$i',
          'artist': '歌手$i',
          'album': '专辑$i',
          'size': 1000000,
          'size_text': '976.6 KB',
          'length_ms': 200000,
          'length_text': '3:20',
          'bitrate': 320,
          'play_count': 0,
        },
    ],
  };
}

ApiClient fakeApi({
  Map<String, dynamic> Function(Uri uri)? handler,
  List<String>? recordedBodies,

  /// 按路径定状态码——用来造"后端拒绝"（比如 409 不给清空曲库）。
  /// 不能全局设状态码：页面一进来就要加载曲目列表，那一步得成功。
  int Function(Uri uri)? statusFor,

  /// 按路径定 `detail` 文案（FastAPI 的错误体就是 {"detail": ...}）。
  String Function(Uri uri)? detailFor,
}) {
  return ApiClient(
    baseUrl: 'http://test',
    httpClient: MockClient((request) async {
      if (recordedBodies != null && request.body.isNotEmpty) {
        recordedBodies.add(request.body);
      }
      final status = statusFor?.call(request.url) ?? 200;
      final data = status >= 400
          ? <String, dynamic>{'detail': detailFor?.call(request.url) ?? '出错了'}
          : (handler?.call(request.url) ?? tracksPayload());
      return http.Response(
        jsonEncode(data),
        status,
        headers: <String, String>{
          'content-type': 'application/json; charset=utf-8',
        },
      );
    }),
  );
}

Widget wrap(ApiClient api) {
  final state = AppState(api: api);
  return ChangeNotifierProvider<AppState>.value(
    value: state,
    child: MaterialApp(
      theme: buildTheme(),
      home: const Scaffold(body: LibraryPage()),
    ),
  );
}

Future<void> pumpLibrary(WidgetTester tester, ApiClient api) async {
  await tester.pumpWidget(wrap(api));
  await tester.pumpAndSettle();
}

/// 按住修饰键再点某一行，然后松开。模拟真实的 Ctrl+点击 / Shift+点击。
Future<void> tapWithModifier(
  WidgetTester tester,
  Finder finder,
  LogicalKeyboardKey modifier,
) async {
  await tester.sendKeyDownEvent(modifier);
  await tester.tap(finder);
  await tester.pump();
  await tester.sendKeyUpEvent(modifier);
  await tester.pump();
}

void main() {
  group('曲目列表', () {
    testWidgets('列出曲目，标题和艺人都在', (tester) async {
      await pumpLibrary(tester, fakeApi());
      expect(find.text('歌曲0'), findsOneWidget);
      expect(find.text('歌曲4'), findsOneWidget);
      // 艺人拼在副标题里
      expect(find.textContaining('歌手0'), findsWidgets);
    });

    testWidgets('空库给引导而不是空白', (tester) async {
      await pumpLibrary(
        tester,
        fakeApi(handler: (_) => tracksPayload(count: 0)),
      );
      expect(find.textContaining('还没有歌'), findsOneWidget);
      expect(find.textContaining('导入音乐'), findsWidgets);
    });

    testWidgets('搜索无结果时说清楚', (tester) async {
      await pumpLibrary(
        tester,
        fakeApi(handler: (_) => tracksPayload(count: 7, filtered: 0)),
      );
      expect(find.textContaining('没有匹配'), findsOneWidget);
      expect(find.textContaining('共 7 首'), findsOneWidget);
    });
  });

  group('多选', () {
    testWidgets('单击只选中一首', (tester) async {
      await pumpLibrary(tester, fakeApi());

      await tester.tap(find.text('歌曲0'));
      await tester.pump();

      expect(find.textContaining('已选 1 首'), findsOneWidget);
    });

    testWidgets('★ 单击另一首是**累加**，跟歌单页一致', (tester) async {
      // 以前这里是"只选它"（清掉其它），照着文件管理器的老习惯设计的。
      // 但行左边画的是**复选框**，用户看到方框就以为点一下是勾它——
      // 点第二首时第一首自动取消，多选根本没法用。
      // 用户原话："选择一个另一个的选框就会取消。"
      // 而且歌单页同一件事本来就是累加的，两页不一致更容易懵。
      await pumpLibrary(tester, fakeApi());

      await tester.tap(find.text('歌曲0'));
      await tester.pump();
      await tester.tap(find.text('歌曲2'));
      await tester.pump();

      expect(
        find.textContaining('已选 2 首'),
        findsOneWidget,
        reason: '复选框就该是复选框的行为：点一下勾它，再点一下取消',
      );
    });

    testWidgets('★ 连续点几首都能攒起来（多选的主路径）', (tester) async {
      await pumpLibrary(tester, fakeApi());

      for (final name in <String>['歌曲0', '歌曲1', '歌曲3']) {
        await tester.tap(find.text(name));
        await tester.pump();
      }

      expect(find.textContaining('已选 3 首'), findsOneWidget);
    });

    testWidgets('Ctrl+点击切换某一首', (tester) async {
      await pumpLibrary(tester, fakeApi());

      await tester.tap(find.text('歌曲0'));
      await tester.pump();
      await tapWithModifier(
        tester,
        find.text('歌曲2'),
        LogicalKeyboardKey.controlLeft,
      );

      expect(find.textContaining('已选 2 首'), findsOneWidget);

      // 再 Ctrl 点一次就取消选中
      await tapWithModifier(
        tester,
        find.text('歌曲2'),
        LogicalKeyboardKey.controlLeft,
      );
      expect(find.textContaining('已选 1 首'), findsOneWidget);
    });

    testWidgets('★ Shift+点击选中一整段', (tester) async {
      await pumpLibrary(tester, fakeApi());

      await tester.tap(find.text('歌曲1'));
      await tester.pump();
      await tapWithModifier(
        tester,
        find.text('歌曲3'),
        LogicalKeyboardKey.shiftLeft,
      );

      // 1、2、3 三首，不是两首（端点要含进去）
      expect(find.textContaining('已选 3 首'), findsOneWidget);
    });

    testWidgets('Shift 反向选也对（从下往上）', (tester) async {
      await pumpLibrary(tester, fakeApi());

      await tester.tap(find.text('歌曲3'));
      await tester.pump();
      await tapWithModifier(
        tester,
        find.text('歌曲1'),
        LogicalKeyboardKey.shiftLeft,
      );

      expect(find.textContaining('已选 3 首'), findsOneWidget);
    });

    testWidgets('★ 「全选本页」和「全选筛选结果」是两个按钮，各自写明数量', (tester) async {
      // 造一个"筛选出 246 首、但当前页只有 5 首"的场景
      await pumpLibrary(
        tester,
        fakeApi(handler: (_) => tracksPayload(count: 246, size: 100)),
      );

      // 操作条只在有选中时出现，所以先选一首
      await tester.tap(find.text('歌曲0'));
      await tester.pump();

      expect(find.text('全选本页（100 首）'), findsOneWidget, reason: '本页数量没写清或者写错了');
      expect(
        find.text('全选筛选结果（246 首）'),
        findsOneWidget,
        reason: '筛选结果数量没写清或者写错了',
      );
    });

    testWidgets('★ 全选筛选结果超出已加载范围时会明说', (tester) async {
      await pumpLibrary(
        tester,
        fakeApi(handler: (_) => tracksPayload(count: 246, size: 100)),
      );

      await tester.tap(find.text('歌曲0'));
      await tester.pump();
      await tester.tap(find.text('全选筛选结果（246 首）'));
      await tester.pump();

      // 只能选到已加载的 5 首；剩下的要翻页——必须告诉用户，
      // 不能让他以为整个 246 首都选上了
      expect(find.textContaining('已选中当前这一页的 100 首'), findsOneWidget);
      expect(find.textContaining('筛选结果共 246 首'), findsOneWidget);
    });

    testWidgets('Esc 清空选择', (tester) async {
      await pumpLibrary(tester, fakeApi());

      await tester.tap(find.text('歌曲0'));
      await tester.pump();
      expect(find.textContaining('已选 1 首'), findsOneWidget);

      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await tester.pumpAndSettle();

      expect(find.textContaining('已选'), findsNothing);
    });

    testWidgets('Ctrl+A 全选', (tester) async {
      await pumpLibrary(tester, fakeApi());

      await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
      await tester.pumpAndSettle();

      expect(find.textContaining('已选 5 首'), findsOneWidget);
    });

    testWidgets('已选体积按真实大小算', (tester) async {
      await pumpLibrary(tester, fakeApi());

      await tester.tap(find.text('歌曲0'));
      await tester.pump();
      // 每首 1000000 字节 ≈ 976.6 KB
      expect(find.textContaining('976'), findsWidgets);
    });
  });

  group('危险操作', () {
    testWidgets('★ 删除必须先预览，且确认按钮写明数量', (tester) async {
      List<String> bodies = <String>[];
      final api = fakeApi(
        recordedBodies: bodies,
        handler: (uri) {
          if (uri.path.contains('remove/preview')) {
            return <String, dynamic>{
              'ok': true,
              'preview_id': 'tok-abc',
              'count': 2,
              'bytes': 2000000,
              'size_text': '1.9 MB',
              'remaining': 3,
              'missing': 0,
              'items': <Map<String, String>>[
                {'title': '歌曲0', 'artist': '歌手0', 'size_text': '976.6 KB'},
                {'title': '歌曲1', 'artist': '歌手1', 'size_text': '976.6 KB'},
              ],
            };
          }
          return tracksPayload();
        },
      );
      await pumpLibrary(tester, api);

      await tester.tap(find.text('歌曲0'));
      await tester.pump();
      await tapWithModifier(
        tester,
        find.text('歌曲1'),
        LogicalKeyboardKey.controlLeft,
      );
      expect(find.textContaining('已选 2 首'), findsOneWidget);

      await tester.tap(find.textContaining('删除选中的 2 首'));
      await tester.pumpAndSettle();

      // 预览对话框：数量、释放空间、曲名、剩余数量，一样都不能少
      expect(find.text('确认删除'), findsOneWidget);
      expect(find.textContaining('删除 2 首'), findsWidgets);
      expect(find.textContaining('1.9 MB'), findsWidgets);
      expect(find.textContaining('歌曲0'), findsWidgets);
      expect(find.textContaining('剩余 3 首'), findsOneWidget);

      // ★ 确认按钮必须写明数量——"确认"两个字的按钮会让人手滑
      expect(find.text('确认删除 2 首'), findsOneWidget);
      expect(find.text('取消'), findsOneWidget);

      // 还没确认，后端不该收到删除请求
      expect(
        bodies.where((b) => b.contains('"ids"')).length,
        1,
        reason: '只该有 preview 那一次带 ids 的请求',
      );
    });

    testWidgets('预览里点取消就真的什么都不删', (tester) async {
      List<String> bodies = <String>[];
      final api = fakeApi(
        recordedBodies: bodies,
        handler: (uri) {
          if (uri.path.contains('remove/preview')) {
            return <String, dynamic>{
              'ok': true,
              'preview_id': 'tok-xyz',
              'count': 1,
              'bytes': 1000000,
              'size_text': '976.6 KB',
              'remaining': 4,
              'missing': 0,
              'items': <Map<String, String>>[
                {'title': '歌曲0', 'artist': '', 'size_text': '976.6 KB'},
              ],
            };
          }
          return tracksPayload();
        },
      );
      await pumpLibrary(tester, api);

      await tester.tap(find.text('歌曲0'));
      await tester.pump();
      await tester.tap(find.textContaining('删除选中的 1 首'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('取消'));
      await tester.pumpAndSettle();

      expect(
        bodies.where((b) => b.contains('/remove"')).length,
        0,
        reason: '取消了却还是发了删除请求',
      );
      expect(find.textContaining('已选 1 首'), findsOneWidget);
    });

    testWidgets('后端拒绝时错误原样显示（比如"会清空曲库"）', (tester) async {
      final api = ApiClient(
        baseUrl: 'http://test',
        httpClient: MockClient((request) async {
          if (request.url.path.contains('remove/preview')) {
            return http.Response(
              jsonEncode(<String, String>{
                'detail': '这会删掉全部 5 首曲目，把曲库清空。本工具不提供清空操作。',
              }),
              409,
              headers: <String, String>{
                'content-type': 'application/json; charset=utf-8',
              },
            );
          }
          return http.Response(
            jsonEncode(tracksPayload()),
            200,
            headers: <String, String>{
              'content-type': 'application/json; charset=utf-8',
            },
          );
        }),
      );
      await pumpLibrary(tester, api);

      await tester.tap(find.text('歌曲0'));
      await tester.pump();
      await tester.tap(find.textContaining('删除选中的 1 首'));
      await tester.pumpAndSettle();

      // 后端已经写了能看懂的中文原因，界面必须原样转达，
      // 不能吞掉换成"操作失败"
      expect(find.textContaining('曲库清空'), findsWidgets);
    });
  });

  testWidgets('★ 后端拒绝不做事时用**对话框**把话说完（不是一闪而过的吐司）', (tester) async {
    // 用户实测：逐首删到只剩 1 首，再删"就没反应了"——只看到一闪而过
    // 的提示。后端那条 409 里写着原因和替代做法，塞进 4 秒的 SnackBar
    // 等于没说。这类"我们决定不做"的消息要让人能读完、能复制。
    const refusal =
        '这会删掉 iPod 上全部 1 首曲目，把曲库清空。\n\n'
        '本工具不提供清空操作——删除是整库重写，写下去没法撤销，\n'
        '误点的代价太大。想清空有更稳妥的路子：\n\n'
        '  · 用 Finder / iTunes 的「恢复 iPod」\n'
        '  · 或在 iPod 上「设置 → 通用 → 还原 → 抹掉所有内容和设置」';

    final api = fakeApi(
      statusFor: (uri) => uri.path.contains('remove/preview') ? 409 : 200,
      detailFor: (uri) => refusal,
    );
    await pumpLibrary(tester, api);

    await tester.tap(find.text('歌曲0'));
    await tester.pump();
    await tester.tap(find.textContaining('删除选中的 1 首'));
    await tester.pumpAndSettle();

    expect(find.text('这个操作没有执行'), findsOneWidget);
    expect(
      find.textContaining('抹掉所有内容和设置'),
      findsOneWidget,
      reason: '得给出替代做法，不能只说"不给做"',
    );
    expect(find.byType(SnackBar), findsNothing, reason: '这种消息不该用会自动消失的吐司');

    await tester.tap(find.text('知道了'));
    await tester.pumpAndSettle();
    expect(find.text('这个操作没有执行'), findsNothing);
  });
}
