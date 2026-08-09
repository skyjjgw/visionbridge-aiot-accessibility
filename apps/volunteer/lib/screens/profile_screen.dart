import 'package:flutter/material.dart';

import '../api_client.dart';
import '../app_theme.dart';
import '../models.dart';

class ProfileScreen extends StatefulWidget {
  const ProfileScreen(
      {super.key,
      required this.api,
      required this.onLogout,
      this.refreshRevision = 0});
  final ApiClient api;
  final Future<void> Function() onLogout;
  final int refreshRevision;
  @override
  State<ProfileScreen> createState() => _ProfileScreenState();
}

class _ProfileScreenState extends State<ProfileScreen> {
  UserProfile? user;
  List<ReportItem> reports = [];
  List<TaskItem> tasks = [];
  bool loading = true;
  String error = '';
  int loadRevision = 0;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant ProfileScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.refreshRevision != widget.refreshRevision) {
      _load(silent: true);
    }
  }

  Future<void> _load({bool silent = false}) async {
    final revision = ++loadRevision;
    setState(() {
      if (!silent) loading = true;
      if (!silent) error = '';
    });
    final failures = <String>[];
    Future<T?> safe<T>(Future<T> future, String label) async {
      try {
        return await future;
      } catch (_) {
        failures.add(label);
        return null;
      }
    }

    try {
      final values = await Future.wait<Object?>([
        safe(widget.api.me(), '账号'),
        safe(widget.api.myReports(), '上报'),
        safe(widget.api.myTasks(), '任务'),
      ]);
      if (mounted && revision == loadRevision) {
        setState(() {
          if (values[0] != null) user = values[0] as UserProfile;
          if (values[1] != null) reports = values[1] as List<ReportItem>;
          if (values[2] != null) tasks = values[2] as List<TaskItem>;
          error =
              failures.isEmpty ? '' : '${failures.join('、')}数据暂时无法加载，其他内容已保留';
        });
      }
    } finally {
      if (mounted && revision == loadRevision) {
        setState(() => loading = false);
      }
    }
  }

  Future<void> _deleteReport(ReportItem report) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('删除这条上报？'),
        content: const Text('删除后照片和记录将被移除，且无法恢复。只有待审核或已驳回的本人上报可以删除。'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('取消')),
          FilledButton(
              style: FilledButton.styleFrom(backgroundColor: Colors.redAccent),
              onPressed: () => Navigator.pop(context, true),
              child: const Text('确认删除')),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    try {
      await widget.api.deleteReport(report.id);
      await _load();
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('上报已删除')));
      }
    } on ApiException catch (exception) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(exception.message)));
      }
    }
  }

  void _showReport(ReportItem report) {
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 20),
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (report.photoUrl.isNotEmpty)
                  ClipRRect(
                    borderRadius: BorderRadius.circular(16),
                    child: AspectRatio(
                      aspectRatio: 16 / 9,
                      child: Image.network(
                        widget.api.resolveUrl(report.photoUrl),
                        fit: BoxFit.cover,
                        errorBuilder: (_, __, ___) => const ColoredBox(
                            color: Color(0xFFE8F0F1),
                            child: Icon(Icons.image_not_supported_outlined)),
                      ),
                    ),
                  ),
                const SizedBox(height: 14),
                Row(children: [
                  _ReportStatus(status: report.status),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(report.categoryLabel,
                        style: const TextStyle(
                            fontSize: 20,
                            fontWeight: FontWeight.w800,
                            color: AppTheme.ink)),
                  ),
                  Text(_statusLabel(report.status),
                      style: TextStyle(
                          color: _statusColor(report.status),
                          fontWeight: FontWeight.w700)),
                ]),
                const SizedBox(height: 12),
                Text(report.description, style: const TextStyle(height: 1.6)),
                if (report.cleanupReasonLabel.isNotEmpty)
                  ListTile(
                    contentPadding: EdgeInsets.zero,
                    leading: const Icon(Icons.health_and_safety_outlined,
                        color: AppTheme.warm),
                    title: Text(report.cleanupReasonLabel),
                  ),
                if (report.address.isNotEmpty)
                  ListTile(
                    contentPadding: EdgeInsets.zero,
                    leading: const Icon(Icons.location_on_outlined,
                        color: AppTheme.teal),
                    title: Text(report.address),
                    subtitle: report.lat == 0 && report.lng == 0
                        ? null
                        : Text(
                            '${report.lng.toStringAsFixed(6)}, ${report.lat.toStringAsFixed(6)}'),
                  ),
                if (report.reviewNote.isNotEmpty)
                  Container(
                    width: double.infinity,
                    padding: const EdgeInsets.all(13),
                    decoration: BoxDecoration(
                        color: Colors.orange.withValues(alpha: .08),
                        borderRadius: BorderRadius.circular(13)),
                    child: Text('审核说明：${report.reviewNote}',
                        style: const TextStyle(
                            color: Colors.deepOrange, height: 1.5)),
                  ),
                if (report.canDelete) ...[
                  const SizedBox(height: 12),
                  SizedBox(
                    width: double.infinity,
                    child: OutlinedButton.icon(
                      style: OutlinedButton.styleFrom(
                          foregroundColor: Colors.redAccent),
                      onPressed: () {
                        Navigator.pop(context);
                        _deleteReport(report);
                      },
                      icon: const Icon(Icons.delete_outline_rounded),
                      label: const Text('删除这条上报'),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final pending = reports.where((item) => item.status == 'pending').length;
    final completed = tasks.where((item) => item.status == 'verified').length;
    return Scaffold(
      appBar: AppBar(
          title:
              const Text('我的', style: TextStyle(fontWeight: FontWeight.w800)),
          actions: [
            IconButton(
                onPressed: _load, icon: const Icon(Icons.refresh_rounded))
          ]),
      body: loading
          ? const Center(child: CircularProgressIndicator())
          : RefreshIndicator(
              onRefresh: _load,
              child: ListView(
                  padding: const EdgeInsets.fromLTRB(16, 4, 16, 110),
                  children: [
                    Card(
                        child: Padding(
                            padding: const EdgeInsets.all(18),
                            child: Row(children: [
                              Container(
                                  width: 62,
                                  height: 62,
                                  alignment: Alignment.center,
                                  decoration: BoxDecoration(
                                      gradient: const LinearGradient(colors: [
                                        AppTheme.teal,
                                        AppTheme.cyan
                                      ]),
                                      borderRadius: BorderRadius.circular(20)),
                                  child: Text(
                                      (user?.displayName ?? '志')
                                          .characters
                                          .first,
                                      style: const TextStyle(
                                          color: Colors.white,
                                          fontSize: 25,
                                          fontWeight: FontWeight.w800))),
                              const SizedBox(width: 14),
                              Expanded(
                                  child: Column(
                                      crossAxisAlignment:
                                          CrossAxisAlignment.start,
                                      children: [
                                    Text(user?.displayName ?? '视桥志愿者',
                                        style: const TextStyle(
                                            fontSize: 19,
                                            fontWeight: FontWeight.w800,
                                            color: AppTheme.ink)),
                                    const SizedBox(height: 4),
                                    Text(user?.email ?? '',
                                        style: const TextStyle(
                                            fontSize: 12,
                                            color: Colors.blueGrey)),
                                    const SizedBox(height: 7),
                                    const Row(children: [
                                      Icon(Icons.verified_user_outlined,
                                          size: 15, color: AppTheme.teal),
                                      SizedBox(width: 4),
                                      Text('邮箱已验证',
                                          style: TextStyle(
                                              fontSize: 11,
                                              color: AppTheme.teal,
                                              fontWeight: FontWeight.w700))
                                    ])
                                  ]))
                            ]))),
                    const SizedBox(height: 12),
                    Row(children: [
                      Expanded(
                          child: _StatCard(
                              value: '${reports.length}',
                              label: '累计上报',
                              icon: Icons.add_location_alt_outlined)),
                      const SizedBox(width: 10),
                      Expanded(
                          child: _StatCard(
                              value: '$pending',
                              label: '等待审核',
                              icon: Icons.hourglass_top_rounded)),
                      const SizedBox(width: 10),
                      Expanded(
                          child: _StatCard(
                              value: '$completed',
                              label: '完成任务',
                              icon: Icons.task_alt_rounded))
                    ]),
                    const SizedBox(height: 22),
                    const _Heading('我的上报'),
                    if (reports.isEmpty)
                      const _EmptyLine('还没有上报记录')
                    else
                      ...reports.map((report) => Card(
                          child: ListTile(
                              onTap: () => _showReport(report),
                              leading: _ReportStatus(status: report.status),
                              title: Text(report.categoryLabel,
                                  style: const TextStyle(
                                      fontWeight: FontWeight.w700)),
                              subtitle: Text(
                                  report.reviewNote.isEmpty
                                      ? report.description
                                      : '审核说明：${report.reviewNote}',
                                  maxLines: 2,
                                  overflow: TextOverflow.ellipsis),
                              trailing: Row(
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    if (report.canDelete)
                                      IconButton(
                                          tooltip: '删除上报',
                                          onPressed: () =>
                                              _deleteReport(report),
                                          icon: const Icon(
                                              Icons.delete_outline_rounded,
                                              color: Colors.redAccent)),
                                    const Icon(Icons.chevron_right_rounded,
                                        color: Colors.blueGrey)
                                  ])))),
                    const SizedBox(height: 20),
                    const _Heading('安全与关于'),
                    Card(
                        child: Column(children: [
                      const ListTile(
                          leading:
                              Icon(Icons.map_outlined, color: AppTheme.teal),
                          title: Text('地图与定位'),
                          subtitle:
                              Text('高德地图 JS API · GCJ-02\n审图号由地图加载后动态显示')),
                      const Divider(height: 1),
                      const ListTile(
                          leading: Icon(Icons.health_and_safety_outlined,
                              color: AppTheme.warm),
                          title: Text('志愿服务安全'),
                          subtitle: Text('施工、坑洼和固定设施请转交专业人员')),
                      const Divider(height: 1),
                      ListTile(
                          leading: const Icon(Icons.logout_rounded,
                              color: Colors.redAccent),
                          title: const Text('退出登录'),
                          onTap: () async {
                            final confirm = await showDialog<bool>(
                                context: context,
                                builder: (context) => AlertDialog(
                                        title: const Text('退出登录？'),
                                        content: const Text('本机登录令牌将被清除。'),
                                        actions: [
                                          TextButton(
                                              onPressed: () =>
                                                  Navigator.pop(context, false),
                                              child: const Text('取消')),
                                          FilledButton(
                                              onPressed: () =>
                                                  Navigator.pop(context, true),
                                              child: const Text('退出'))
                                        ]));
                            if (confirm == true) await widget.onLogout();
                          })
                    ])),
                    if (error.isNotEmpty)
                      Padding(
                          padding: const EdgeInsets.all(12),
                          child: Text(error,
                              style: const TextStyle(color: Colors.redAccent))),
                  ])),
    );
  }

  static String _statusLabel(String value) =>
      {'pending': '待审核', 'approved': '已通过', 'rejected': '已驳回'}[value] ?? value;
  static Color _statusColor(String value) => value == 'approved'
      ? AppTheme.teal
      : value == 'rejected'
          ? Colors.redAccent
          : AppTheme.warm;
}

class _StatCard extends StatelessWidget {
  const _StatCard(
      {required this.value, required this.label, required this.icon});
  final String value;
  final String label;
  final IconData icon;
  @override
  Widget build(BuildContext context) => Card(
      child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 15, horizontal: 8),
          child: Column(children: [
            Icon(icon, color: AppTheme.teal, size: 20),
            const SizedBox(height: 6),
            Text(value,
                style: const TextStyle(
                    fontSize: 20,
                    fontWeight: FontWeight.w800,
                    color: AppTheme.ink)),
            Text(label,
                style: const TextStyle(fontSize: 10, color: Colors.blueGrey))
          ])));
}

class _Heading extends StatelessWidget {
  const _Heading(this.text);
  final String text;
  @override
  Widget build(BuildContext context) => Padding(
      padding: const EdgeInsets.only(left: 4, bottom: 8),
      child: Text(text,
          style: const TextStyle(
              fontSize: 16, fontWeight: FontWeight.w800, color: AppTheme.ink)));
}

class _EmptyLine extends StatelessWidget {
  const _EmptyLine(this.text);
  final String text;
  @override
  Widget build(BuildContext context) => Card(
      child: Padding(
          padding: const EdgeInsets.all(24),
          child: Center(
              child:
                  Text(text, style: const TextStyle(color: Colors.blueGrey)))));
}

class _ReportStatus extends StatelessWidget {
  const _ReportStatus({required this.status});
  final String status;
  @override
  Widget build(BuildContext context) {
    final icon = status == 'approved'
        ? Icons.check_rounded
        : status == 'rejected'
            ? Icons.close_rounded
            : Icons.schedule_rounded;
    final color = _ProfileScreenState._statusColor(status);
    return Container(
        width: 38,
        height: 38,
        decoration: BoxDecoration(
            color: color.withValues(alpha: .1),
            borderRadius: BorderRadius.circular(11)),
        child: Icon(icon, color: color, size: 20));
  }
}
