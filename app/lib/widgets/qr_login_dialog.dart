/// 扫码登录对话框。
///
/// 二维码直接显示在窗口里（后端给的是 PNG 的 base64）——不用落盘、
/// 不用外部看图工具，用户扫完就完事。
///
/// 轮询在前端做：后端那边登录是**一个作业**，这里只是读它的状态。
/// 所以"取消"是真的取消了后端的活，不是只关掉这个弹窗。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../theme.dart';
import '../widgets/common.dart';

/// 打开登录对话框。返回 true 表示登录成功。
Future<bool> showLoginDialog(BuildContext context, ApiClient api) async {
  final result = await showDialog<bool>(
    context: context,
    barrierDismissible: false,
    builder: (_) => _LoginDialog(api: api),
  );
  return result ?? false;
}

class _LoginDialog extends StatefulWidget {
  const _LoginDialog({required this.api});

  final ApiClient api;

  @override
  State<_LoginDialog> createState() => _LoginDialogState();
}

class _LoginDialogState extends State<_LoginDialog> {
  LoginState _state = LoginState.idle;
  Uint8List? _image;
  String? _error;
  bool _starting = true;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    unawaited(_begin());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _begin() async {
    setState(() {
      _starting = true;
      _error = null;
    });
    try {
      final state = await widget.api.startLogin();
      if (!mounted) return;
      _apply(state);
      _timer = Timer.periodic(
        const Duration(milliseconds: 1200),
        (_) => unawaited(_poll()),
      );
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _starting = false;
        _error = e.message;
      });
    }
  }

  void _apply(LoginState state) {
    setState(() {
      _starting = false;
      _state = state;
      if (state.hasImage && _image == null) {
        try {
          _image = base64Decode(state.imageBase64);
        } catch (_) {
          _error = '二维码图片解不开';
        }
      }
    });
    if (state.succeeded) {
      _timer?.cancel();
      // 让用户看清"登录成功"再关，闪一下关掉会很突兀
      Future<void>.delayed(const Duration(milliseconds: 600), () {
        if (mounted) Navigator.of(context).pop(true);
      });
    }
    if (state.finished && !state.succeeded) {
      _timer?.cancel();
    }
  }

  Future<void> _poll() async {
    if (!mounted) return;
    try {
      final state = await widget.api.loginState();
      if (!mounted) return;
      _apply(state);
    } catch (_) {
      // 单次轮询失败不打断：网络抖一下不该让用户重新扫一次
    }
  }

  Future<void> _cancel() async {
    _timer?.cancel();
    try {
      await widget.api.cancelLogin();
    } catch (_) {
      // 取消失败也无所谓，关掉走人
    }
    if (mounted) Navigator.of(context).pop(false);
  }

  Future<void> _retry() async {
    _timer?.cancel();
    _image = null;
    await _begin();
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final state = _state;

    return AlertDialog(
      title: const Text('扫码登录网易云'),
      content: SizedBox(
        width: 380,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.center,
          children: <Widget>[
            if (_starting)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 40),
                child: Column(
                  children: <Widget>[
                    CircularProgressIndicator(),
                    SizedBox(height: 14),
                    Text('正在获取二维码…', style: TextStyle(fontSize: 13)),
                  ],
                ),
              )
            else if (_error != null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 20),
                child: Notice(
                  icon: Icons.error_outline,
                  title: '拿不到二维码',
                  text: _error!,
                  color: StatusColors.error,
                ),
              )
            else ...<Widget>[
              Container(
                width: 240,
                height: 240,
                decoration: BoxDecoration(
                  color: Colors.white,
                  border: Border.all(color: scheme.outlineVariant),
                  borderRadius: BorderRadius.circular(8),
                ),
                padding: const EdgeInsets.all(10),
                child: _image == null
                    ? const Center(child: Text('二维码已过期'))
                    : Image.memory(_image!, filterQuality: FilterQuality.none),
              ),
              const SizedBox(height: 14),
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: <Widget>[
                  Dot(switch (state.state) {
                    'confirmed' => StatusColors.ok,
                    'scanned' => StatusColors.warn,
                    'expired' || 'failed' => StatusColors.error,
                    _ => StatusColors.info,
                  }),
                  const SizedBox(width: 8),
                  Text(state.message, style: const TextStyle(fontSize: 13.5)),
                ],
              ),
              const SizedBox(height: 10),
              Text(
                '用网易云音乐 App 扫描上面的二维码，在手机上点确认。',
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontSize: 12,
                  color: scheme.onSurfaceVariant,
                  height: 1.5,
                ),
              ),
            ],
          ],
        ),
      ),
      actions: <Widget>[
        if (state.finished && !state.succeeded && _error == null)
          TextButton(onPressed: _retry, child: const Text('换一张')),
        if (_error != null)
          TextButton(onPressed: _retry, child: const Text('重试')),
        TextButton(onPressed: _cancel, child: const Text('取消')),
      ],
    );
  }
}
