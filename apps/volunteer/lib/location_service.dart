import 'dart:async';

import 'package:geolocator/geolocator.dart';

import 'geo_utils.dart';
import 'models.dart';

class LocationAcquireException implements Exception {
  const LocationAcquireException(this.message);
  final String message;
}

class LocationService {
  const LocationService._();

  static Future<MapSelection> acquireBestFix({
    String address = '当前位置',
    Duration samplingWindow = const Duration(seconds: 14),
  }) async {
    if (!await Geolocator.isLocationServiceEnabled()) {
      throw const LocationAcquireException('请先开启系统定位服务');
    }

    var permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.denied) {
      permission = await Geolocator.requestPermission();
    }
    if (permission == LocationPermission.deniedForever) {
      throw const LocationAcquireException('定位权限已被永久关闭，请在系统设置中允许视桥访问精确位置');
    }
    if (permission == LocationPermission.denied) {
      throw const LocationAcquireException('未获得定位权限，仍可使用地图手动选点');
    }

    final samples = <Position>[];
    try {
      final stream = Geolocator.getPositionStream(
        locationSettings: LocationSettings(
          accuracy: LocationAccuracy.bestForNavigation,
          distanceFilter: 0,
          timeLimit: samplingWindow,
        ),
      );
      await for (final position in stream.take(5)) {
        samples.add(position);
        if (samples.length >= 2 && position.accuracy <= 15) break;
      }
    } on TimeoutException {
      // Keep the best sample already received during the sampling window.
    } catch (_) {
      // Some devices do not support a continuous high-accuracy stream. The
      // one-shot fallback below still provides a fresh fix.
    }

    if (samples.isEmpty) {
      samples.add(await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.best,
          timeLimit: Duration(seconds: 20),
        ),
      ));
    }
    samples.sort((a, b) => a.accuracy.compareTo(b.accuracy));
    final best = samples.first;
    return wgs84ToGcj02(
      lat: best.latitude,
      lng: best.longitude,
      accuracy: best.accuracy,
      address: address,
    );
  }

  static String accuracyHint(double? accuracy) {
    if (accuracy == null) return '';
    if (accuracy <= 20) return '定位精度约 ${accuracy.round()} 米';
    if (accuracy <= 50) return '定位精度约 ${accuracy.round()} 米，建议在地图上核对标记';
    return '定位精度仅约 ${accuracy.round()} 米，请到开阔位置重试或在地图上校正';
  }
}
