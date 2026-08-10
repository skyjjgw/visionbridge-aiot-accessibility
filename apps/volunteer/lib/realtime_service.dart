import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/web_socket_channel.dart';

import 'api_client.dart';

/// Realtime invalidation channel. REST remains the source of truth: messages
/// only tell screens to fetch a fresh, authenticated snapshot.
class RealtimeService {
  RealtimeService(this.api);

  final ApiClient api;
  final StreamController<String> _events = StreamController.broadcast();
  WebSocketChannel? _channel;
  Timer? _reconnectTimer;
  bool _stopped = false;

  Stream<String> get events => _events.stream;

  Uri _endpoint() {
    final base = api.baseUrl.isNotEmpty ? Uri.parse(api.baseUrl) : Uri.base;
    return base.replace(
      scheme: base.scheme == 'https' ? 'wss' : 'ws',
      path: '/ws/realtime',
      query: '',
      fragment: '',
    );
  }

  void start() {
    _stopped = false;
    _connect();
  }

  void _connect() {
    if (_stopped || _channel != null) return;
    try {
      final channel = WebSocketChannel.connect(_endpoint());
      _channel = channel;
      channel.stream.listen(
        (message) {
          try {
            final decoded = jsonDecode(message.toString());
            if (decoded is Map && decoded['type'] != 'heartbeat') {
              _events.add(decoded['type']?.toString() ?? 'data.updated');
            }
          } catch (_) {
            // Ignore malformed notifications; periodic REST refresh is the
            // bounded fallback and no unverified payload enters application state.
          }
        },
        onError: (_) => _scheduleReconnect(),
        onDone: _scheduleReconnect,
        cancelOnError: true,
      );
    } catch (_) {
      _scheduleReconnect();
    }
  }

  void _scheduleReconnect() {
    _channel = null;
    if (_stopped || _reconnectTimer?.isActive == true) return;
    _reconnectTimer = Timer(const Duration(seconds: 3), _connect);
  }

  Future<void> dispose() async {
    _stopped = true;
    _reconnectTimer?.cancel();
    await _channel?.sink.close();
    await _events.close();
  }
}
