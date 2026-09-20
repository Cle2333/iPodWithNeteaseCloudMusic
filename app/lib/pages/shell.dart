/// 主界面骨架：左侧导航 + 顶部状态条 + 内容区。
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../state/app_state.dart';
import '../widgets/status_bar.dart';
import 'download_page.dart';
import 'library_page.dart';
import 'playlist_page.dart';
import 'settings_page.dart';

class Shell extends StatefulWidget {
  const Shell({super.key});

  @override
  State<Shell> createState() => _ShellState();
}

class _ShellState extends State<Shell> {
  int _index = 0;

  static const List<({IconData icon, IconData selected, String label})> _tabs =
      <({IconData icon, IconData selected, String label})>[
        (
          icon: Icons.queue_music_outlined,
          selected: Icons.queue_music,
          label: '歌单',
        ),
        (icon: Icons.download_outlined, selected: Icons.download, label: '下载'),
        (
          icon: Icons.sd_storage_outlined,
          selected: Icons.sd_storage,
          label: 'iPod 音乐管理',
        ),
        (icon: Icons.settings_outlined, selected: Icons.settings, label: '设置'),
      ];

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();

    return Scaffold(
      body: Column(
        children: <Widget>[
          TopStatusBar(
            status: state.status,
            jobs: state.jobs,
            error: state.error,
            onJobsTap: () => setState(() => _index = 1),
            onRefresh: state.refreshStatus,
          ),
          Expanded(
            child: Row(
              children: <Widget>[
                NavigationRail(
                  selectedIndex: _index,
                  onDestinationSelected: (i) => setState(() => _index = i),
                  labelType: NavigationRailLabelType.all,
                  destinations: <NavigationRailDestination>[
                    for (final tab in _tabs)
                      NavigationRailDestination(
                        icon: Icon(tab.icon),
                        selectedIcon: Icon(tab.selected),
                        label: Text(tab.label),
                      ),
                  ],
                ),
                const VerticalDivider(width: 1),
                Expanded(child: _body()),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _body() {
    return switch (_index) {
      0 => const PlaylistPage(),
      1 => const DownloadPage(),
      2 => const LibraryPage(),
      _ => const SettingsPage(),
    };
  }
}
