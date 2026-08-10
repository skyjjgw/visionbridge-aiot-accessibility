"use client";

import { useEffect } from "react";

/** WebSocket invalidation with reconnect. REST endpoints remain authoritative. */
export function useRealtimeRefresh(refresh: () => void) {
  useEffect(() => {
    let stopped = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    const connect = () => {
      const endpoint = new URL("/ws/realtime", window.location.href);
      endpoint.protocol = endpoint.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(endpoint);
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(String(message.data)) as { type?: string };
          if (event.type !== "heartbeat") refresh();
        } catch { /* Keep the latest verified REST state. */ }
      };
      socket.onerror = () => socket?.close();
      socket.onclose = () => {
        if (!stopped) reconnectTimer = window.setTimeout(connect, 3000);
      };
    };
    connect();
    return () => {
      stopped = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [refresh]);
}
