import 'dart:async';

import 'package:flutter/material.dart';

import '../api_client.dart';
import '../app_theme.dart';
import '../models.dart';
import 'map_screen.dart';
import 'profile_screen.dart';
import 'report_screen.dart';
import 'tasks_screen.dart';

class HomeShell extends StatefulWidget {
  const HomeShell({super.key, required this.api, required this.onLogout});
  final ApiClient api;
  final Future<void> Function() onLogout;
  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> with WidgetsBindingObserver {
  int index = 0;
  MapSelection? reportLocation;
  int mapRevision = 0;
  int taskRevision = 0;
  int profileRevision = 0;
  Timer? refreshTimer;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    refreshTimer = Timer.periodic(const Duration(seconds: 6), (_) {
      if (!mounted) return;
      _refreshActivePage();
    });
  }

  void _refreshActivePage() {
    if (index == 1) return;
    setState(() {
      if (index == 0) mapRevision++;
      if (index == 2) taskRevision++;
      if (index == 3) profileRevision++;
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _refreshActivePage();
  }

  @override
  void dispose() {
    refreshTimer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final pages = [
      MapScreen(
        api: widget.api,
        refreshRevision: mapRevision,
        onReportAt: (selection) => setState(() {
          reportLocation = selection;
          index = 1;
        }),
        onOpenTasks: () => setState(() {
          taskRevision++;
          index = 2;
        }),
        onTaskClaimed: () => setState(() => taskRevision++),
      ),
      ReportScreen(
          api: widget.api,
          initialLocation: reportLocation,
          onSubmitted: () => setState(() {
                reportLocation = null;
                mapRevision++;
                profileRevision++;
                index = 3;
              })),
      TasksScreen(api: widget.api, refreshRevision: taskRevision),
      ProfileScreen(
          api: widget.api,
          onLogout: widget.onLogout,
          refreshRevision: profileRevision),
    ];
    return Scaffold(
      body: IndexedStack(index: index, children: pages),
      bottomNavigationBar: NavigationBar(
        selectedIndex: index,
        onDestinationSelected: (value) => setState(() {
          if (value == 0) mapRevision++;
          if (value == 2) taskRevision++;
          if (value == 3) profileRevision++;
          index = value;
        }),
        indicatorColor: AppTheme.teal.withValues(alpha: .12),
        destinations: const [
          NavigationDestination(
              icon: Icon(Icons.map_outlined),
              selectedIcon: Icon(Icons.map_rounded),
              label: '地图'),
          NavigationDestination(
              icon: Icon(Icons.add_a_photo_outlined),
              selectedIcon: Icon(Icons.add_a_photo_rounded),
              label: '上报'),
          NavigationDestination(
              icon: Icon(Icons.route_outlined),
              selectedIcon: Icon(Icons.route_rounded),
              label: '任务'),
          NavigationDestination(
              icon: Icon(Icons.person_outline_rounded),
              selectedIcon: Icon(Icons.person_rounded),
              label: '我的'),
        ],
      ),
    );
  }
}
