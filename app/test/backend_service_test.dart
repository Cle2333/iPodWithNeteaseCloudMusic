/// BackendService 的通知机制。
///
/// 这个文件守的是一个**测试很难自然跑到**的坑：
///
/// 为了让"退出应用时 stop() 里的日志不再通知已销毁的对象"，所有通知都
/// 改走了 `_notify()` 包装。如果哪天批量替换脚本把 `_notify()` **自己内部**
/// 那行 `notifyListeners()` 也换成 `_notify()`，就成了自递归——一路爆栈。
/// 测试里没跑到这条路径的话是全绿的，真机上表现为"应用起不来、后端没被拉起"。
library;

import 'package:flutter_test/flutter_test.dart';

import 'package:ipod_manager/services/backend.dart';

void main() {
  group('状态通知', () {
    test('发通知时监听者能收到（而不是自己调自己爆栈）', () {
      final backend = BackendService(port: 8765);
      var notified = 0;
      backend.addListener(() => notified++);

      backend.clearLog();

      expect(notified, 1, reason: '通知没发出去，或者 _notify 在自递归');
      backend.dispose();
    });

    test('★ 销毁之后不再发通知', () {
      // dispose() 会去停子进程，那个过程里还会写日志发通知。
      // 在已销毁的 ChangeNotifier 上 notifyListeners() 会直接抛断言，
      // 所以 _notify 必须把它挡掉。
      final backend = BackendService(port: 8765);
      var notified = 0;
      backend.addListener(() => notified++);

      backend.dispose();

      // dispose 之后再调：不该抛，也不该通知
      expect(() => backend.clearLog(), returnsNormally);
      expect(notified, 0);
    });
  });
}
