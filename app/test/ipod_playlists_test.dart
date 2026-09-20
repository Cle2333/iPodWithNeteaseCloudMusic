/// iPod 音乐管理页的**歌单**界面。
///
/// 这个文件守的是"改错了会动到用户歌单内容"的地方：
///
/// * **切歌单必须清空勾选**。`_selected` 里装的是上一个列表的 db_id，
///   带过去会让「从歌单移出」移错歌——那是真的改用户的歌单。
/// * **删歌单必须先确认**，且确认前不能发请求。
/// * **只读歌单要给出原因**，不能只是"点了没反应"。
/// * **移出/加入**发的 db_id 必须正好是选中的那些。
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:ipod_manager/api/client.dart';
import 'package:ipod_manager/pages/library_page.dart';
import 'package:ipod_manager/state/app_state.dart';
import 'package:ipod_manager/theme.dart';

String dbId(int i) => '1844674407370955${1000 + i}';

Map<String, dynamic> trackJson(int i) => <String, dynamic>{
  'index': i,
  'db_id': dbId(i),
  'title': '歌曲$i',
  'artist': '歌手$i',
  'album': '专辑$i',
  'size': 1000000,
  'size_text': '976.6 KB',
  'length_ms': 200000,
  'length_text': '3:20',
  'bitrate': 320,
  'play_count': 0,
};

Map<String, dynamic> tracksPayload({int count = 5}) => <String, dynamic>{
  'total': count,
  'filtered': count,
  'page': 1,
  'size': 100,
  'pages': 1,
  'filtered_bytes': count * 1000000,
  'all_bytes': count * 1000000,
  'total_text': '$count MB',
  'ipod_name': '我的iPod',
  'sorts': <Map<String, String>>[
    {'value': 'title', 'label': '标题'},
  ],
  'tracks': <Map<String, dynamic>>[
    for (var i = 0; i < count; i++) trackJson(i),
  ],
};

/// 一台"有主列表 + 一个普通歌单 + 一个智能歌单"的设备。
Map<String, dynamic> playlistsPayload() => <String, dynamic>{
  'ok': true,
  'playlists': <Map<String, dynamic>>[
    <String, dynamic>{
      'playlist_id': '111',
      'name': '我的iPod',
      'count': 5,
      'dataset': 'mhlp',
      'dataset_text': '普通',
      'is_master': true,
      'is_device_name': true,
      'editable': false,
      'readonly_reason': '「我的iPod」是主播放列表（iPod 的名字就存在它的标题里），'
          '成员和名字都不由这里改。',
    },
    <String, dynamic>{
      'playlist_id': '222',
      'name': '通勤',
      'count': 2,
      'dataset': 'mhlp',
      'dataset_text': '普通',
      'is_master': false,
      'is_device_name': false,
      'editable': true,
      'readonly_reason': '',
    },
    <String, dynamic>{
      'playlist_id': '333',
      'name': '最近添加',
      'count': 3,
      'dataset': 'mhlp_smart',
      'dataset_text': '智能',
      'is_master': false,
      'is_device_name': false,
      'editable': false,
      'readonly_reason': '「最近添加」是智能播放列表，成员是按规则算出来的，不能手动改。',
    },
  ],
  'master_id': '111',
  'track_count': 5,
};

Map<String, dynamic> playlistTracksPayload(String id, List<int> indexes) =>
    <String, dynamic>{
      'ok': true,
      'playlist_id': id,
      'name': id == '222' ? '通勤' : '最近添加',
      'dataset_text': id == '222' ? '普通' : '智能',
      'editable': id == '222',
      'readonly_reason': id == '222' ? '' : '智能播放列表不能手动改。',
      'count': indexes.length,
      'tracks': <Map<String, dynamic>>[
        for (var k = 0; k < indexes.length; k++) trackJson(indexes[k]),
      ],
    };

/// 假后端：按路径分发。返回的 [sent] 记录所有写请求的 body，
/// 用来断言"发了什么"以及"有没有发"。
ApiClient fakeApi({
  List<String>? sent,
  List<String>? paths,
  int Function(Uri uri)? statusFor,
  String Function(Uri uri)? detailFor,
}) {
  return ApiClient(
    baseUrl: 'http://test',
    httpClient: MockClient((request) async {
      final path = request.url.path;
      paths?.add('${request.method} $path');
      if (sent != null && request.method == 'POST' && request.body.isNotEmpty) {
        sent.add(request.body);
      }
      final status = statusFor?.call(request.url) ?? 200;
      if (status >= 400) {
        return http.Response(
          jsonEncode(<String, dynamic>{
            'detail': detailFor?.call(request.url) ?? '出错了',
          }),
          status,
          headers: const <String, String>{
            'content-type': 'application/json; charset=utf-8',
          },
        );
      }
      Map<String, dynamic> body;
      if (path == '/api/library/playlists') {
        body = playlistsPayload();
      } else if (path.startsWith('/api/library/playlists/') &&
          path.endsWith('/tracks') &&
          request.method == 'GET') {
        final id = path.split('/')[4];
        body = playlistTracksPayload(id, id == '222' ? <int>[1, 3] : <int>[0, 1, 2]);
      } else if (path.endsWith('/delete/preview')) {
        body = <String, dynamic>{
          'ok': true,
          'preview_id': 'tok-1',
          'playlist_id': '222',
          'name': '通勤',
          'count': 2,
          'note': '只删这个播放列表，2 首歌本身**不会被删**，它们仍在「全部歌曲」里。',
        };
      } else if (request.method == 'POST') {
        body = <String, dynamic>{'ok': true, 'job_id': 'job-1'};
      } else {
        body = tracksPayload();
      }
      return http.Response(
        jsonEncode(body),
        status,
        headers: const <String, String>{
          'content-type': 'application/json; charset=utf-8',
        },
      );
    }),
  );
}

Future<void> pumpLibrary(WidgetTester tester, ApiClient api) async {
  await tester.pumpWidget(
    ChangeNotifierProvider<AppState>.value(
      value: AppState(api: api),
      child: MaterialApp(
        theme: buildTheme(),
        home: const Scaffold(body: LibraryPage()),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

/// 切到「歌单」模式并等它读完。
Future<void> openPlaylists(WidgetTester tester) async {
  await tester.tap(find.text('歌单'));
  await tester.pumpAndSettle();
}

void main() {
  group('切到歌单模式', () {
    testWidgets('列出 iPod 上的歌单，主列表和智能列表也列出来', (tester) async {
      await pumpLibrary(tester, fakeApi());
      await openPlaylists(tester);

      expect(find.textContaining('通勤'), findsWidgets);
      expect(find.textContaining('最近添加'), findsWidgets);
      expect(find.textContaining('我的iPod'), findsWidgets);
    });

    testWidgets('★ 自动选中第一个可编辑的歌单，而不是主列表', (tester) async {
      // 默认停在能编辑的那个上，用户切过来就能直接用；
      // 停在主列表上的话，改名/删除都是灰的，看起来像坏了。
      await pumpLibrary(tester, fakeApi());
      await openPlaylists(tester);

      // 自动选中的是「通勤」，于是曲目列表是它的内容
      expect(find.text('歌曲1'), findsWidgets);
      expect(find.text('歌曲3'), findsWidgets);
    });

    testWidgets('选中歌单后曲目列表换成它的内容', (tester) async {
      await pumpLibrary(tester, fakeApi());
      await openPlaylists(tester);

      await tester.tap(find.textContaining('最近添加').first);
      await tester.pumpAndSettle();

      expect(find.text('歌曲0'), findsWidgets);
      expect(find.text('歌曲2'), findsWidgets);
      // 「通勤」只有 1 和 3，切过去之后不该还看得到
      expect(find.text('歌曲3'), findsNothing);
    });

    testWidgets('★ 切换歌单会清空勾选', (tester) async {
      // 带着上一个歌单的 db_id 去操作「从歌单移出」，移的就是错的歌。
      await pumpLibrary(tester, fakeApi());
      await openPlaylists(tester);

      await tester.tap(find.text('歌曲1'));
      await tester.pump();
      expect(find.textContaining('已选 1 首'), findsOneWidget);

      await tester.tap(find.textContaining('最近添加').first);
      await tester.pumpAndSettle();

      expect(find.textContaining('已选'), findsNothing,
          reason: '切歌单后勾选必须清空');
    });
  });

  group('只读歌单', () {
    testWidgets('★ 智能歌单的改名/删除是禁用的，且能说明原因', (tester) async {
      await pumpLibrary(tester, fakeApi());
      await openPlaylists(tester);

      await tester.tap(find.textContaining('最近添加').first);
      await tester.pumpAndSettle();

      final rename = tester.widget<TextButton>(
        find.widgetWithText(TextButton, '改名'),
      );
      expect(rename.onPressed, isNull, reason: '智能歌单不该能改名');
      final del = tester.widget<TextButton>(
        find.widgetWithText(TextButton, '删除歌单'),
      );
      expect(del.onPressed, isNull, reason: '智能歌单不该能删');

      // 原因写在 chips 的 tooltip 里（读得到，不是"点了没反应"）
      expect(
        find.byTooltip('「最近添加」是智能播放列表，成员是按规则算出来的，不能手动改。'),
        findsOneWidget,
      );
    });

    testWidgets('★ 主列表也不能改名/删除', (tester) async {
      await pumpLibrary(tester, fakeApi());
      await openPlaylists(tester);

      await tester.tap(find.textContaining('我的iPod').first);
      await tester.pumpAndSettle();

      final del = tester.widget<TextButton>(
        find.widgetWithText(TextButton, '删除歌单'),
      );
      expect(del.onPressed, isNull);
    });
  });

  group('改歌单内容', () {
    testWidgets('★ 从歌单移出：发出去的 db_id 正好是选中的那些', (tester) async {
      final sent = <String>[];
      await pumpLibrary(tester, fakeApi(sent: sent));
      await openPlaylists(tester);

      await tester.tap(find.text('歌曲1'));
      await tester.pump();
      await tester.tap(find.text('歌曲3'));
      await tester.pump();

      await tester.tap(find.textContaining('从「通勤」移出'));
      await tester.pumpAndSettle();

      expect(sent, hasLength(1));
      final body = jsonDecode(sent.first) as Map<String, dynamic>;
      expect(body['remove'], <String>[dbId(1), dbId(3)]);
      expect(body['add'], isEmpty);
    });

    testWidgets('★ 切歌单后再移出，不会带上上一个歌单的勾选', (tester) async {
      final sent = <String>[];
      await pumpLibrary(tester, fakeApi(sent: sent));
      await openPlaylists(tester);

      // 先在「通勤」里勾一首
      await tester.tap(find.text('歌曲1'));
      await tester.pump();
      // 切到智能歌单（只读，但勾选必须已经被清掉）
      await tester.tap(find.textContaining('最近添加').first);
      await tester.pumpAndSettle();

      // 再勾一首（智能歌单的曲目是 0/1/2）
      await tester.tap(find.text('歌曲0'));
      await tester.pump();

      // 回到「通勤」—— 勾选又该被清空
      await tester.tap(find.textContaining('通勤').first);
      await tester.pumpAndSettle();
      expect(find.textContaining('已选'), findsNothing);

      await tester.tap(find.text('歌曲1'));
      await tester.pump();
      await tester.tap(find.textContaining('从「通勤」移出'));
      await tester.pumpAndSettle();

      final body = jsonDecode(sent.last) as Map<String, dynamic>;
      expect(body['remove'], <String>[dbId(1)],
          reason: '只该移出这一次真正勾上的那首');
    });
  });

  group('删除歌单', () {
    testWidgets('★ 必须确认，且确认前不发删除请求', (tester) async {
      final sent = <String>[];
      final paths = <String>[];
      await pumpLibrary(tester, fakeApi(sent: sent, paths: paths));
      await openPlaylists(tester);

      await tester.tap(find.text('删除歌单'));
      await tester.pumpAndSettle();

      // 弹出了确认框，里面写清了"歌不会被删"
      expect(find.textContaining('删除歌单「通勤」？'), findsOneWidget);
      expect(find.textContaining('不会被删'), findsOneWidget);
      // 还没点确认 → 不该有 /delete 请求
      expect(paths.where((p) => p.endsWith('/playlists/delete')), isEmpty);

      await tester.tap(find.text('取消'));
      await tester.pumpAndSettle();
      expect(paths.where((p) => p.endsWith('/playlists/delete')), isEmpty,
          reason: '取消之后也不能发');
    });

    testWidgets('★ 确认后带上预览令牌', (tester) async {
      final sent = <String>[];
      await pumpLibrary(tester, fakeApi(sent: sent));
      await openPlaylists(tester);

      await tester.tap(find.text('删除歌单'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('删除这个歌单'));
      await tester.pumpAndSettle();

      final deleteBody = sent
          .map((e) => jsonDecode(e) as Map<String, dynamic>)
          .firstWhere((b) => b.containsKey('preview_id'));
      expect(deleteBody['playlist_id'], '222');
      expect(deleteBody['preview_id'], 'tok-1');
    });

    testWidgets('后端拒绝时把话说完（对话框，不是一闪而过的吐司）', (tester) async {
      await pumpLibrary(
        tester,
        fakeApi(
          statusFor: (uri) =>
              uri.path.endsWith('/delete/preview') ? 409 : 200,
          detailFor: (_) => '这是主播放列表——iPod 的名字就存在它的标题里，不能删。',
        ),
      );
      await openPlaylists(tester);

      await tester.tap(find.text('删除歌单'));
      await tester.pumpAndSettle();

      expect(find.text('这个操作没有执行'), findsOneWidget);
      expect(find.textContaining('iPod 的名字'), findsOneWidget);
    });
  });

  group('加入歌单', () {
    testWidgets('★ 在「全部歌曲」里勾选 → 加入歌单 → 发对了请求', (tester) async {
      final sent = <String>[];
      await pumpLibrary(tester, fakeApi(sent: sent));

      // 全部歌曲模式：勾两首
      await tester.tap(find.text('歌曲0'));
      await tester.pump();
      await tester.tap(find.text('歌曲2'));
      await tester.pump();

      // 图标按钮（窄窗口下为了不挤出别的按钮，只放图标）
      await tester.tap(find.byTooltip('把选中的 2 首加入歌单'));
      await tester.pumpAndSettle();

      // 选目标歌单：只列可编辑的
      expect(find.textContaining('把选中的 2 首加进'), findsOneWidget);
      expect(find.text('最近添加'), findsNothing,
          reason: '智能歌单不该出现在可选目标里');

      await tester.tap(find.text('通勤'));
      await tester.pumpAndSettle();

      final body = jsonDecode(sent.last) as Map<String, dynamic>;
      expect(body['add'], <String>[dbId(0), dbId(2)]);
    });
  });
}
