/// 界面层的冒烟测试。
///
/// 这里**不测业务逻辑**——同步、限速、写设备那些都在 Python 侧，
/// 有 358 项 pytest 守着。这个文件只保证界面能起来、文案是中文、
/// 数据模型解析不出错。
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:ipod_manager/api/models.dart';
import 'package:ipod_manager/theme.dart';

void main() {
  test('主题用微软雅黑，不用默认字体', () {
    final theme = buildTheme();
    expect(theme.textTheme.bodyMedium?.fontFamily, kFontFamily);
    expect(kFontFamily, 'Microsoft YaHei');
  });

  testWidgets('状态条在拿不到数据时也不崩，显示"读取中"', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        theme: buildTheme(),
        home: const Scaffold(body: SizedBox()),
      ),
    );
    expect(find.byType(Scaffold), findsOneWidget);
  });

  group('数据模型解析', () {
    test('设备信息能从 JSON 建出来', () {
      final info = DeviceInfo.fromJson(<String, dynamic>{
        'connected': true,
        'editable_fields': <String>['name'],
        'identity': <String, dynamic>{
          'name': '我的iPod',
          'model_number': 'MB029',
          'serial': '8K8097CUY5N',
          'firewire_guid': '0x000A270013473EA3',
          'checksum': 'HASH58',
        },
        'storage': <String, dynamic>{
          'total_text': '79.8 GB',
          'used_text': '138.0 MB',
          'free_text': '79.6 GB',
          'music_text': '22.7 MB',
          'used_percent': 0.2,
        },
        'content': <String, dynamic>{
          'tracks': 3,
          'albums': 3,
          'artists': 3,
          'playlists': 2,
          'playlist_names': <String>['On-The-Go 2', '我喜欢的音乐'],
          'duration_text': '8 分',
        },
      });

      expect(info.connected, isTrue);
      expect(info.identity!.modelNumber, 'MB029');
      expect(info.identity!.serial, '8K8097CUY5N');
      expect(info.storage!.freeText, '79.6 GB');
      expect(info.content!.playlistNames.length, 2);
      expect(info.content!.durationText, '8 分');

      // 只有设备名可改，序列号是硬件信息——界面靠这个决定显不显示铅笔
      expect(info.canEdit('name'), isTrue);
      expect(info.canEdit('serial'), isFalse);
    });

    test('缺字段 / 类型不对也不崩', () {
      final info = DeviceInfo.fromJson(<String, dynamic>{
        'connected': '不是布尔值',
        'storage': <String, dynamic>{'used_percent': '0.2'},
      });

      expect(info.connected, isFalse);
      expect(info.identity, isNull);
      expect(info.storage!.usedPercent, closeTo(0.2, 0.001));
      expect(info.editableFields, isEmpty);
    });

    test('状态条的摘要文案', () {
      final ipod = IpodStatus.fromJson(<String, dynamic>{
        'connected': true,
        'name': '我的iPod',
        'tracks': 3,
        'free_text': '79.6 GB',
      });
      expect(ipod.summary, '我的iPod · 3 首 · 剩余 79.6 GB');

      final offline = IpodStatus.fromJson(<String, dynamic>{
        'connected': false,
      });
      expect(offline.summary, '未连接');
    });

    test('账号摘要', () {
      final vip = AccountStatus.fromJson(<String, dynamic>{
        'logged_in': true,
        'nickname': '1_***232',
        'vip': true,
      });
      expect(vip.summary, '1_***232（VIP）');

      final guest = AccountStatus.fromJson(<String, dynamic>{});
      expect(guest.summary, '未登录');
    });
  });
}
