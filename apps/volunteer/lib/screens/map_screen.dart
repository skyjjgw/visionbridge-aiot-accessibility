import 'package:flutter/material.dart';

import '../api_client.dart';
import '../app_theme.dart';
import '../location_service.dart';
import '../models.dart';
import '../widgets/map_surface.dart';

class MapScreen extends StatefulWidget {
  const MapScreen({
    super.key,
    required this.api,
    this.onReportAt,
    this.onOpenTasks,
    this.onTaskClaimed,
    this.refreshRevision = 0,
  });

  final ApiClient api;
  final ValueChanged<MapSelection>? onReportAt;
  final VoidCallback? onOpenTasks;
  final VoidCallback? onTaskClaimed;
  final int refreshRevision;

  @override
  State<MapScreen> createState() => _MapScreenState();
}

class _MapScreenState extends State<MapScreen> {
  PublicConfig config = const PublicConfig();
  List<Obstacle> obstacles = [];
  MapSelection? userLocation;
  MapSelection? selectedLocation;
  String filter = 'all';
  bool loading = true;
  bool locating = false;
  bool selecting = false;
  String error = '';
  String mapNotice = '';
  int loadRevision = 0;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant MapScreen oldWidget) {
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
    PublicConfig? nextConfig;
    List<Obstacle>? nextObstacles;
    final failures = <String>[];
    await Future.wait<void>([
      (() async {
        try {
          nextConfig = await widget.api.publicConfig();
        } catch (_) {
          failures.add('地图配置');
        }
      })(),
      (() async {
        try {
          nextObstacles = await widget.api.obstacles();
        } catch (_) {
          failures.add('障碍数据');
        }
      })(),
    ]);
    if (!mounted || revision != loadRevision) return;
    setState(() {
      if (nextConfig != null) config = nextConfig!;
      if (nextObstacles != null) obstacles = nextObstacles!;
      if (failures.isNotEmpty) error = '${failures.join('、')}暂时无法连接，可点击右上角重试';
      if (failures.isEmpty) error = '';
      loading = false;
    });
  }

  List<Obstacle> get visible => filter == 'all'
      ? obstacles
      : obstacles.where((item) => item.priority == filter).toList();

  Future<MapSelection?> _locate() async {
    setState(() {
      locating = true;
      error = '';
    });
    try {
      final next = await LocationService.acquireBestFix(address: '当前位置');
      if (mounted) {
        setState(() {
          userLocation = next;
          mapNotice = LocationService.accuracyHint(next.accuracy);
        });
      }
      return next;
    } on LocationAcquireException catch (exception) {
      if (mounted) setState(() => error = exception.message);
    } catch (_) {
      if (mounted) setState(() => error = '定位失败，请到开阔位置重试或直接在地图选点');
    } finally {
      if (mounted) setState(() => locating = false);
    }
    return null;
  }

  Future<void> _startSelecting() async {
    MapSelection? initial = userLocation;
    if (!selecting && initial == null) initial = await _locate();
    if (!mounted) return;
    setState(() {
      selecting = !selecting;
      if (selecting && initial != null) selectedLocation = initial;
      if (!selecting) selectedLocation = null;
    });
  }

  Future<void> _selectLocation(MapSelection selection) async {
    setState(() => selectedLocation = selection);
    final confirmed = await showModalBottomSheet<bool>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 20),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Icon(Icons.add_location_alt_rounded,
                color: AppTheme.teal, size: 38),
            const SizedBox(height: 8),
            const Text('在此位置上报障碍？',
                style: TextStyle(
                    fontSize: 19,
                    fontWeight: FontWeight.w800,
                    color: AppTheme.ink)),
            const SizedBox(height: 8),
            Text(
              selection.address.isEmpty ? '地图手动标注点' : selection.address,
              textAlign: TextAlign.center,
              style: const TextStyle(color: Colors.blueGrey, height: 1.5),
            ),
            const SizedBox(height: 4),
            Text(
                '${selection.lng.toStringAsFixed(6)}, ${selection.lat.toStringAsFixed(6)}',
                style: const TextStyle(fontSize: 11, color: Colors.blueGrey)),
            const SizedBox(height: 16),
            Row(children: [
              Expanded(
                child: OutlinedButton(
                    onPressed: () => Navigator.pop(context, false),
                    child: const Text('继续调整')),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: FilledButton(
                    onPressed: () => Navigator.pop(context, true),
                    child: const Text('去填写上报')),
              ),
            ]),
          ]),
        ),
      ),
    );
    if (confirmed == true && mounted) {
      setState(() => selecting = false);
      widget.onReportAt?.call(selection);
    }
  }

  Future<void> _showObstacle(Obstacle obstacle) async {
    final action = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              ClipRRect(
                borderRadius: BorderRadius.circular(16),
                child: AspectRatio(
                  aspectRatio: 16 / 8,
                  child: Image.network(
                    widget.api.resolveUrl(obstacle.photoUrl),
                    fit: BoxFit.cover,
                    errorBuilder: (_, __, ___) => const ColoredBox(
                      color: Color(0xFFE7EFF0),
                      child: Icon(Icons.image_not_supported_outlined),
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 16),
              Row(children: [
                _PriorityBadge(priority: obstacle.priority),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(obstacle.categoryLabel,
                      style: const TextStyle(
                          fontSize: 20,
                          fontWeight: FontWeight.w800,
                          color: AppTheme.ink)),
                ),
              ]),
              const SizedBox(height: 10),
              Text(obstacle.description, style: const TextStyle(height: 1.6)),
              const SizedBox(height: 12),
              Row(children: [
                const Icon(Icons.location_on_outlined,
                    size: 18, color: AppTheme.teal),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(obstacle.address,
                      style: const TextStyle(color: Colors.blueGrey)),
                ),
              ]),
              const SizedBox(height: 18),
              if (obstacle.isClaimable)
                FilledButton.icon(
                  onPressed: () => Navigator.pop(context, 'claim'),
                  icon: const Icon(Icons.volunteer_activism_outlined),
                  label: const Text('立即接单'),
                )
              else
                FilledButton.tonalIcon(
                  onPressed: () => Navigator.pop(context, 'tasks'),
                  icon: const Icon(Icons.route_outlined),
                  label: Text(obstacle.taskStatus == 'claimed'
                      ? '该任务已被认领'
                      : obstacle.taskStatus == 'submitted'
                          ? '任务已提交，等待复核'
                          : '前往任务大厅'),
                ),
            ],
          ),
        ),
      ),
    );
    if (!mounted) return;
    if (action == 'tasks') {
      widget.onOpenTasks?.call();
    } else if (action == 'claim') {
      await _claimObstacleTask(obstacle);
    }
  }

  Future<void> _claimObstacleTask(Obstacle obstacle) async {
    final taskId = obstacle.taskId;
    if (taskId == null) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        icon: const Icon(Icons.health_and_safety_outlined,
            color: AppTheme.teal, size: 38),
        title: const Text('确认接单'),
        content: const Text('请先确认现场安全且自己具备处置能力。施工、路面破损等高风险问题不要擅自处理。'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('再考虑一下')),
          FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('确认接单')),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    try {
      await widget.api.claimTask(taskId);
      await _load(silent: true);
      widget.onTaskClaimed?.call();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('接单成功，可在“我的任务”中查看并提交处置结果')),
        );
      }
    } on ApiException catch (exception) {
      await _load(silent: true);
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(exception.message)));
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('接单失败，请检查网络后重试')));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final countText =
        visible.isEmpty ? '暂无审核入库的活动障碍' : '${visible.length} 处活动障碍';
    return Scaffold(
      appBar: AppBar(
        title: const Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('障碍物地图', style: TextStyle(fontWeight: FontWeight.w800)),
            Text('全域态势 · 审核入库数据',
                style: TextStyle(fontSize: 10, color: Colors.blueGrey)),
          ],
        ),
        actions: [
          IconButton(onPressed: _load, icon: const Icon(Icons.refresh_rounded)),
        ],
      ),
      body: Column(children: [
        SizedBox(
          height: 48,
          child: ListView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 5),
            children: [
              for (final item in const [
                ('all', '全部'),
                ('urgent', '紧急'),
                ('normal', '普通'),
                ('low', '低风险')
              ])
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    label: Text(item.$2),
                    selected: filter == item.$1,
                    onSelected: (_) => setState(() => filter = item.$1),
                  ),
                ),
            ],
          ),
        ),
        if (error.isNotEmpty)
          Container(
            width: double.infinity,
            color: Colors.redAccent.withValues(alpha: .08),
            padding: const EdgeInsets.all(10),
            child: Text(error,
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.redAccent, fontSize: 12)),
          ),
        Expanded(
          child: Stack(children: [
            Positioned.fill(
              child: MapSurface(
                config: config,
                obstacles: visible,
                remotePageUrl:
                    widget.api.resolveUrl('/volunteer/assets/assets/amap.html'),
                selectionEnabled: selecting,
                selection: selectedLocation,
                userLocation: userLocation,
                onLocationSelected: _selectLocation,
                onMapError: (value) {
                  if (mounted) setState(() => mapNotice = value);
                },
                onObstacleTap: (item) => _showObstacle(item),
              ),
            ),
            Positioned(
              left: 16,
              right: 16,
              top: 12,
              child: Card(
                child: Padding(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                  child: Row(children: [
                    Container(
                      width: 38,
                      height: 38,
                      decoration: BoxDecoration(
                        color: AppTheme.teal.withValues(alpha: .1),
                        borderRadius: BorderRadius.circular(11),
                      ),
                      child: Icon(
                          selecting
                              ? Icons.add_location_alt
                              : Icons.map_outlined,
                          color: AppTheme.teal),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(selecting ? '请点击地图标注障碍位置' : countText,
                              style: const TextStyle(
                                  fontWeight: FontWeight.w800,
                                  color: AppTheme.ink)),
                          Text(
                            selecting
                                ? '选点后确认，即可自动带入上报页面'
                                : visible.isEmpty
                                    ? '点击右下角标记按钮发起第一条上报'
                                    : '点击标记查看照片与详情',
                            style: const TextStyle(
                                fontSize: 11, color: Colors.blueGrey),
                          ),
                        ],
                      ),
                    ),
                    if (!selecting) const _LiveDot(),
                  ]),
                ),
              ),
            ),
            Positioned(
              right: 16,
              bottom: 78,
              child: Column(children: [
                FloatingActionButton.small(
                  heroTag: 'locate',
                  onPressed: locating ? null : _locate,
                  tooltip: '定位到当前位置',
                  child: locating
                      ? const Padding(
                          padding: EdgeInsets.all(10),
                          child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.my_location_rounded),
                ),
                const SizedBox(height: 10),
                FloatingActionButton.extended(
                  heroTag: 'report-at',
                  onPressed: _startSelecting,
                  backgroundColor: selecting ? Colors.redAccent : AppTheme.teal,
                  foregroundColor: Colors.white,
                  icon: Icon(selecting ? Icons.close : Icons.add_location_alt),
                  label: Text(selecting ? '取消标注' : '地图标注'),
                ),
              ]),
            ),
            if (loading)
              const Positioned.fill(
                child: IgnorePointer(
                  child: Center(child: CircularProgressIndicator()),
                ),
              ),
            if (mapNotice.isNotEmpty && !selecting)
              Positioned(
                left: 16,
                bottom: 18,
                child: Text(mapNotice,
                    style:
                        const TextStyle(fontSize: 9, color: Colors.blueGrey)),
              ),
          ]),
        ),
      ]),
    );
  }
}

class _PriorityBadge extends StatelessWidget {
  const _PriorityBadge({required this.priority});
  final String priority;
  @override
  Widget build(BuildContext context) {
    final label =
        {'urgent': '紧急', 'normal': '普通', 'low': '低风险'}[priority] ?? '普通';
    final color = priority == 'urgent'
        ? Colors.redAccent
        : priority == 'low'
            ? AppTheme.teal
            : AppTheme.warm;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
          color: color.withValues(alpha: .1),
          borderRadius: BorderRadius.circular(7)),
      child: Text(label,
          style: TextStyle(
              fontSize: 11, color: color, fontWeight: FontWeight.w700)),
    );
  }
}

class _LiveDot extends StatelessWidget {
  const _LiveDot();
  @override
  Widget build(BuildContext context) => Container(
        width: 9,
        height: 9,
        decoration: const BoxDecoration(
          shape: BoxShape.circle,
          color: Color(0xFF2BB79B),
          boxShadow: [
            BoxShadow(color: Color(0x552BB79B), blurRadius: 8, spreadRadius: 3)
          ],
        ),
      );
}
