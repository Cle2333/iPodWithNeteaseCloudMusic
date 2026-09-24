/// 修复相关模型的解析边界。
///
/// ## 为什么值得单独一个文件
///
/// 这些模型是**接口契约的落点**。后端少发一个字段、或者把 id 当数字发，
/// 界面不会报错，只会静默显示错的东西（或者永远转圈）。对话框的测试用的是
/// 形状良好的夹具，覆盖不到"字段缺了""形状不对"这些情况，所以边界在这里钉。
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:ipod_manager/api/client.dart';
import 'package:ipod_manager/api/models.dart';
import 'package:http/testing.dart';
import 'package:http/http.dart' as http;

/// 形状良好的扫描结果夹具（要改哪一项就在这里覆盖）。
Map<String, dynamic> scanJson({
  Map<String, dynamic>? orphans,
  Map<String, dynamic>? broken,
  Map<String, dynamic>? strayTemp,
}) =>
    <String, dynamic>{
      'db_tracks': 148,
      'disk_files': 150,
      'clean': false,
      'summary': '2 个孤儿文件（155.4 MB）',
      'orphans': orphans ??
          <String, dynamic>{'count': 0, 'bytes': 0, 'size_text': '0 B', 'items': []},
      'broken': broken ??
          <String, dynamic>{'count': 0, 'bytes': 0, 'size_text': '0 B', 'items': []},
      'stray_temp': strayTemp ?? <String, dynamic>{'count': 0, 'items': []},
    };

void main() {
  group('RepairCleanResult', () {
    test('各类别的 errors 汇总起来，> 0 就是有失败项', () {
      final result = RepairCleanResult.fromJson(<String, dynamic>{
        'cleaned': 2,
        'freed_bytes': 100,
        'freed_text': '100 B',
        'kinds': <Map<String, dynamic>>[
          <String, dynamic>{'kind': 'orphans', 'note': '删了 2 个', 'errors': 0},
          <String, dynamic>{
            'kind': 'broken',
            'note': '已清掉 1 条断链记录，但读回校验未通过',
            'errors': 1,
          },
        ],
      });

      expect(result.cleaned, 2);
      expect(result.notes, hasLength(2));
      expect(result.errors, 1);
      expect(
        result.hasFailure,
        isTrue,
        reason: '断链重写校验失败必须算失败，否则完成页会给绿勾',
      );
    });

    test('kinds 缺字段 / 形状不对时退化成「没有失败项」，不抛', () {
      final result = RepairCleanResult.fromJson(<String, dynamic>{'cleaned': 0});

      expect(result.errors, 0);
      expect(result.hasFailure, isFalse);
      expect(result.notes, isEmpty);

      // kinds 不是列表也要能扛住（后端出错时可能回别的东西）
      final weird = RepairCleanResult.fromJson(<String, dynamic>{
        'kinds': 'oops',
      });
      expect(weird.errors, 0);
      expect(weird.notes, isEmpty);
    });

    test('全部成功时 hasFailure 为 false', () {
      final result = RepairCleanResult.fromJson(<String, dynamic>{
        'cleaned': 3,
        'kinds': <Map<String, dynamic>>[
          <String, dynamic>{'note': 'a', 'errors': 0},
          <String, dynamic>{'note': 'b', 'errors': 0},
        ],
      });
      expect(result.hasFailure, isFalse);
    });
  });

  group('RepairScan 的 id', () {
    test('★ 64 位无符号 id 以字符串承载，不能被截断成负数', () {
      // iPod 的持久 ID 是随机 64 位**无符号**数，真机里超过 2^63-1 的一抓
      // 一大把。当 JSON 数字发出去，Dart 的有符号 int 会静默溢出成负数。
      const big = '10987654321098765432';   // > 2^63-1 = 9223372036854775807

      final scan = RepairScan.fromJson(scanJson(
        broken: <String, dynamic>{
          'count': 1,
          'bytes': 1024,
          'size_text': '1.0 KB',
          'items': <Map<String, dynamic>>[
            <String, dynamic>{
              'id': big,
              'title': '测试歌曲',
              'artist': '歌手',
              'rel': 'iPod_Control/Music/F00/AB12.m4a',
              'size_text': '1.0 KB',
            },
          ],
        },
      ));

      expect(scan.broken.broken.single.id, big);
      // ★ 反过来钉住"为什么必须是字符串"：这个 id 根本塞不进 Dart 的
      // 有符号 64 位 int（tryParse 直接返回 null）。它要是被当数字传来，
      // 界面拿到的就是溢出后的负数，跟设备上的记录对不上。
      expect(
        int.tryParse(big),
        isNull,
        reason: '这个夹具必须超出 int 范围，否则测不到溢出那条路',
      );
    });

    test('items 缺字段时退化成空串/默认值，不抛', () {
      final scan = RepairScan.fromJson(scanJson(
        broken: <String, dynamic>{
          'count': 1,
          'items': <Map<String, dynamic>>[<String, dynamic>{}],
        },
      ));

      final track = scan.broken.broken.single;
      expect(track.id, '');
      expect(track.title, '');
    });

    test('items 不是列表时当成空，不抛', () {
      final scan = RepairScan.fromJson(scanJson(
        orphans: <String, dynamic>{'count': 3, 'items': 'oops'},
      ));

      expect(scan.orphans.count, 3);
      expect(scan.orphans.orphans, isEmpty);
    });
  });

  group('ApiClient 的修复接口', () {
    ApiClient apiWith(dynamic Function(http.Request) reply) => ApiClient(
          baseUrl: 'http://test',
          httpClient: MockClient((request) async => http.Response(
                jsonEncode(reply(request)),
                200,
                headers: <String, String>{'content-type': 'application/json'},
              )),
        );

    test('★ repairScan 遇到 ok:false 要抛，不能静默返回空 jobId', () {
      // 不抛的话调用方拿着空 jobId 的 _watch 永远不轮询，
      // 界面停在「正在扫描 iPod…」且不给任何提示。
      final api = apiWith(
        (_) => <String, dynamic>{'ok': false, 'message': '设备正忙'},
      );

      expect(api.repairScan(), throwsA(isA<ApiException>()));
    });

    test('repairClean 同样校验 ok:false', () {
      final api = apiWith(
        (_) => <String, dynamic>{'ok': false, 'message': '没有勾选任何要清理的内容'},
      );

      expect(
        api.repairClean(orphans: true),
        throwsA(isA<ApiException>()),
      );
    });

    test('ok:true 时正常返回作业 id', () async {
      final api = apiWith(
        (request) => <String, dynamic>{'ok': true, 'job_id': 'job-7'},
      );

      expect(await api.repairScan(), 'job-7');
      expect(await api.repairClean(), 'job-7');
    });
  });
}
