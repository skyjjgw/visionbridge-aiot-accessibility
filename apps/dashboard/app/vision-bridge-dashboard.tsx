"use client";

import { Avatar, Button, Chip, Tooltip } from "@heroui/react";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BarChart3,
  BellRing,
  Camera,
  Check,
  ChevronDown,
  CircleGauge,
  Cloud,
  Cpu,
  Database,
  ExternalLink,
  Focus,
  Gauge,
  Layers3,
  LocateFixed,
  MapPinned,
  Menu,
  Radio,
  RefreshCw,
  Route,
  Satellite,
  Search,
  Settings2,
  ShieldCheck,
  Signal,
  SlidersHorizontal,
  Timer,
  TriangleAlert,
  X,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { VolunteerAdminView } from "./volunteer-admin";
import { DeviceFleetView } from "./device-fleet";

type View = "overview" | "events" | "devices" | "analytics" | "volunteers" | "settings";

type EventItem = {
  id: string;
  type: string;
  typeLabel: string;
  status: "suspected" | "active" | "dispatched" | "cleared";
  statusLabel: string;
  severity: "attention" | "warning" | "critical";
  confidence: number;
  pointName: string;
  address: string;
  lat: number;
  lng: number;
  createdAt: string;
  durationSec: number;
  source: string;
};

export type Device = {
  id: string;
  name: string;
  status: "online" | "offline" | "warning";
  pointName: string;
  lastSeen: string;
  cameraStatus: string;
  gpsStatus: string;
  cameraFps: number;
  inferenceMs: number;
  inferenceFps: number;
  sats: number;
  hdop: number;
  lat: number;
  lng: number;
  model: string;
  streamPath: string;
  streamStatus: "live" | "offline";
  streamReaders: number;
  webRtcUrl: string;
  hlsUrl: string;
};

type Overview = {
  generatedAt: string;
  dataMode: "live" | "empty";
  linkStatus: "online" | "degraded" | "offline";
  kpis: {
    onlineDevices: number;
    totalDevices: number;
    activeEvents: number;
    todayEvents: number;
    closureRate: number;
    averageResponseMin: number | null;
    todayChangePercent: number;
  };
  device: Device | null;
  recentEvents: EventItem[];
  trends: {
    labels: string[];
    events: number[];
    inferenceMs: (number | null)[];
    cameraFps: (number | null)[];
  };
  analysis: { provider: string; configured: boolean; jobs: Record<string, number> };
};

type PublicConfig = { amapKey?: string; amapSecurityCode?: string; defaultCenter?: [number, number] };

type AMapMarkerInstance = {
  on: (event: "click", handler: () => void) => void;
};

type AMapInstance = {
  add: (markers: AMapMarkerInstance[]) => void;
  remove: (markers: AMapMarkerInstance[]) => void;
  destroy?: () => void;
};

type AMapNamespace = {
  Map: new (container: HTMLDivElement, options: {
    zoom: number;
    center: [number, number];
    viewMode: "2D";
    mapStyle: string;
    showLabel: boolean;
  }) => AMapInstance;
  Marker: new (options: {
    position: [number, number];
    content: string;
    anchor: "center";
  }) => AMapMarkerInstance;
};

declare global {
  interface Window {
    AMap?: AMapNamespace;
    _AMapSecurityConfig?: { securityJsCode: string };
  }
}

const emptyOverview: Overview = {
  generatedAt: "",
  dataMode: "empty",
  linkStatus: "offline",
  kpis: { onlineDevices: 0, totalDevices: 0, activeEvents: 0, todayEvents: 0, closureRate: 0, averageResponseMin: null, todayChangePercent: 0 },
  device: null,
  recentEvents: [],
  trends: { labels: [], events: [], inferenceMs: [], cameraFps: [] },
  analysis: { provider: "disabled", configured: false, jobs: {} },
};

const navItems = [
  { id: "overview" as const, label: "监管总览", icon: Layers3 },
  { id: "events" as const, label: "事件中心", icon: BellRing },
  { id: "devices" as const, label: "边缘设备", icon: Cpu },
  { id: "analytics" as const, label: "趋势分析", icon: BarChart3 },
  { id: "volunteers" as const, label: "志愿者协同", icon: ShieldCheck },
  { id: "settings" as const, label: "数据与接口", icon: Settings2 },
];

const statusStyles: Record<EventItem["status"], string> = {
  suspected: "status-suspected",
  active: "status-active",
  dispatched: "status-dispatched",
  cleared: "status-cleared",
};

const formatAgo = (value: string) => {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  return `${Math.floor(seconds / 3600)} 小时前`;
};

const fetchJson = async <T,>(url: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(url, { cache: "no-store", ...init });
  if (!response.ok) throw new Error(`${response.status}`);
  return response.json() as Promise<T>;
};

function StatusPill({ status }: { status: EventItem["status"] }) {
  const labels = { suspected: "疑似", active: "未接单", dispatched: "处置中", cleared: "已闭环" };
  return <Chip className={`status-pill ${statusStyles[status]}`} size="sm" variant="soft"><span />{labels[status]}</Chip>;
}

function MetricCard({ icon: Icon, label, value, suffix, detail, tone = "blue" }: {
  icon: typeof Activity; label: string; value: string | number; suffix?: string; detail: string; tone?: string;
}) {
  return (
    <article className={`metric-card tone-${tone}`}>
      <div className="metric-icon"><Icon size={18} /></div>
      <div>
        <p>{label}</p>
        <strong>{value}<small>{suffix}</small></strong>
        <span>{detail}</span>
      </div>
    </article>
  );
}

function MapStage({ config, overview, onEvent }: { config: PublicConfig; overview: Overview; onEvent: (event: EventItem) => void }) {
  const mapRef = useRef<HTMLDivElement>(null);
  const mapInstance = useRef<AMapInstance | null>(null);
  const markersRef = useRef<AMapMarkerInstance[]>([]);
  const [mapReady, setMapReady] = useState(false);
  const [mapFailed, setMapFailed] = useState(false);
  const centerLng = overview.device?.lng ?? config.defaultCenter?.[0] ?? 121.138923;
  const centerLat = overview.device?.lat ?? config.defaultCenter?.[1] ?? 28.632112;

  useEffect(() => {
    if (!config.amapKey || !mapRef.current) return;
    let cancelled = false;
    const boot = () => {
      if (cancelled || !mapRef.current || !window.AMap) return;
      const AMap = window.AMap;
      mapInstance.current = new AMap.Map(mapRef.current, {
        zoom: 16.5,
        center: [centerLng, centerLat],
        viewMode: "2D",
        mapStyle: "amap://styles/darkblue",
        showLabel: true,
      });
      setMapReady(true);
    };
    if (window.AMap) boot();
    else {
      window._AMapSecurityConfig = config.amapSecurityCode ? { securityJsCode: config.amapSecurityCode } : undefined;
      const script = document.createElement("script");
      script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(config.amapKey)}`;
      script.async = true;
      script.onload = boot;
      script.onerror = () => setMapFailed(true);
      document.head.appendChild(script);
    }
    return () => {
      cancelled = true;
      markersRef.current = [];
      mapInstance.current?.destroy?.();
      mapInstance.current = null;
    };
  }, [centerLat, centerLng, config.amapKey, config.amapSecurityCode]);

  useEffect(() => {
    if (!mapReady || !mapInstance.current || !window.AMap) return;
    const AMap = window.AMap;
    if (markersRef.current.length) mapInstance.current.remove(markersRef.current);
    markersRef.current = overview.recentEvents.map((event) => {
      const marker = new AMap.Marker({
        position: [event.lng, event.lat],
        content: `<button class="amap-event-marker ${event.status}" aria-label="${event.typeLabel}"><span></span></button>`,
        anchor: "center",
      });
      marker.on("click", () => onEvent(event));
      return marker;
    });
    if (markersRef.current.length) mapInstance.current.add(markersRef.current);
  }, [mapReady, onEvent, overview.recentEvents]);

  const fallback = !config.amapKey || mapFailed;
  return (
    <section className="panel map-panel">
      <div className="panel-head map-head">
        <div><span className="eyebrow">空间态势</span><h2>盲道事件地图</h2></div>
        <div className="map-controls">
          <Button className="chip active" size="sm" variant="ghost"><Focus size={14} />事件点</Button>
          <Tooltip><Button className="icon-button" isIconOnly size="sm" variant="ghost" aria-label="定位设备"><LocateFixed size={17} /></Button><Tooltip.Content>定位巡检终端</Tooltip.Content></Tooltip>
          <Tooltip><Button className="icon-button" isIconOnly size="sm" variant="ghost" aria-label="全屏地图"><ExternalLink size={17} /></Button><Tooltip.Content>展开地图</Tooltip.Content></Tooltip>
        </div>
      </div>
      <div className={`map-canvas ${fallback ? "fallback-map" : ""}`} ref={mapRef}>
        {fallback && <div className="map-loading"><MapPinned size={20} />高德地图服务未配置或暂不可用；事件列表仍为实时数据</div>}
        {!fallback && !mapReady && <div className="map-loading"><RefreshCw size={20} className="spin" />地图加载中</div>}
        <div className="map-legend"><span><i className="legend-critical" />未接单</span><span><i className="legend-dispatch" />处置中</span></div>
        <div className="map-source">高德地图 JS API 2.0 · GCJ-02</div>
      </div>
    </section>
  );
}

function DeviceHealth({ device }: { device: Device | null }) {
  if (!device) return <section className="panel device-health"><div className="panel-head"><div><span className="eyebrow">实时终端</span><h2>设备健康</h2></div></div><div className="map-loading"><Cpu size={20} />尚无边缘设备上报</div></section>;
  const items = [
    { label: "摄像头", value: device.cameraStatus === "streaming" ? "工作正常" : device.cameraStatus, icon: Camera, ok: device.cameraStatus === "streaming" },
    { label: "GPS 定位", value: device.sats > 0 ? `${device.sats} 星 · HDOP ${device.hdop}` : "串口在线 · 等待定位", icon: Satellite, ok: device.gpsStatus === "connected" },
    { label: "边缘推理", value: `${device.inferenceMs} ms`, icon: Zap, ok: device.inferenceMs < 1200 },
    { label: "采集帧率", value: `${device.cameraFps.toFixed(1)} FPS`, icon: Gauge, ok: device.cameraFps >= 5 },
  ];
  return (
    <section className="panel device-health">
      <div className="panel-head"><div><span className="eyebrow">实时终端</span><h2>设备健康</h2></div><span className="live-badge"><i />在线</span></div>
      <div className="device-title"><div className="device-orbit"><Cpu size={24} /></div><div><strong>{device.name}</strong><span>{device.pointName}</span></div></div>
      <div className="health-list">
        {items.map(({ label, value, icon: Icon, ok }) => <div className="health-row" key={label}><Icon size={17} /><div><span>{label}</span><strong>{value}</strong></div><i className={ok ? "ok" : "warn"}>{ok ? <Check size={13} /> : <TriangleAlert size={13} />}</i></div>)}
      </div>
      <div className="model-strip"><span>当前模型</span><strong>{device.model}</strong></div>
    </section>
  );
}

function EventQueue({ events, onSelect }: { events: EventItem[]; onSelect: (event: EventItem) => void }) {
  return (
    <section className="panel event-queue">
      <div className="panel-head"><div><span className="eyebrow">事件闭环</span><h2>处置队列</h2></div><Button className="text-button" size="sm" variant="ghost">查看全部 <ArrowRight size={14} /></Button></div>
      <div className="queue-list">
        {events.slice(0, 3).map((event) => (
          <button className="queue-item" key={event.id} onClick={() => onSelect(event)}>
            <div className={`severity-line ${event.severity}`} />
            <div className="queue-copy"><div><strong>{event.typeLabel}</strong><StatusPill status={event.status} /></div><span><MapPinned size={13} />{event.pointName}</span><small suppressHydrationWarning>{formatAgo(event.createdAt)} · 置信度 {event.confidence}%</small></div>
            <ArrowRight size={16} />
          </button>
        ))}
        {events.length === 0 && <div className="map-loading"><Check size={18} />当前没有待处置事件</div>}
      </div>
    </section>
  );
}

function TrendPanel({ overview }: { overview: Overview }) {
  const max = Math.max(...overview.trends.events, 1);
  const peakIndex = overview.trends.events.indexOf(Math.max(...overview.trends.events, 0));
  const peakLabel = peakIndex >= 0 ? `${overview.trends.labels[peakIndex]}:00` : "暂无";
  const change = overview.kpis.todayChangePercent;
  return (
    <section className="panel trend-panel">
      <div className="panel-head"><div><span className="eyebrow">24 小时</span><h2>事件发生趋势</h2></div><Button className="chip" size="sm" variant="ghost">今日 <ChevronDown size={13} /></Button></div>
      <div className="bar-chart" role="img" aria-label="24 小时事件发生趋势柱状图">
        {overview.trends.events.map((value, index) => <div className="bar-column" key={`${overview.trends.labels[index]}-${index}`}><div className="bar-value">{value}</div><div className="bar-track"><i style={{ height: `${Math.max(12, value / max * 100)}%` }} /></div><span>{overview.trends.labels[index]}:00</span></div>)}
      </div>
      <div className="trend-summary"><span><i className="dot-blue" />峰值时段 <strong>{peakLabel}</strong></span><span>较昨日 <strong className={change >= 0 ? "up" : ""}>{change > 0 ? "+" : ""}{change}%</strong></span></div>
    </section>
  );
}

function EventTable({ events, onSelect }: { events: EventItem[]; onSelect: (event: EventItem) => void }) {
  const [filter, setFilter] = useState<"all" | EventItem["status"]>("all");
  const filtered = filter === "all" ? events : events.filter((item) => item.status === filter);
  return (
    <section className="panel event-table-panel">
      <div className="panel-head event-table-head"><div><span className="eyebrow">事件中心</span><h2>最近告警与处置记录</h2></div><div className="table-tools"><label><Search size={15} /><input placeholder="搜索事件或点位" /></label><Tooltip><Button className="icon-button" isIconOnly size="sm" variant="ghost" aria-label="筛选事件"><SlidersHorizontal size={16} /></Button><Tooltip.Content>筛选事件</Tooltip.Content></Tooltip></div></div>
      <div className="filter-row">
        {(["all", "active", "dispatched"] as const).map((item) => <button key={item} className={filter === item ? "active" : ""} onClick={() => setFilter(item)}>{({ all: "全部", active: "未接单", dispatched: "处置中" })[item]}</button>)}
      </div>
      <div className="event-table">
        <div className="event-row table-header"><span>事件</span><span>点位</span><span>置信度</span><span>发现时间</span><span>状态</span><span /></div>
        {filtered.map((event) => <button className="event-row" key={event.id} onClick={() => onSelect(event)}><span><i className={`event-type-icon ${event.severity}`}><AlertTriangle size={15} /></i><b>{event.typeLabel}</b><small>{event.id}</small></span><span><b>{event.pointName}</b><small>{event.address}</small></span><span><b>{event.confidence}%</b><small>YOLOv8</small></span><span><b suppressHydrationWarning>{formatAgo(event.createdAt)}</b><small suppressHydrationWarning>{new Date(event.createdAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</small></span><span><StatusPill status={event.status} /></span><span><ArrowRight size={16} /></span></button>)}
      </div>
    </section>
  );
}

function EventDrawer({ event, onClose, onAction }: { event: EventItem | null; onClose: () => void; onAction: (id: string, action: "dispatch" | "clear") => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  if (!event) return null;
  const managedByVolunteerTask = event.source.includes("志愿者");
  const act = async (action: "dispatch" | "clear") => { setBusy(true); try { await onAction(event.id, action); onClose(); } finally { setBusy(false); } };
  return <div className="drawer-layer" role="dialog" aria-modal="true" aria-label="事件详情"><button className="drawer-mask" aria-label="关闭" onClick={onClose} /><aside className="event-drawer"><div className="drawer-head"><div><span className="eyebrow">事件详情</span><h2>{event.typeLabel}</h2></div><button className="icon-button" onClick={onClose}><X size={18} /></button></div><div className="drawer-map-preview"><div className="pulse-marker"><span /></div><div><MapPinned size={16} /><strong>{event.pointName}</strong><span>{event.address}</span></div></div><div className="drawer-status"><StatusPill status={event.status} /><strong>{event.confidence}%</strong><span>模型置信度</span></div><dl className="event-facts"><div><dt>事件编号</dt><dd>{event.id}</dd></div><div><dt>发现时间</dt><dd>{new Date(event.createdAt).toLocaleString("zh-CN")}</dd></div><div><dt>持续时间</dt><dd>{Math.max(1, Math.floor(event.durationSec / 60))} 分钟</dd></div><div><dt>数据来源</dt><dd>{event.source}</dd></div><div><dt>坐标</dt><dd>{event.lng.toFixed(6)}, {event.lat.toFixed(6)}</dd></div></dl><div className="timeline"><h3>处置轨迹</h3><div className="timeline-item complete"><i><Check size={12} /></i><div><strong>边缘端确认事件</strong><span>连续帧命中 ROI，冻结位置与时间</span></div></div><div className="timeline-item complete"><i><Check size={12} /></i><div><strong>云端完成入库</strong><span>事件已进入监管平台</span></div></div><div className={`timeline-item ${event.status === "cleared" ? "complete" : ""}`}><i>{event.status === "cleared" ? <Check size={12} /> : <Timer size={12} />}</i><div><strong>人工处置闭环</strong><span>{event.status === "cleared" ? "现场已确认清除" : "等待处置人员反馈"}</span></div></div></div><div className="drawer-actions">{managedByVolunteerTask ? <span>该事件由 App 公共派单接单，并在志愿者审核页复核闭环。</span> : <>{event.status === "active" && <button className="secondary-action" disabled={busy} onClick={() => act("dispatch")}><Route size={16} />接单处置</button>} {event.status !== "cleared" && <button className="primary-action" disabled={busy} onClick={() => act("clear")}><ShieldCheck size={16} />确认已清除</button>}</>}</div></aside></div>;
}

function IntegrationView({ overview }: { overview: Overview }) {
  const device = overview.device;
  const sources = [
    { icon: Camera, name: "USB 摄像头", detail: "OpenCV / MJPG", status: device?.cameraStatus === "streaming" ? "已连接" : "未上报" },
    { icon: Satellite, name: "LC76G GNSS", detail: "NMEA 0183 · GCJ-02", status: device?.gpsStatus === "connected" ? "串口在线" : "未上报" },
    { icon: Cpu, name: "YOLOv8 边缘推理", detail: device ? `${device.inferenceMs} ms · ONNX Runtime` : "等待边缘设备上报", status: device ? "运行数据已接入" : "未上报" },
    { icon: Cloud, name: "视桥自有云 API", detail: "主进程直传 · Bearer REST", status: overview.linkStatus === "online" ? "可用" : "降级" },
    { icon: Database, name: "异步数据清洗", detail: overview.analysis.provider === "advantech_dify" ? "研华托管 Dify Workflow" : overview.analysis.provider, status: overview.analysis.configured ? "已配置" : "待配置" },
  ];
  const endpoints = [
    ["POST", "/api/v1/telemetry", "边缘端遥测与事件上报"],
    ["GET", "/api/v1/overview", "总览聚合数据"],
    ["GET", "/api/v1/events", "事件列表与筛选"],
    ["PATCH", "/api/v1/events/{id}", "接单、清除与闭环"],
    ["WS", "/ws/realtime", "实时状态变化推送"],
  ];
  return <div className="subpage"><div className="subpage-head"><div><span className="eyebrow">数据契约</span><h1>数据来源与开放接口</h1><p>树莓派主进程将标准化数据直接上传视桥自有云，运行链路不经过研华或其他第三方平台。</p></div><span className="architecture-badge"><ShieldCheck size={17} />边云接口已解耦</span></div><div className="integration-grid">{sources.map(({ icon: Icon, name, detail, status }) => <article className="panel source-card" key={name}><div className="source-icon"><Icon size={21} /></div><div><strong>{name}</strong><span>{detail}</span></div><em><i />{status}</em></article>)}</div><section className="panel flow-panel"><div className="panel-head"><div><span className="eyebrow">标准链路</span><h2>端到云数据流</h2></div></div><div className="data-flow"><div><Camera /><strong>现场感知</strong><span>图像 + GNSS</span></div><ArrowRight /><div><Cpu /><strong>边缘计算</strong><span>识别 + 状态机</span></div><ArrowRight /><div><Radio /><strong>自有云直传</strong><span>Bearer REST</span></div><ArrowRight /><div><Database /><strong>事件存储</strong><span>SQLite / API</span></div><ArrowRight /><div><BarChart3 /><strong>监管界面</strong><span>地图 + 闭环</span></div></div></section><section className="panel api-panel"><div className="panel-head"><div><span className="eyebrow">API v1</span><h2>一期开放接口</h2></div><span className="api-base">同源 /api/v1</span></div><div className="endpoint-list">{endpoints.map(([method, path, desc]) => <div key={path}><code className={`method method-${method.toLowerCase()}`}>{method}</code><code>{path}</code><span>{desc}</span></div>)}</div></section></div>;
}

export function VisionBridgeDashboard() {
  const [view, setView] = useState<View>("overview");
  const [overview, setOverview] = useState<Overview>(emptyOverview);
  const [config, setConfig] = useState<PublicConfig>({ defaultCenter: [121.138923, 28.632112] });
  const [selectedEvent, setSelectedEvent] = useState<EventItem | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [dataError, setDataError] = useState("");
  const [realtimeConnected, setRealtimeConnected] = useState(false);
  const [lastUpdated, setLastUpdated] = useState(new Date());
  const refreshSequence = useRef(0);

  const refresh = useCallback(async () => {
    const sequence = ++refreshSequence.current;
    try {
      const nextOverview = await fetchJson<Overview>("/api/v1/overview");
      if (sequence !== refreshSequence.current) return;
      setOverview(nextOverview);
      setDataError("");
      setLastUpdated(new Date());
    } catch (reason) {
      if (sequence !== refreshSequence.current) return;
      setDataError(reason instanceof Error ? reason.message : "实时数据暂不可用");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => {
    void fetchJson<PublicConfig>("/api/v1/config/public").then(setConfig).catch(() => undefined);
    const initial = window.setTimeout(() => void refresh(), 0);
    const timer = window.setInterval(() => void refresh(), 15000);
    const onVisibility = () => { if (document.visibilityState === "visible") void refresh(); };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", onVisibility);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", onVisibility);
    };
  }, [refresh]);

  useEffect(() => {
    let stopped = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    const connect = () => {
      const endpoint = new URL("/ws/realtime", window.location.href);
      endpoint.protocol = endpoint.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(endpoint);
      socket.onopen = () => setRealtimeConnected(true);
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(String(message.data)) as { type?: string };
          if (event.type !== "heartbeat") void refresh();
        } catch { /* Ignore malformed frames and keep the last verified snapshot. */ }
      };
      socket.onerror = () => socket?.close();
      socket.onclose = () => {
        setRealtimeConnected(false);
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

  const actionEvent = useCallback(async (id: string, action: "dispatch" | "clear") => {
    try {
      await fetchJson(`/api/v1/events/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) });
      await refresh();
    } catch { await refresh(); }
  }, [refresh]);

  const currentLabel = useMemo(() => navItems.find((item) => item.id === view)?.label, [view]);
  const eventComposition = useMemo(() => {
    const counts = new Map<string, number>();
    for (const event of overview.recentEvents) counts.set(event.typeLabel, (counts.get(event.typeLabel) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3);
  }, [overview.recentEvents]);
  const inferenceValues = overview.trends.inferenceMs.filter((value): value is number => value !== null);
  const cameraValues = overview.trends.cameraFps.filter((value): value is number => value !== null);
  const chooseView = (next: View) => { setView(next); setMenuOpen(false); window.scrollTo({ top: 0, behavior: "smooth" }); };

  return <div className="app-shell">
    <aside className={`sidebar ${menuOpen ? "open" : ""}`}>
      <div className="brand"><div className="brand-mark"><Route size={24} /></div><div><strong>视桥</strong><span>VISIONBRIDGE</span></div></div>
      <nav>{navItems.map(({ id, label, icon: Icon }) => <button key={id} className={view === id ? "active" : ""} onClick={() => chooseView(id)}><Icon size={18} /><span>{label}</span>{id === "events" && overview.kpis.activeEvents > 0 && <em>{overview.kpis.activeEvents}</em>}</button>)}</nav>
       <div className="sidebar-system"><span>系统链路</span><div className="mini-flow"><i className={overview.device?.status === "online" ? "on" : ""} /><b>端</b><span /><i className={!dataError ? "on" : ""} /><b>云</b><span /><i className={realtimeConnected ? "on" : ""} /><b>屏</b></div><small>{realtimeConnected ? "实时推送已连接" : "实时推送重连中，15 秒轮询兜底"}</small></div>
      <div className="competition-tag"><span>ADVANTECH</span><strong>2026 研华 AIoT 大赛</strong></div>
    </aside>
    {menuOpen && <button className="mobile-mask" onClick={() => setMenuOpen(false)} aria-label="关闭导航" />}
    <div className="workspace">
       <header className="topbar"><div className="topbar-left"><Button className="mobile-menu" isIconOnly size="sm" variant="ghost" onClick={() => setMenuOpen(true)} aria-label="打开导航"><Menu size={19} /></Button><span>{currentLabel}</span><i /> <small>城市无障碍设施智能监管</small></div><div className="topbar-right"><Chip className={`connection-state state-${dataError ? "offline" : overview.linkStatus}`} size="sm" variant="soft"><i />{dataError ? "数据接口异常" : realtimeConnected ? "实时推送正常" : "轮询同步中"}</Chip><Tooltip><Button className="icon-button" isIconOnly size="sm" variant="ghost" onClick={refresh} aria-label="刷新数据"><RefreshCw size={16} className={loading ? "spin" : ""} /></Button><Tooltip.Content>刷新数据</Tooltip.Content></Tooltip><div className="time-block"><strong suppressHydrationWarning>{new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</strong><span suppressHydrationWarning>{lastUpdated.toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "short" })}</span></div><Avatar className="avatar" size="sm"><Avatar.Fallback>管</Avatar.Fallback></Avatar></div></header>
      <main>
        {dataError && <div className="data-notice"><TriangleAlert size={16} />{dataError}。页面保留最后一次成功同步的数据，并自动重试。</div>}
        {view === "overview" && <>
          <section className="hero-line">
            <div className="hero-copy"><span className="eyebrow">城市盲道智能监管平台</span><h1>城市盲道通行态势</h1><p>从边缘识别到现场处置，持续追踪每一处影响安全通行的占用。</p></div>
            <div className="hero-signal">
              <div className="signal-orbit" aria-hidden="true"><i className="orbit-ring ring-one" /><i className="orbit-ring ring-two" /><span className="orbit-dot dot-one" /><span className="orbit-dot dot-two" /><span className="orbit-dot dot-three" /><div className="hero-signal-count"><span>当前需处置</span><strong>{overview.kpis.activeEvents}<small>起</small></strong></div></div>
              <div className="source-badges"><span><Radio size={14} />边缘设备实机</span><span><Cloud size={14} />自有云接入</span><span className={`mode-${overview.dataMode}`}>{overview.dataMode === "live" ? "真实入库数据" : "等待真实数据"}</span></div>
            </div>
          </section>
          <section className="metric-grid"><MetricCard icon={Signal} label="在线终端" value={`${overview.kpis.onlineDevices}/${overview.kpis.totalDevices}`} detail={overview.kpis.totalDevices ? "来自设备心跳" : "尚无设备上报"} tone="cyan" /><MetricCard icon={AlertTriangle} label="活动事件" value={overview.kpis.activeEvents} detail="需要关注与处置" tone="orange" /><MetricCard icon={Activity} label="今日事件" value={overview.kpis.todayEvents} detail={`较昨日 ${overview.kpis.todayChangePercent > 0 ? "+" : ""}${overview.kpis.todayChangePercent}%`} tone="blue" /><MetricCard icon={ShieldCheck} label="闭环率" value={overview.kpis.closureRate} suffix="%" detail={overview.kpis.averageResponseMin === null ? "暂无接单响应样本" : `平均响应 ${overview.kpis.averageResponseMin} 分钟`} tone="green" /></section>
          <div className="primary-grid"><MapStage config={config} overview={overview} onEvent={setSelectedEvent} /><div className="right-stack"><DeviceHealth device={overview.device} /><EventQueue events={overview.recentEvents} onSelect={setSelectedEvent} /></div></div>
          <div className="secondary-grid"><TrendPanel overview={overview} /><section className="panel insight-panel"><div className="panel-head"><div><span className="eyebrow">风险洞察</span><h2>活动事件类型构成</h2></div><CircleGauge size={20} /></div><div className="donut-wrap"><div className="css-donut"><div><strong>{overview.recentEvents.length}</strong><span>活动事件</span></div></div><div className="legend-list">{eventComposition.map(([label, count], index) => <span key={label}><i className={`risk-${String.fromCharCode(97 + index)}`} /><b>{label}</b><strong>{count}</strong></span>)}{eventComposition.length === 0 && <span><b>暂无活动事件</b></span>}</div></div><div className="insight-note"><Database size={15} /><span>统计仅来自当前已入库的真实活动事件。</span></div></section></div>
          <EventTable events={overview.recentEvents} onSelect={setSelectedEvent} />
        </>}
        {view === "events" && <div className="subpage"><div className="subpage-head"><div><span className="eyebrow">活动事件</span><h1>事件中心</h1><p>这里只展示未接单和处置中的活动障碍；复核通过后自动移出。</p></div><button className="primary-action"><BellRing size={16} />告警策略</button></div><EventTable events={overview.recentEvents} onSelect={setSelectedEvent} /></div>}
        {view === "devices" && <DeviceFleetView fallbackDevice={overview.device} />}
        {view === "analytics" && <div className="subpage"><div className="subpage-head"><div><span className="eyebrow">ANALYTICS</span><h1>趋势分析</h1><p>观察事件高发时段、边缘性能和设备稳定性。</p></div><button className="chip">今日 3 小时分桶 <ChevronDown size={13} /></button></div><div className="analytics-grid"><TrendPanel overview={overview} /><section className="panel performance-panel"><div className="panel-head"><div><span className="eyebrow">边缘性能</span><h2>推理延迟与帧率</h2></div></div><div className="performance-kpis"><div><span>平均推理延迟</span><strong>{inferenceValues.length ? Math.round(inferenceValues.reduce((a,b)=>a+b,0)/inferenceValues.length) : "—"} <small>ms</small></strong></div><div><span>平均采集帧率</span><strong>{cameraValues.length ? (cameraValues.reduce((a,b)=>a+b,0)/cameraValues.length).toFixed(1) : "—"} <small>FPS</small></strong></div></div><div className="latency-list">{overview.trends.labels.map((label,index)=>{const value=overview.trends.inferenceMs[index]; return <div key={label}><span>{label}:00</span><i><b style={{width:`${value === null ? 0 : Math.min(100,value/12)}%`}} /></i><strong>{value === null ? "无样本" : `${value} ms`}</strong></div>;})}</div></section></div></div>}
        {view === "volunteers" && <VolunteerAdminView />}
        {view === "settings" && <IntegrationView overview={overview} />}
      </main>
    </div>
    <EventDrawer event={selectedEvent} onClose={() => setSelectedEvent(null)} onAction={actionEvent} />
  </div>;
}
