"use client";

import { Alert, Button, Chip, Tabs, Tooltip } from "@heroui/react";
import {
  Camera,
  Eye,
  HardDrive,
  MapPinned,
  MonitorPlay,
  Radio,
  RefreshCw,
  Satellite,
  Signal,
  Timer,
  VideoOff,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Device } from "./vision-bridge-dashboard";

type DeviceResponse = { items: Device[]; count: number; generatedAt: string };
type PlayerMode = "webrtc" | "hls";

async function fetchDevices(): Promise<DeviceResponse> {
  const response = await fetch("/api/v1/devices", { cache: "no-store" });
  if (!response.ok) throw new Error(`设备接口返回 ${response.status}`);
  return response.json() as Promise<DeviceResponse>;
}

function relativeTime(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "暂无上报";
  const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  if (seconds < 5) return "刚刚更新";
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  return new Date(timestamp).toLocaleString("zh-CN", { hour12: false });
}

function streamLabel(device: Device): string {
  if (device.streamStatus === "live") return "视频在线";
  return device.status === "online" ? "视频待接入" : "设备离线";
}

export function DeviceFleetView({ fallbackDevice }: { fallbackDevice: Device }) {
  const [devices, setDevices] = useState<Device[]>([fallbackDevice]);
  const [selectedId, setSelectedId] = useState(fallbackDevice.id);
  const [mode, setMode] = useState<PlayerMode>("webrtc");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshedAt, setRefreshedAt] = useState(new Date());
  const [playerKey, setPlayerKey] = useState(0);
  const sequence = useRef(0);

  const refresh = useCallback(async () => {
    const current = ++sequence.current;
    try {
      const result = await fetchDevices();
      if (current !== sequence.current) return;
      const next = result.items.length > 0 ? result.items : [fallbackDevice];
      setDevices(next);
      setSelectedId((selected) => next.some((item) => item.id === selected) ? selected : next[0].id);
      setError("");
      setRefreshedAt(new Date());
    } catch (reason) {
      if (current !== sequence.current) return;
      setError(reason instanceof Error ? reason.message : "设备数据暂时不可用");
    } finally {
      if (current === sequence.current) setLoading(false);
    }
  }, [fallbackDevice]);

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(), 0);
    const timer = window.setInterval(() => void refresh(), 3000);
    const resume = () => { if (document.visibilityState === "visible") void refresh(); };
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("focus", resume);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", resume);
      window.removeEventListener("focus", resume);
    };
  }, [refresh]);

  const selected = useMemo(
    () => devices.find((item) => item.id === selectedId) ?? devices[0] ?? fallbackDevice,
    [devices, fallbackDevice, selectedId],
  );
  const onlineCount = devices.filter((item) => item.status === "online").length;
  const streamCount = devices.filter((item) => item.streamStatus === "live").length;
  const playerUrl = mode === "webrtc" ? selected.webRtcUrl : selected.hlsUrl;
  const playerSrc = `${playerUrl}?controls=true&muted=true&autoplay=true&playsinline=true&v=${playerKey}`;

  const selectDevice = (device: Device) => {
    setSelectedId(device.id);
    setMode("webrtc");
    setPlayerKey((value) => value + 1);
  };

  return <div className="subpage device-fleet-page">
    <div className="subpage-head">
      <div><span className="eyebrow">EDGE FLEET</span><h1>边缘设备</h1><p>从设备列表进入实时识别画面，统一查看 GNSS、推理性能与视频链路。</p></div>
      <div className="fleet-head-actions">
        <Chip className="architecture-badge" size="sm" variant="soft"><Signal size={17} />{onlineCount}/{devices.length} 台在线</Chip>
        <Chip className="architecture-badge stream-badge" size="sm" variant="soft"><MonitorPlay size={17} />{streamCount} 路直播</Chip>
        <Tooltip><Button className="icon-button" isIconOnly size="sm" variant="ghost" onClick={() => void refresh()} aria-label="刷新设备"><RefreshCw size={16} className={loading ? "spin" : ""} /></Button><Tooltip.Content>刷新设备</Tooltip.Content></Tooltip>
      </div>
    </div>

    {error && <Alert className="fleet-error" status="warning"><Alert.Indicator /><Alert.Content><Alert.Title>设备数据暂不可用</Alert.Title><Alert.Description>{error}，系统将在 3 秒后自动重试。</Alert.Description></Alert.Content></Alert>}

    <div className="fleet-layout">
      <aside className="panel fleet-list-panel">
        <div className="panel-head"><div><span className="eyebrow">设备列表</span><h2>全部终端</h2></div><span className="fleet-refresh-state"><i />3 秒同步</span></div>
        <div className="fleet-list">
          {devices.map((device) => <button
            type="button"
            key={device.id}
            className={`fleet-device-card ${selected.id === device.id ? "active" : ""}`}
            onClick={() => selectDevice(device)}
          >
            <span className="fleet-device-icon"><Camera size={19} /></span>
            <span className="fleet-device-copy"><strong>{device.name}</strong><small>{device.pointName}</small><em suppressHydrationWarning>{relativeTime(device.lastSeen)}</em></span>
            <Chip className={`fleet-state state-${device.streamStatus}`} size="sm" variant="soft"><i />{streamLabel(device)}</Chip>
          </button>)}
        </div>
        <div className="fleet-list-foot"><span>最近同步</span><strong suppressHydrationWarning>{refreshedAt.toLocaleTimeString("zh-CN", { hour12: false })}</strong></div>
      </aside>

      <section className="panel stream-detail-panel">
        <div className="stream-detail-head">
          <div><span className="eyebrow">实时识别画面</span><h2>{selected.name}</h2><p>{selected.id} · {selected.pointName}</p></div>
          <div className="stream-controls">
            <Tabs className="stream-mode-switch" selectedKey={mode} onSelectionChange={(key) => { setMode(String(key) as PlayerMode); setPlayerKey((value) => value + 1); }} aria-label="播放协议">
              <Tabs.ListContainer><Tabs.List><Tabs.Tab id="webrtc">WebRTC 低延迟</Tabs.Tab><Tabs.Tab id="hls">HLS 兼容</Tabs.Tab></Tabs.List></Tabs.ListContainer>
            </Tabs>
            <Tooltip><Button className="icon-button" isIconOnly size="sm" variant="ghost" onClick={() => setPlayerKey((value) => value + 1)} aria-label="重新连接视频"><RefreshCw size={15} /></Button><Tooltip.Content>重新连接视频</Tooltip.Content></Tooltip>
          </div>
        </div>

        <div className="stream-stage">
          {selected.streamStatus === "live" ? <iframe
            key={`${selected.id}-${mode}-${playerKey}`}
            src={playerSrc}
            title={`${selected.name}实时识别视频`}
            allow="autoplay; fullscreen; picture-in-picture"
          /> : <div className="stream-empty"><VideoOff size={34} /><strong>实时视频尚未接入</strong><span>遥测数据仍会持续同步；边缘视频发布服务上线后，这里会自动显示。</span></div>}
          <div className="stream-overlay-top"><span className={`stream-live-dot ${selected.streamStatus === "live" ? "on" : ""}`}><i />{selected.streamStatus === "live" ? "LIVE" : "OFFLINE"}</span><span>{mode === "webrtc" ? "WebRTC · 低延迟" : "HLS · 兼容模式"}</span></div>
          <div className="stream-overlay-bottom"><Radio size={13} /><span>边缘端识别框与 ROI 已叠加在视频帧内</span></div>
        </div>

        <div className="stream-metrics">
          <div><Camera /><span>采集帧率</span><strong>{selected.cameraFps.toFixed(1)} FPS</strong></div>
          <div><Timer /><span>单帧推理</span><strong>{selected.inferenceMs} ms</strong></div>
          <div><Radio /><span>识别频率</span><strong>{selected.inferenceFps.toFixed(2)} FPS</strong></div>
          <div><Satellite /><span>GNSS</span><strong>{selected.sats} 颗 · HDOP {selected.hdop}</strong></div>
          <div><Eye /><span>当前观看</span><strong>{selected.streamReaders} 路</strong></div>
          <div><HardDrive /><span>模型版本</span><strong>{selected.model}</strong></div>
        </div>
        <div className="device-coordinate stream-coordinate"><MapPinned size={18} /><div><span>最近位置</span><strong>{selected.lng.toFixed(6)}, {selected.lat.toFixed(6)}</strong></div><em suppressHydrationWarning>{relativeTime(selected.lastSeen)}</em></div>
      </section>
    </div>
  </div>;
}
