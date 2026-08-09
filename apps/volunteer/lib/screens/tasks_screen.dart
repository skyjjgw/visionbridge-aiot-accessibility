import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api_client.dart';
import '../app_theme.dart';
import '../models.dart';

class TasksScreen extends StatefulWidget {
  const TasksScreen({super.key, required this.api, this.refreshRevision = 0});
  final ApiClient api;
  final int refreshRevision;
  @override
  State<TasksScreen> createState() => _TasksScreenState();
}

class _TasksScreenState extends State<TasksScreen> {
  bool mine = false;
  bool loading = true;
  String error = '';
  List<TaskItem> tasks = [];
  int loadRevision = 0;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant TasksScreen oldWidget) {
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
    try {
      final result =
          mine ? await widget.api.myTasks() : await widget.api.tasks();
      if (mounted && revision == loadRevision) setState(() => tasks = result);
    } on ApiException catch (exception) {
      if (mounted && revision == loadRevision) {
        setState(() => error = exception.message);
      }
    } catch (_) {
      if (mounted && revision == loadRevision && !silent) {
        setState(() => error = '公共任务暂时无法加载');
      }
    } finally {
      if (mounted && revision == loadRevision) {
        setState(() => loading = false);
      }
    }
  }

  Future<void> _openTask(TaskItem task) async {
    await showModalBottomSheet(
        context: context,
        isScrollControlled: true,
        showDragHandle: true,
        builder: (context) =>
            _TaskSheet(api: widget.api, task: task, mine: mine));
    await _load();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
          title: const Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('公共派单', style: TextStyle(fontWeight: FontWeight.w800)),
                Text('量力而行 · 安全优先',
                    style: TextStyle(fontSize: 10, color: Colors.blueGrey))
              ]),
          actions: [
            IconButton(
                onPressed: _load, icon: const Icon(Icons.refresh_rounded))
          ]),
      body: Column(children: [
        Padding(
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 10),
            child: SegmentedButton<bool>(
                segments: const [
                  ButtonSegment(
                      value: false,
                      label: Text('任务大厅'),
                      icon: Icon(Icons.public_rounded)),
                  ButtonSegment(
                      value: true,
                      label: Text('我的任务'),
                      icon: Icon(Icons.assignment_ind_outlined))
                ],
                selected: {
                  mine
                },
                onSelectionChanged: (value) {
                  setState(() => mine = value.first);
                  _load();
                })),
        if (error.isNotEmpty)
          Padding(
              padding: const EdgeInsets.all(12),
              child:
                  Text(error, style: const TextStyle(color: Colors.redAccent))),
        Expanded(
            child: loading
                ? const Center(child: CircularProgressIndicator())
                : tasks.isEmpty
                    ? _EmptyTasks(mine: mine)
                    : RefreshIndicator(
                        onRefresh: _load,
                        child: ListView.separated(
                            padding: const EdgeInsets.fromLTRB(16, 5, 16, 110),
                            itemCount: tasks.length,
                            separatorBuilder: (_, __) =>
                                const SizedBox(height: 10),
                            itemBuilder: (context, index) => _TaskCard(
                                api: widget.api,
                                task: tasks[index],
                                onTap: () => _openTask(tasks[index]))))),
      ]),
    );
  }
}

class _TaskCard extends StatelessWidget {
  const _TaskCard({required this.api, required this.task, required this.onTap});
  final ApiClient api;
  final TaskItem task;
  final VoidCallback onTap;
  @override
  Widget build(BuildContext context) => Card(
      child: InkWell(
          borderRadius: BorderRadius.circular(20),
          onTap: onTap,
          child: Padding(
              padding: const EdgeInsets.all(13),
              child:
                  Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                ClipRRect(
                    borderRadius: BorderRadius.circular(13),
                    child: SizedBox(
                        width: 94,
                        height: 94,
                        child: Image.network(api.resolveUrl(task.photoUrl),
                            fit: BoxFit.cover,
                            errorBuilder: (_, __, ___) => const ColoredBox(
                                color: Color(0xFFE8F0F1),
                                child: Icon(Icons.image_outlined,
                                    color: Colors.blueGrey))))),
                const SizedBox(width: 13),
                Expanded(
                    child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                      Row(children: [
                        _StatusBadge(
                            status: task.status, priority: task.priority),
                        const Spacer(),
                        const Icon(Icons.chevron_right_rounded,
                            color: Colors.blueGrey)
                      ]),
                      const SizedBox(height: 7),
                      Text(task.title,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                              fontSize: 15,
                              fontWeight: FontWeight.w800,
                              color: AppTheme.ink)),
                      const SizedBox(height: 5),
                      Text(task.description,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                              fontSize: 11,
                              height: 1.45,
                              color: Colors.blueGrey)),
                      const SizedBox(height: 7),
                      Row(children: [
                        const Icon(Icons.location_on_outlined,
                            size: 14, color: AppTheme.teal),
                        const SizedBox(width: 4),
                        Expanded(
                            child: Text(task.address,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                    fontSize: 10, color: Colors.blueGrey)))
                      ])
                    ]))
              ]))));
}

class _TaskSheet extends StatefulWidget {
  const _TaskSheet({required this.api, required this.task, required this.mine});
  final ApiClient api;
  final TaskItem task;
  final bool mine;
  @override
  State<_TaskSheet> createState() => _TaskSheetState();
}

class _TaskSheetState extends State<_TaskSheet> {
  bool busy = false;
  String error = '';
  Future<void> _claim() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('确认认领任务？'),
        content: const Text('认领后请尽快到场核实。若现场存在施工、车流或其他危险，请立即停止处理并在处置说明中反馈。'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('再考虑一下')),
          FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('确认认领')),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => busy = true);
    try {
      await widget.api.claimTask(widget.task.id);
      if (mounted) Navigator.pop(context);
    } on ApiException catch (e) {
      setState(() {
        error = e.statusCode == 409 ? '任务刚刚已被其他志愿者认领' : e.message;
        busy = false;
      });
    }
  }

  Future<void> _navigate() async {
    final task = widget.task;
    final uri = Uri.parse(
        'https://uri.amap.com/navigation?to=${task.lng},${task.lat},${Uri.encodeComponent(task.title)}&mode=walk&policy=1&src=visionbridge&coordinate=gaode&callnative=1');
    if (!await launchUrl(uri, mode: LaunchMode.externalApplication) &&
        mounted) {
      setState(() => error = '无法打开地图导航，请复制页面中的经纬度手动搜索');
    }
  }

  @override
  Widget build(BuildContext context) {
    final task = widget.task;
    final dangerous =
        task.categoryLabel.contains('施工') || task.categoryLabel.contains('坑洼');
    return SafeArea(
        child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 18),
            child: SingleChildScrollView(
                child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                  ClipRRect(
                      borderRadius: BorderRadius.circular(18),
                      child: AspectRatio(
                          aspectRatio: 16 / 8,
                          child: Image.network(
                              widget.api.resolveUrl(task.photoUrl),
                              fit: BoxFit.cover,
                              errorBuilder: (_, __, ___) => const ColoredBox(
                                  color: Color(0xFFE8F0F1),
                                  child: Icon(Icons.image_outlined))))),
                  const SizedBox(height: 16),
                  _StatusBadge(status: task.status, priority: task.priority),
                  const SizedBox(height: 10),
                  Text(task.title,
                      style: const TextStyle(
                          fontSize: 22,
                          fontWeight: FontWeight.w800,
                          color: AppTheme.ink)),
                  const SizedBox(height: 9),
                  Text(task.description, style: const TextStyle(height: 1.6)),
                  const SizedBox(height: 14),
                  ListTile(
                      contentPadding: EdgeInsets.zero,
                      leading: const Icon(Icons.location_on_outlined,
                          color: AppTheme.teal),
                      title: Text(task.address),
                      subtitle: Text(
                          '${task.lng.toStringAsFixed(6)}, ${task.lat.toStringAsFixed(6)}'),
                      trailing: IconButton.filledTonal(
                          tooltip: '步行导航',
                          onPressed: _navigate,
                          icon: const Icon(Icons.directions_walk_rounded))),
                  if (dangerous)
                    Container(
                        padding: const EdgeInsets.all(13),
                        decoration: BoxDecoration(
                            color: Colors.orange.withValues(alpha: .09),
                            borderRadius: BorderRadius.circular(13)),
                        child: const Row(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Icon(Icons.health_and_safety_outlined,
                                  color: Colors.deepOrange),
                              SizedBox(width: 9),
                              Expanded(
                                  child: Text(
                                      '此任务可能需要专业人员。志愿者应以现场提示、拍照复核和转交管理部门为主，不得冒险施工。',
                                      style: TextStyle(
                                          fontSize: 12,
                                          height: 1.5,
                                          color: Colors.deepOrange)))
                            ])),
                  if (task.reviewNote.isNotEmpty)
                    Padding(
                        padding: const EdgeInsets.only(top: 12),
                        child: Text('后台说明：${task.reviewNote}',
                            style: const TextStyle(color: Colors.deepOrange))),
                  if (error.isNotEmpty)
                    Padding(
                        padding: const EdgeInsets.only(top: 12),
                        child: Text(error,
                            style: const TextStyle(color: Colors.redAccent))),
                  const SizedBox(height: 18),
                  if (task.status == 'open')
                    FilledButton.icon(
                        onPressed: busy ? null : _claim,
                        icon: const Icon(Icons.volunteer_activism_outlined),
                        label: Text(busy ? '正在认领…' : '确认认领任务')),
                  if (widget.mine && task.status == 'claimed')
                    FilledButton.icon(
                        onPressed: busy
                            ? null
                            : () async {
                                final completed = await showDialog<bool>(
                                    context: context,
                                    builder: (context) => _CompleteTaskDialog(
                                        api: widget.api, task: task));
                                if (completed == true && context.mounted) {
                                  Navigator.pop(context);
                                }
                              },
                        icon: const Icon(Icons.add_a_photo_outlined),
                        label: const Text('提交处置结果')),
                  if (task.status == 'submitted')
                    const ListTile(
                        contentPadding: EdgeInsets.zero,
                        leading: Icon(Icons.hourglass_top_rounded,
                            color: AppTheme.warm),
                        title: Text('处理凭证已提交'),
                        subtitle: Text('等待后台审核员复核')),
                  if (task.status == 'verified')
                    const ListTile(
                        contentPadding: EdgeInsets.zero,
                        leading:
                            Icon(Icons.verified_rounded, color: AppTheme.teal),
                        title: Text('任务已完成闭环'))
                ]))));
  }
}

class _CompleteTaskDialog extends StatefulWidget {
  const _CompleteTaskDialog({required this.api, required this.task});
  final ApiClient api;
  final TaskItem task;
  @override
  State<_CompleteTaskDialog> createState() => _CompleteTaskDialogState();
}

class _CompleteTaskDialogState extends State<_CompleteTaskDialog> {
  final note = TextEditingController();
  final picker = ImagePicker();
  XFile? photo;
  Uint8List? bytes;
  bool busy = false;
  String error = '';
  @override
  void dispose() {
    note.dispose();
    super.dispose();
  }

  Future<void> _pick(ImageSource source) async {
    try {
      final selected = await picker.pickImage(
          source: source, imageQuality: 78, maxWidth: 1600);
      if (selected != null) {
        final data = await selected.readAsBytes();
        if (mounted) {
          setState(() {
            photo = selected;
            bytes = data;
            error = '';
          });
        }
      }
    } catch (_) {
      if (mounted) setState(() => error = '无法读取照片，请检查相机或相册权限');
    }
  }

  Future<void> _submit() async {
    if (bytes == null || photo == null || note.text.trim().length < 3) {
      setState(() => error = '请上传完成照片并填写处置说明');
      return;
    }
    setState(() => busy = true);
    try {
      await widget.api
          .completeTask(widget.task.id, note.text.trim(), bytes!, photo!.name);
      if (mounted) Navigator.pop(context, true);
    } on ApiException catch (e) {
      setState(() {
        error = e.message;
        busy = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
          title: const Text('提交处置结果'),
          content: SizedBox(
              width: 380,
              child: SingleChildScrollView(
                  child: Column(mainAxisSize: MainAxisSize.min, children: [
                InkWell(
                    onTap: () => _pick(ImageSource.camera),
                    child: Container(
                        height: 150,
                        decoration: BoxDecoration(
                            color: const Color(0xFFEAF1F2),
                            borderRadius: BorderRadius.circular(14)),
                        child: bytes == null
                            ? const Center(
                                child: Column(
                                    mainAxisSize: MainAxisSize.min,
                                    children: [
                                    Icon(Icons.add_a_photo_outlined,
                                        color: AppTheme.teal, size: 32),
                                    SizedBox(height: 6),
                                    Text('拍摄处理后照片')
                                  ]))
                            : ClipRRect(
                                borderRadius: BorderRadius.circular(14),
                                child: Image.memory(bytes!,
                                    width: double.infinity,
                                    fit: BoxFit.cover)))),
                Row(mainAxisAlignment: MainAxisAlignment.end, children: [
                  TextButton.icon(
                      onPressed: busy ? null : () => _pick(ImageSource.gallery),
                      icon: const Icon(Icons.photo_library_outlined),
                      label: const Text('从相册选择')),
                ]),
                const SizedBox(height: 12),
                TextField(
                    controller: note,
                    minLines: 3,
                    maxLines: 5,
                    decoration: const InputDecoration(
                        labelText: '处置说明', alignLabelWithHint: true)),
                if (error.isNotEmpty)
                  Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: Text(error,
                          style: const TextStyle(
                              color: Colors.redAccent, fontSize: 12)))
              ]))),
          actions: [
            TextButton(
                onPressed: busy ? null : () => Navigator.pop(context),
                child: const Text('取消')),
            FilledButton(
                onPressed: busy ? null : _submit,
                child: Text(busy ? '提交中…' : '提交复核'))
          ]);
}

class _StatusBadge extends StatelessWidget {
  const _StatusBadge({required this.status, required this.priority});
  final String status;
  final String priority;
  @override
  Widget build(BuildContext context) {
    final text = {
          'open': '待认领',
          'claimed': '已认领',
          'submitted': '待复核',
          'verified': '已完成',
          'cancelled': '已取消'
        }[status] ??
        status;
    final color = status == 'verified'
        ? AppTheme.teal
        : status == 'submitted'
            ? Colors.deepPurpleAccent
            : priority == 'urgent'
                ? Colors.redAccent
                : AppTheme.warm;
    return Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
            color: color.withValues(alpha: .1),
            borderRadius: BorderRadius.circular(7)),
        child: Text(text,
            style: TextStyle(
                color: color, fontSize: 10, fontWeight: FontWeight.w800)));
  }
}

class _EmptyTasks extends StatelessWidget {
  const _EmptyTasks({required this.mine});
  final bool mine;
  @override
  Widget build(BuildContext context) => Center(
      child: Padding(
          padding: const EdgeInsets.all(30),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Icon(Icons.route_outlined, size: 48, color: AppTheme.teal),
            const SizedBox(height: 12),
            Text(mine ? '还没有认领任务' : '附近暂时没有公开任务',
                style: const TextStyle(
                    fontWeight: FontWeight.w800, color: AppTheme.ink)),
            const SizedBox(height: 5),
            const Text('新的审核任务发布后会出现在这里',
                style: TextStyle(fontSize: 12, color: Colors.blueGrey))
          ])));
}
