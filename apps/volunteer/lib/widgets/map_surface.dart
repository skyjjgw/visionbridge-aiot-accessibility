import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../app_theme.dart';
import '../models.dart';

class MapSurface extends StatefulWidget {
  const MapSurface({
    super.key,
    required this.config,
    required this.obstacles,
    required this.onObstacleTap,
    this.remotePageUrl = '',
    this.selectionEnabled = false,
    this.selection,
    this.userLocation,
    this.onLocationSelected,
    this.onApprovalNumber,
    this.onMapError,
  });

  final PublicConfig config;
  final List<Obstacle> obstacles;
  final ValueChanged<Obstacle> onObstacleTap;
  final String remotePageUrl;
  final bool selectionEnabled;
  final MapSelection? selection;
  final MapSelection? userLocation;
  final ValueChanged<MapSelection>? onLocationSelected;
  final ValueChanged<String>? onApprovalNumber;
  final ValueChanged<String>? onMapError;

  @override
  State<MapSurface> createState() => _MapSurfaceState();
}

class _MapSurfaceState extends State<MapSurface> {
  WebViewController? controller;
  Timer? readinessTimer;
  bool ready = false;
  bool failed = false;
  String failureReason = '';

  bool get supportsWebView =>
      !kIsWeb &&
      (defaultTargetPlatform == TargetPlatform.android ||
          defaultTargetPlatform == TargetPlatform.iOS ||
          defaultTargetPlatform == TargetPlatform.macOS);

  @override
  void initState() {
    super.initState();
    _ensureController();
  }

  @override
  void didUpdateWidget(covariant MapSurface oldWidget) {
    super.didUpdateWidget(oldWidget);
    // Config is loaded asynchronously by MapScreen. The original implementation
    // only checked it in initState, which permanently left the App in fallback.
    if (controller == null && widget.config.amapKey.isNotEmpty) {
      _ensureController();
    } else if (oldWidget.config.amapKey != widget.config.amapKey &&
        widget.config.amapKey.isNotEmpty) {
      _retry();
    } else if (ready) {
      _sync();
    }
  }

  @override
  void dispose() {
    readinessTimer?.cancel();
    super.dispose();
  }

  void _ensureController() {
    if (!supportsWebView ||
        widget.config.amapKey.isEmpty ||
        controller != null) {
      return;
    }
    final next = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(const Color(0xFFF0F5F6))
      ..addJavaScriptChannel('VisionBridge', onMessageReceived: _onMessage)
      ..setNavigationDelegate(NavigationDelegate(
        onPageFinished: (_) => _boot(),
        onWebResourceError: (event) {
          if (event.isForMainFrame ?? false) {
            _markFailed('地图页面加载失败（${event.errorCode}）');
          }
        },
      ));
    controller = next;
    readinessTimer?.cancel();
    readinessTimer = Timer(const Duration(seconds: 15), () {
      if (mounted && !ready) _markFailed('高德地图加载超时');
    });
    if (widget.remotePageUrl.startsWith('http')) {
      next.loadRequest(Uri.parse(widget.remotePageUrl));
    } else {
      next.loadFlutterAsset('assets/amap.html');
    }
  }

  Future<void> _boot() async {
    try {
      await controller?.runJavaScript(
          'bootVisionBridge(${jsonEncode(widget.config.toJson())})');
    } catch (_) {
      _markFailed('地图初始化脚本执行失败');
    }
  }

  void _onMessage(JavaScriptMessage message) {
    final value = message.message;
    if (value == 'ready') {
      readinessTimer?.cancel();
      if (mounted) {
        setState(() {
          ready = true;
          failed = false;
          failureReason = '';
        });
      }
      _sync();
      return;
    }
    if (value.startsWith('approval:')) {
      widget.onApprovalNumber?.call(value.substring(9));
      return;
    }
    if (value.startsWith('obstacle:')) {
      final id = value.substring(9);
      final match = widget.obstacles.where((item) => item.id == id);
      if (match.isNotEmpty) widget.onObstacleTap(match.first);
      return;
    }
    if (value.startsWith('select:')) {
      try {
        final data = jsonDecode(value.substring(7)) as Map<String, dynamic>;
        widget.onLocationSelected?.call(MapSelection.fromJson(data));
      } catch (_) {
        _markFailed('地图选点结果无法解析');
      }
      return;
    }
    if (value.startsWith('error:')) {
      _markFailed(_mapErrorLabel(value.substring(6)));
    }
  }

  String _mapErrorLabel(String value) {
    if (value.contains('missing-key')) return '高德地图 Key 未配置';
    if (value.contains('load-failed')) return '高德地图脚本加载失败';
    if (value.contains('boot-failed')) return '高德地图初始化失败';
    return '地图暂时不可用';
  }

  void _markFailed(String reason) {
    readinessTimer?.cancel();
    widget.onMapError?.call(reason);
    if (mounted) {
      setState(() {
        failed = true;
        failureReason = reason;
      });
    }
  }

  void _retry() {
    readinessTimer?.cancel();
    if (mounted) {
      setState(() {
        controller = null;
        ready = false;
        failed = false;
        failureReason = '';
      });
    } else {
      controller = null;
      ready = false;
      failed = false;
    }
    _ensureController();
  }

  Future<void> _sync() async {
    if (!ready) return;
    try {
      await controller?.runJavaScript(
          'syncVisionBridgeObstacles(${jsonEncode(widget.obstacles.map((item) => item.toMapJson()).toList())})');
      await controller?.runJavaScript(
          'setVisionBridgeSelectionMode(${widget.selectionEnabled})');
      await controller?.runJavaScript(
          'syncVisionBridgeSelection(${jsonEncode(widget.selection?.toJson())})');
      await controller?.runJavaScript(
          'syncVisionBridgeUserLocation(${jsonEncode(widget.userLocation?.toJson())})');
    } catch (_) {
      _markFailed('地图数据同步失败');
    }
  }

  @override
  Widget build(BuildContext context) {
    if (controller == null || failed) {
      return _FallbackMap(
        config: widget.config,
        obstacles: widget.obstacles,
        selectionEnabled: widget.selectionEnabled,
        selection: widget.selection,
        userLocation: widget.userLocation,
        onTap: widget.onObstacleTap,
        onLocationSelected: widget.onLocationSelected,
        failureReason: failureReason,
        onRetry: widget.config.amapKey.isEmpty ? null : _retry,
      );
    }
    return Stack(children: [
      Positioned.fill(child: WebViewWidget(controller: controller!)),
      if (!ready)
        const Positioned.fill(
          child: ColoredBox(
            color: Color(0xFFF0F5F6),
            child: Center(child: CircularProgressIndicator()),
          ),
        ),
      if (widget.selectionEnabled)
        Positioned(
          left: 14,
          right: 14,
          top: 12,
          child: IgnorePointer(
            child: DecoratedBox(
              decoration: BoxDecoration(
                color: AppTheme.ink.withValues(alpha: .88),
                borderRadius: BorderRadius.circular(12),
              ),
              child: const Padding(
                padding: EdgeInsets.symmetric(horizontal: 12, vertical: 9),
                child: Text('点击地图标注障碍物准确位置',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: Colors.white, fontSize: 12)),
              ),
            ),
          ),
        ),
    ]);
  }
}

class _FallbackMap extends StatelessWidget {
  const _FallbackMap({
    required this.config,
    required this.obstacles,
    required this.selectionEnabled,
    required this.selection,
    required this.userLocation,
    required this.onTap,
    required this.onLocationSelected,
    required this.failureReason,
    required this.onRetry,
  });

  final PublicConfig config;
  final List<Obstacle> obstacles;
  final bool selectionEnabled;
  final MapSelection? selection;
  final MapSelection? userLocation;
  final ValueChanged<Obstacle> onTap;
  final ValueChanged<MapSelection>? onLocationSelected;
  final String failureReason;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(builder: (context, constraints) {
      final centerLng = config.defaultCenter.isNotEmpty
          ? config.defaultCenter.first
          : 121.138923;
      final centerLat =
          config.defaultCenter.length > 1 ? config.defaultCenter[1] : 28.632112;
      final lats = <double>[centerLat, ...obstacles.map((e) => e.lat)];
      final lngs = <double>[centerLng, ...obstacles.map((e) => e.lng)];
      if (selection != null) {
        lats.add(selection!.lat);
        lngs.add(selection!.lng);
      }
      if (userLocation != null) {
        lats.add(userLocation!.lat);
        lngs.add(userLocation!.lng);
      }
      var minLat = lats.reduce(math.min) - .0012;
      var maxLat = lats.reduce(math.max) + .0012;
      var minLng = lngs.reduce(math.min) - .0012;
      var maxLng = lngs.reduce(math.max) + .0012;
      if (maxLat - minLat < .003) {
        minLat -= .0015;
        maxLat += .0015;
      }
      if (maxLng - minLng < .003) {
        minLng -= .0015;
        maxLng += .0015;
      }

      Offset position(double lat, double lng) => Offset(
            18 +
                (lng - minLng) /
                    (maxLng - minLng) *
                    math.max(1, constraints.maxWidth - 54),
            28 +
                (maxLat - lat) /
                    (maxLat - minLat) *
                    math.max(1, constraints.maxHeight - 82),
          );

      void select(TapDownDetails details) {
        if (!selectionEnabled || onLocationSelected == null) return;
        final usableWidth = math.max(1, constraints.maxWidth - 54);
        final usableHeight = math.max(1, constraints.maxHeight - 82);
        final x =
            ((details.localPosition.dx - 18) / usableWidth).clamp(0.0, 1.0);
        final y =
            ((details.localPosition.dy - 28) / usableHeight).clamp(0.0, 1.0);
        onLocationSelected!(MapSelection(
          lat: maxLat - y * (maxLat - minLat),
          lng: minLng + x * (maxLng - minLng),
          address: '地图手动标注点（请补充附近道路或门牌）',
          source: 'map-fallback',
        ));
      }

      return GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTapDown: select,
        child: Stack(children: [
          const Positioned.fill(child: _MapBackdrop()),
          ...obstacles.map((item) {
            final point = position(item.lat, item.lng);
            final color = item.priority == 'urgent'
                ? const Color(0xFFEF6B62)
                : item.priority == 'low'
                    ? AppTheme.teal
                    : AppTheme.warm;
            return Positioned(
              left: point.dx - 17,
              top: point.dy - 17,
              child: GestureDetector(
                onTap: () => onTap(item),
                child: Container(
                  width: 34,
                  height: 34,
                  decoration: BoxDecoration(
                    color: color,
                    shape: BoxShape.circle,
                    border: Border.all(color: Colors.white, width: 3),
                    boxShadow: const [
                      BoxShadow(color: Colors.black26, blurRadius: 12)
                    ],
                  ),
                  child: const Icon(Icons.priority_high_rounded,
                      color: Colors.white, size: 18),
                ),
              ),
            );
          }),
          if (userLocation != null)
            _positionedPoint(
              position(userLocation!.lat, userLocation!.lng),
              color: Colors.blueAccent,
              icon: Icons.my_location_rounded,
            ),
          if (selection != null)
            _positionedPoint(
              position(selection!.lat, selection!.lng),
              color: Colors.redAccent,
              icon: Icons.location_on_rounded,
              size: 38,
            ),
          Positioned(
            left: 14,
            right: 14,
            bottom: 14,
            child: Row(children: [
              Expanded(
                child: DecoratedBox(
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: .94),
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Padding(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
                    child: Text(
                      selectionEnabled
                          ? '降级地图仍可选点 · 点击道路位置标注'
                          : failureReason.isEmpty
                              ? '地图配置加载中 · 数据来自视桥自有云'
                              : '$failureReason · 已启用可操作降级地图',
                      style:
                          const TextStyle(fontSize: 10, color: Colors.blueGrey),
                    ),
                  ),
                ),
              ),
              if (onRetry != null) ...[
                const SizedBox(width: 8),
                IconButton.filledTonal(
                  tooltip: '重试高德地图',
                  onPressed: onRetry,
                  icon: const Icon(Icons.refresh_rounded),
                ),
              ],
            ]),
          ),
        ]),
      );
    });
  }

  static Positioned _positionedPoint(Offset point,
          {required Color color, required IconData icon, double size = 30}) =>
      Positioned(
        left: point.dx - size / 2,
        top: point.dy - size / 2,
        child: IgnorePointer(
          child: Container(
            width: size,
            height: size,
            decoration: BoxDecoration(
              color: color,
              shape: BoxShape.circle,
              border: Border.all(color: Colors.white, width: 3),
              boxShadow: const [
                BoxShadow(color: Colors.black26, blurRadius: 9)
              ],
            ),
            child: Icon(icon, color: Colors.white, size: size * .55),
          ),
        ),
      );
}

class _MapBackdrop extends StatelessWidget {
  const _MapBackdrop();
  @override
  Widget build(BuildContext context) => CustomPaint(painter: _MapPainter());
}

class _MapPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    canvas.drawColor(const Color(0xFFEAF1F2), BlendMode.src);
    final road = Paint()
      ..color = Colors.white
      ..strokeWidth = 18
      ..strokeCap = StrokeCap.round;
    final lane = Paint()
      ..color = const Color(0xFFD4E1E3)
      ..strokeWidth = 1;
    final paths = [
      Path()
        ..moveTo(-20, size.height * .28)
        ..cubicTo(size.width * .3, size.height * .08, size.width * .55,
            size.height * .72, size.width + 20, size.height * .5),
      Path()
        ..moveTo(size.width * .2, -20)
        ..cubicTo(size.width * .32, size.height * .3, size.width * .62,
            size.height * .55, size.width * .8, size.height + 20),
      Path()
        ..moveTo(-20, size.height * .78)
        ..lineTo(size.width + 20, size.height * .18)
    ];
    for (final path in paths) {
      canvas.drawPath(path, road);
      canvas.drawPath(path, lane);
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
