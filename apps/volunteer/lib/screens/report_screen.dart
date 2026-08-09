import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';

import '../api_client.dart';
import '../app_theme.dart';
import '../location_service.dart';
import '../models.dart';
import '../widgets/map_surface.dart';

class ReportScreen extends StatefulWidget {
  const ReportScreen(
      {super.key, required this.api, this.onSubmitted, this.initialLocation});
  final ApiClient api;
  final VoidCallback? onSubmitted;
  final MapSelection? initialLocation;

  @override
  State<ReportScreen> createState() => _ReportScreenState();
}

class _ReportScreenState extends State<ReportScreen> {
  final picker = ImagePicker();
  final description = TextEditingController();
  final address = TextEditingController();
  String category = 'temporary_obstacle';
  String cleanupReason = 'unable_now';
  XFile? photo;
  Uint8List? photoBytes;
  double? lat;
  double? lng;
  double? accuracy;
  String locationSource = '';
  bool locating = false;
  bool submitting = false;
  String error = '';

  static const categories = {
    'temporary_obstacle': ('临时杂物/堆放', Icons.inventory_2_outlined),
    'shop_step': ('店铺台阶/固定高差', Icons.stairs_outlined),
    'construction': ('临时施工', Icons.construction_outlined),
    'road_damage': ('路面坑洼/破损', Icons.warning_amber_rounded),
    'vehicle': ('车辆占用', Icons.directions_car_outlined),
    'other': ('其他障碍', Icons.more_horiz_rounded),
  };
  static const reasons = {
    'unable_now': '当时不方便清理',
    'fixed_barrier': '固定障碍无法移动',
    'unsafe_to_clear': '不具备安全处理条件',
  };

  @override
  void initState() {
    super.initState();
    _applySelection(widget.initialLocation);
  }

  @override
  void didUpdateWidget(covariant ReportScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    final next = widget.initialLocation;
    final previous = oldWidget.initialLocation;
    if (next != null &&
        (previous == null ||
            previous.lat != next.lat ||
            previous.lng != next.lng)) {
      _applySelection(next);
    }
  }

  void _applySelection(MapSelection? selection) {
    if (selection == null) return;
    lat = selection.lat;
    lng = selection.lng;
    accuracy = selection.accuracy;
    locationSource = selection.source;
    if (selection.address.trim().isNotEmpty) {
      address.text = selection.address.trim();
    }
  }

  @override
  void dispose() {
    description.dispose();
    address.dispose();
    super.dispose();
  }

  Future<void> _pick(ImageSource source) async {
    try {
      final selected = await picker.pickImage(
          source: source, imageQuality: 78, maxWidth: 1600);
      if (selected == null) return;
      final bytes = await selected.readAsBytes();
      if (!mounted) return;
      setState(() {
        photo = selected;
        photoBytes = bytes;
        error = '';
      });
    } catch (_) {
      setState(() => error = '无法读取图片，请检查相机或相册权限');
    }
  }

  Future<void> _locate() async {
    setState(() {
      locating = true;
      error = '';
    });
    try {
      final selection =
          await LocationService.acquireBestFix(address: '当前位置（请补充附近道路或门牌）');
      if (!mounted) return;
      setState(() {
        _applySelection(selection);
        error = selection.accuracy != null && selection.accuracy! > 50
            ? LocationService.accuracyHint(selection.accuracy)
            : '';
      });
    } on LocationAcquireException catch (exception) {
      setState(() => error = exception.message);
    } catch (_) {
      setState(() => error = '定位失败，请移动到开阔位置后重试');
    } finally {
      if (mounted) setState(() => locating = false);
    }
  }

  Future<void> _pickOnMap() async {
    setState(() {
      locating = true;
      error = '';
    });
    try {
      final config = await widget.api.publicConfig();
      if (!mounted) return;
      final initial = lat == null || lng == null
          ? MapSelection(
              lat: config.defaultCenter[1],
              lng: config.defaultCenter[0],
              address: address.text.trim(),
              source: 'map')
          : MapSelection(
              lat: lat!,
              lng: lng!,
              address: address.text.trim(),
              source: locationSource.isEmpty ? 'map' : locationSource,
              accuracy: accuracy);
      final selected = await Navigator.of(context).push<MapSelection>(
        MaterialPageRoute(
          fullscreenDialog: true,
          builder: (context) => _LocationPickerPage(
              api: widget.api, config: config, initial: initial),
        ),
      );
      if (selected != null && mounted) {
        setState(() {
          _applySelection(selected);
          error = '';
        });
      }
    } on ApiException catch (exception) {
      if (mounted) setState(() => error = exception.message);
    } catch (_) {
      if (mounted) setState(() => error = '地图配置加载失败，请稍后重试');
    } finally {
      if (mounted) setState(() => locating = false);
    }
  }

  void _selectCategory(String value) {
    setState(() {
      category = value;
      if (value == 'shop_step') {
        cleanupReason = 'fixed_barrier';
      } else if (value == 'construction' || value == 'road_damage') {
        cleanupReason = 'unsafe_to_clear';
      } else if (cleanupReason != 'unable_now') {
        cleanupReason = 'unable_now';
      }
    });
  }

  Future<void> _submit() async {
    if (photoBytes == null || photo == null) {
      setState(() => error = '请先拍摄或选择一张障碍物照片');
      return;
    }
    if (description.text.trim().length < 5) {
      setState(() => error = '请至少用 5 个字描述现场情况');
      return;
    }
    if (lat == null || lng == null) {
      setState(() => error = '请先获取障碍物位置');
      return;
    }
    if (address.text.trim().length < 2) {
      setState(() => error = '请补充附近道路、门牌或店铺名称，方便审核和处置');
      return;
    }
    setState(() {
      submitting = true;
      error = '';
    });
    try {
      await widget.api.createReport(
          category: category,
          cleanupReason: cleanupReason,
          description: description.text.trim(),
          address: address.text.trim(),
          lat: lat!,
          lng: lng!,
          photoBytes: photoBytes!,
          photoName: photo!.name);
      if (!mounted) return;
      await showDialog(
          context: context,
          builder: (context) => AlertDialog(
                  icon: const Icon(Icons.task_alt_rounded,
                      color: AppTheme.teal, size: 42),
                  title: const Text('上报已提交'),
                  content: const Text('后台审核通过后，障碍物会出现在地图，并根据情况发布为公共任务。'),
                  actions: [
                    FilledButton(
                        onPressed: () => Navigator.pop(context),
                        child: const Text('知道了'))
                  ]));
      description.clear();
      address.clear();
      setState(() {
        photo = null;
        photoBytes = null;
        lat = null;
        lng = null;
        accuracy = null;
        locationSource = '';
        category = 'temporary_obstacle';
        cleanupReason = 'unable_now';
      });
      widget.onSubmitted?.call();
    } on ApiException catch (exception) {
      setState(() => error = exception.message);
    } catch (_) {
      setState(() => error = '上报失败，内容已保留，请稍后重试');
    } finally {
      if (mounted) setState(() => submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final dangerous = category == 'construction' ||
        category == 'road_damage' ||
        cleanupReason == 'unsafe_to_clear';
    return Scaffold(
      appBar: AppBar(
          title: const Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
            Text('拍照上报', style: TextStyle(fontWeight: FontWeight.w800)),
            Text('照片 + 位置 + 描述',
                style: TextStyle(fontSize: 10, color: Colors.blueGrey))
          ])),
      body: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 120),
          children: [
            Card(
                child: InkWell(
                    borderRadius: BorderRadius.circular(20),
                    onTap: () => _pick(ImageSource.camera),
                    child: SizedBox(
                        height: 210,
                        child: photoBytes == null
                            ? const Column(
                                mainAxisAlignment: MainAxisAlignment.center,
                                children: [
                                    Icon(Icons.add_a_photo_outlined,
                                        size: 42, color: AppTheme.teal),
                                    SizedBox(height: 12),
                                    Text('拍摄障碍物全景',
                                        style: TextStyle(
                                            fontWeight: FontWeight.w800,
                                            color: AppTheme.ink)),
                                    SizedBox(height: 5),
                                    Text('尽量包含盲道、障碍物和周围参照物',
                                        style: TextStyle(
                                            fontSize: 11,
                                            color: Colors.blueGrey))
                                  ])
                            : ClipRRect(
                                borderRadius: BorderRadius.circular(20),
                                child: Stack(fit: StackFit.expand, children: [
                                  Image.memory(photoBytes!, fit: BoxFit.cover),
                                  Positioned(
                                      right: 10,
                                      top: 10,
                                      child: IconButton.filledTonal(
                                          onPressed: () =>
                                              _pick(ImageSource.camera),
                                          icon: const Icon(
                                              Icons.refresh_rounded)))
                                ]))))),
            Row(mainAxisAlignment: MainAxisAlignment.end, children: [
              TextButton.icon(
                  onPressed: () => _pick(ImageSource.gallery),
                  icon: const Icon(Icons.photo_library_outlined),
                  label: const Text('从相册选择'))
            ]),
            const _SectionTitle(number: '01', title: '障碍物类型'),
            GridView.count(
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                crossAxisCount: 3,
                childAspectRatio: 1.15,
                mainAxisSpacing: 8,
                crossAxisSpacing: 8,
                children: categories.entries.map((entry) {
                  final selected = category == entry.key;
                  return InkWell(
                      borderRadius: BorderRadius.circular(15),
                      onTap: () => _selectCategory(entry.key),
                      child: AnimatedContainer(
                          duration: const Duration(milliseconds: 180),
                          padding: const EdgeInsets.all(10),
                          decoration: BoxDecoration(
                              color: selected
                                  ? AppTheme.teal.withValues(alpha: .1)
                                  : Colors.white,
                              border: Border.all(
                                  color: selected
                                      ? AppTheme.teal
                                      : const Color(0xFFDDE9EA)),
                              borderRadius: BorderRadius.circular(15)),
                          child: Column(
                              mainAxisAlignment: MainAxisAlignment.center,
                              children: [
                                Icon(entry.value.$2,
                                    color: selected
                                        ? AppTheme.teal
                                        : Colors.blueGrey),
                                const SizedBox(height: 7),
                                Text(entry.value.$1,
                                    textAlign: TextAlign.center,
                                    style: TextStyle(
                                        fontSize: 11,
                                        fontWeight: FontWeight.w700,
                                        color: selected
                                            ? AppTheme.teal
                                            : AppTheme.ink))
                              ])));
                }).toList()),
            const SizedBox(height: 22),
            const _SectionTitle(number: '02', title: '为什么无法现场清理'),
            RadioGroup<String>(
                groupValue: cleanupReason,
                onChanged: (value) {
                  if (value != null) {
                    setState(() => cleanupReason = value);
                  }
                },
                child: Column(
                    children: reasons.entries
                        .map((entry) => RadioListTile<String>(
                            contentPadding: EdgeInsets.zero,
                            value: entry.key,
                            activeColor: AppTheme.teal,
                            title: Text(entry.value,
                                style: const TextStyle(
                                    fontSize: 13,
                                    fontWeight: FontWeight.w600))))
                        .toList())),
            if (dangerous)
              Container(
                  margin: const EdgeInsets.only(bottom: 16),
                  padding: const EdgeInsets.all(13),
                  decoration: BoxDecoration(
                      color: Colors.orange.withValues(alpha: .09),
                      borderRadius: BorderRadius.circular(13)),
                  child: const Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Icon(Icons.health_and_safety_outlined,
                            color: Colors.deepOrange, size: 20),
                        SizedBox(width: 9),
                        Expanded(
                            child: Text(
                                '该问题可能需要市政、施工方或专业人员处理。请保持安全距离，不要擅自进入施工区域或修补路面。',
                                style: TextStyle(
                                    fontSize: 12,
                                    height: 1.5,
                                    color: Colors.deepOrange)))
                      ])),
            const _SectionTitle(number: '03', title: '位置与现场描述'),
            Row(children: [
              Expanded(
                child: OutlinedButton.icon(
                    onPressed: locating ? null : _locate,
                    icon: locating
                        ? const SizedBox(
                            width: 17,
                            height: 17,
                            child: CircularProgressIndicator(strokeWidth: 2))
                        : const Icon(Icons.my_location_rounded),
                    label: const Text('自动定位')),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: FilledButton.tonalIcon(
                    onPressed: locating ? null : _pickOnMap,
                    icon: const Icon(Icons.add_location_alt_outlined),
                    label: const Text('地图选点')),
              ),
            ]),
            if (lat != null && lng != null)
              Container(
                margin: const EdgeInsets.only(top: 10),
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                    color: AppTheme.teal.withValues(alpha: .07),
                    borderRadius: BorderRadius.circular(13),
                    border:
                        Border.all(color: AppTheme.teal.withValues(alpha: .2))),
                child: Row(children: [
                  const Icon(Icons.location_on_rounded, color: AppTheme.teal),
                  const SizedBox(width: 9),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                            locationSource.startsWith('map')
                                ? '已通过地图标注位置'
                                : '已获取当前位置（已转换为高德坐标）',
                            style: const TextStyle(
                                fontWeight: FontWeight.w700,
                                color: AppTheme.ink)),
                        Text(
                          '${lng!.toStringAsFixed(6)}, ${lat!.toStringAsFixed(6)}${accuracy == null ? '' : ' · 精度约 ${accuracy!.round()} 米'}',
                          style: const TextStyle(
                              fontSize: 10, color: Colors.blueGrey),
                        ),
                      ],
                    ),
                  ),
                  IconButton(
                      tooltip: '重新在地图选择',
                      onPressed: _pickOnMap,
                      icon: const Icon(Icons.edit_location_alt_outlined)),
                ]),
              ),
            const SizedBox(height: 10),
            TextField(
                controller: address,
                decoration: const InputDecoration(
                    labelText: '附近道路、门牌或店铺名称',
                    prefixIcon: Icon(Icons.location_on_outlined))),
            const SizedBox(height: 10),
            TextField(
                controller: description,
                minLines: 4,
                maxLines: 7,
                maxLength: 500,
                decoration: const InputDecoration(
                    labelText: '详细描述',
                    hintText: '例如：盲道被店铺台阶截断，轮椅和盲杖均难以通过…',
                    alignLabelWithHint: true)),
            if (error.isNotEmpty)
              Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(error,
                      style: const TextStyle(
                          color: Colors.redAccent, fontSize: 12))),
            const SizedBox(height: 14),
            FilledButton.icon(
                onPressed: submitting ? null : _submit,
                icon: const Icon(Icons.cloud_upload_outlined),
                label: Text(submitting ? '正在安全上传…' : '提交后台审核')),
          ]),
    );
  }
}

class _LocationPickerPage extends StatefulWidget {
  const _LocationPickerPage(
      {required this.api, required this.config, required this.initial});
  final ApiClient api;
  final PublicConfig config;
  final MapSelection initial;

  @override
  State<_LocationPickerPage> createState() => _LocationPickerPageState();
}

class _LocationPickerPageState extends State<_LocationPickerPage> {
  late MapSelection selection = widget.initial;
  String mapError = '';

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          title: const Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('标注障碍位置', style: TextStyle(fontWeight: FontWeight.w800)),
              Text('点击地图可移动红色标记',
                  style: TextStyle(fontSize: 10, color: Colors.blueGrey)),
            ],
          ),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context, selection),
                child: const Text('完成')),
          ],
        ),
        body: Stack(children: [
          Positioned.fill(
            child: MapSurface(
              config: widget.config,
              obstacles: const [],
              remotePageUrl:
                  widget.api.resolveUrl('/volunteer/assets/assets/amap.html'),
              selectionEnabled: true,
              selection: selection,
              userLocation:
                  widget.initial.source == 'gps' ? widget.initial : null,
              onObstacleTap: (_) {},
              onLocationSelected: (value) => setState(() {
                selection = value;
                mapError = '';
              }),
              onMapError: (value) => setState(() => mapError = value),
            ),
          ),
          Positioned(
            left: 16,
            right: 16,
            bottom: 18,
            child: Card(
              child: Padding(
                padding: const EdgeInsets.all(14),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(children: [
                      const Icon(Icons.location_on_rounded,
                          color: AppTheme.teal),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          selection.address.isEmpty
                              ? '点击地图选择准确位置'
                              : selection.address,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                              fontWeight: FontWeight.w700, color: AppTheme.ink),
                        ),
                      ),
                    ]),
                    const SizedBox(height: 5),
                    Text(
                        '${selection.lng.toStringAsFixed(6)}, ${selection.lat.toStringAsFixed(6)}',
                        style: const TextStyle(
                            fontSize: 10, color: Colors.blueGrey)),
                    if (mapError.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 5),
                        child: Text('$mapError，仍可在降级地图选点',
                            style: const TextStyle(
                                fontSize: 10, color: Colors.deepOrange)),
                      ),
                    const SizedBox(height: 10),
                    FilledButton.icon(
                      onPressed: () => Navigator.pop(context, selection),
                      icon: const Icon(Icons.check_rounded),
                      label: const Text('使用此位置'),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ]),
      );
}

class _SectionTitle extends StatelessWidget {
  const _SectionTitle({required this.number, required this.title});
  final String number;
  final String title;
  @override
  Widget build(BuildContext context) => Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Row(children: [
        Container(
            width: 30,
            height: 24,
            alignment: Alignment.center,
            decoration: BoxDecoration(
                color: AppTheme.teal, borderRadius: BorderRadius.circular(7)),
            child: Text(number,
                style: const TextStyle(
                    color: Colors.white,
                    fontSize: 10,
                    fontWeight: FontWeight.w800))),
        const SizedBox(width: 9),
        Text(title,
            style: const TextStyle(
                fontSize: 16, fontWeight: FontWeight.w800, color: AppTheme.ink))
      ]));
}
