import argparse
import base64
from collections import deque
import datetime
import glob
import json
import math
import os
import pathlib
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid

from detection_state import OccupancyState
import cv2
import numpy as np
import pynmea2
import serial
from flask import Flask, Response, request, send_from_directory
from werkzeug.serving import make_server


CLASS_NAMES = {
    0: "person",
    1: "bicycle",
    2: "motorcycle",
    3: "obstacle_other",
    4: "traffic_light_red",
    5: "traffic_light_green",
    6: "zebra_crossing",
}

DEFAULT_EVENT_MAPPING = {
    "bicycle": "non_motor_vehicle",
    "motorcycle": "motor_vehicle",
    "obstacle_other": "construction_obstacle",
}

CAMERA_STATUS_CODES = {
    "init": 0,
    "streaming": 1,
    "read_failed": 2,
}

GPS_STATUS_CODES = {
    "disconnected": 0,
    "ok": 1,
    "connected": 1,
    "connecting": 2,
    "no_port": 3,
    "error": 4,
}

EDGE_STATUS_CODES = {
    "init": 0,
    "running": 1,
    "alert": 2,
    "camera_error": 3,
}

OBSTACLE_TYPE_CODES = {
    "": 0,
    "non_motor_vehicle": 1,
    "motor_vehicle": 2,
    "construction_obstacle": 3,
}

MODEL_CLASS_CODES = {
    "": 0,
    "person": 1,
    "bicycle": 2,
    "motorcycle": 3,
    "obstacle_other": 4,
    "traffic_light_red": 5,
    "traffic_light_green": 6,
    "zebra_crossing": 7,
}

ALERT_LEVEL_CODES = {
    "normal": 0,
    "attention": 1,
    "warning": 2,
    "critical": 3,
}

DISPATCH_STATUS_CODES = {
    "disabled": 0,
    "idle": 1,
    "running": 2,
    "succeeded": 3,
    "failed": 4,
}

DISPATCH_ACTION_CODES = {
    "": 0,
    "continue_patrol": 1,
    "manual_review": 2,
    "dispatch_now": 3,
}

DISPATCH_PRIORITY_CODES = {
    "": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}

GPS_PORT_CANDIDATES = [
    "/dev/ttyUSB1",
    "/dev/ttyUSB0",
    "/dev/ttyACM0",
    "/dev/ttyS0",
    "/dev/ttyAMA0",
    "/dev/ttyAMA10",
]


def iso_ts():
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz).isoformat(timespec="seconds")


def today_text():
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz).strftime("%Y-%m-%d")


def epoch_ts():
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return int(datetime.datetime.now(tz).timestamp())


def http_json_request(url, *, payload=None, headers=None, timeout=10):
    request_headers = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    if not raw:
        return {}
    return json.loads(raw)


class DirectCloudUploader:
    """Asynchronously uploads edge telemetry straight to VisionBridge Cloud."""

    def __init__(self, url, token, timeout_sec=12, queue_size=128):
        self.url = str(url or "").strip()
        self.token = str(token or "").strip()
        self.timeout_sec = float(timeout_sec)
        self.pending = queue.Queue(maxsize=max(8, int(queue_size)))
        self.stop_event = threading.Event()
        self.thread = None

    @property
    def enabled(self):
        return bool(self.url and self.token)

    def start(self):
        if not self.enabled:
            raise RuntimeError("VisionBridge cloud URL/token is required")
        self.thread = threading.Thread(target=self._run, name="visionbridge-cloud-uploader", daemon=True)
        self.thread.start()

    def submit(self, payload):
        try:
            self.pending.put_nowait(payload)
        except queue.Full:
            # Keep inference non-blocking. Discard the oldest heartbeat when a
            # prolonged network outage fills the queue, then retain fresh state.
            try:
                self.pending.get_nowait()
                self.pending.task_done()
            except queue.Empty:
                pass
            self.pending.put_nowait(payload)

    def _post(self, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "VisionBridgeEdge/2.0",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as response:
            if response.status not in (200, 201, 202):
                raise RuntimeError(f"cloud returned HTTP {response.status}")

    def _run(self):
        while not self.stop_event.is_set():
            try:
                payload = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            failures = 0
            while not self.stop_event.is_set():
                try:
                    self._post(payload)
                    if failures:
                        print("[CLOUD] direct link restored")
                    print(f"[CLOUD] accepted telemetry ts={payload.get('ts', '')}")
                    break
                except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
                    failures += 1
                    delay = min(60, 2 ** min(failures, 6))
                    print(f"[CLOUD] upload failed ({failures}), retry in {delay}s: {exc}")
                    if self.stop_event.wait(delay):
                        break
            self.pending.task_done()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3.0)


class WorkflowDispatchClient:
    def __init__(self, api_base_url, api_key, user, timeout_sec=20):
        self.api_base_url = str(api_base_url or "").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.user = str(user or "").strip() or "blind-occupancy-edge"
        self.timeout_sec = float(timeout_sec)

    @property
    def enabled(self):
        return bool(self.api_base_url and self.api_key)

    def _extract_json_text(self, raw_text):
        text = str(raw_text or "").strip()
        if not text:
            raise RuntimeError("workflow output is empty")
        if "</think>" in text:
            text = text.split("</think>", 1)[1].strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError(f"workflow output is not JSON: {text[:200]}")
        return text[start : end + 1]

    def _normalize(self, dispatch, inputs):
        action = str(dispatch.get("action") or "").strip().lower()
        priority = str(dispatch.get("priority") or "").strip().lower()
        if action not in DISPATCH_ACTION_CODES:
            action = "dispatch_now" if str(inputs.get("eventActive", "")) == "1" else "continue_patrol"
        if priority not in DISPATCH_PRIORITY_CODES:
            priority = {
                "dispatch_now": "high",
                "manual_review": "medium",
                "continue_patrol": "low",
            }.get(action, "low")
        return {
            "action": action,
            "priority": priority,
            "summary": str(dispatch.get("summary") or "").strip(),
            "suggestion": str(dispatch.get("suggestion") or "").strip(),
            "deviceId": str(dispatch.get("deviceId") or inputs.get("deviceId") or "").strip(),
            "timestamp": str(dispatch.get("timestamp") or inputs.get("timestamp") or "").strip(),
            "gpsLat": str(dispatch.get("gpsLat") or inputs.get("gpsLat") or "").strip(),
            "gpsLng": str(dispatch.get("gpsLng") or inputs.get("gpsLng") or "").strip(),
            "snapshotUrl": str(dispatch.get("snapshotUrl") or inputs.get("snapshotUrl") or "").strip(),
        }

    def run(self, inputs):
        if not self.enabled:
            raise RuntimeError("workflow dispatch client is disabled")
        payload = {
            "inputs": dict(inputs),
            "response_mode": "blocking",
            "user": self.user,
        }
        response = http_json_request(
            f"{self.api_base_url}/workflows/run",
            payload=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout_sec,
        )
        outputs = ((response.get("data") or {}).get("outputs") or {})
        dispatch_raw = outputs.get("dispatchResult")
        if dispatch_raw is None:
            raise RuntimeError(f"workflow outputs missing dispatchResult: {outputs}")
        if isinstance(dispatch_raw, dict):
            dispatch = dispatch_raw
        else:
            dispatch = json.loads(self._extract_json_text(dispatch_raw))
        return self._normalize(dispatch, inputs)


def make_property(value, data_type):
    return {
        "value": value,
        "dataType": data_type,
    }


def encode_status_code(raw_value, prefix, mapping):
    if raw_value.startswith(prefix):
        return mapping.get(prefix, 0)
    return mapping.get(raw_value, 0)


def scaled_int(value, factor=1.0):
    if value is None:
        return 0
    return int(round(float(value) * factor))


def clamp_box(box, width, height):
    x1, y1, x2, y2 = [int(v) for v in box]
    x1 = max(0, min(x1, width - 1))
    y1 = max(0, min(y1, height - 1))
    x2 = max(0, min(x2, width - 1))
    y2 = max(0, min(y2, height - 1))
    return [x1, y1, x2, y2]


def out_of_china(lng, lat):
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def transform_lat(lng, lat):
    ret = -100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat + 0.1 * lng * lat + 0.2 * math.sqrt(abs(lng))
    ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * math.pi) + 320.0 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def transform_lng(lng, lat):
    ret = 300.0 + lng + 2.0 * lat + 0.1 * lng * lng + 0.1 * lng * lat + 0.1 * math.sqrt(abs(lng))
    ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lng * math.pi) + 40.0 * math.sin(lng / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lng / 12.0 * math.pi) + 300.0 * math.sin(lng / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lng, lat):
    if out_of_china(lng, lat):
        return lng, lat
    a = 6378245.0
    ee = 0.00669342162296594323
    dlat = transform_lat(lng - 105.0, lat - 35.0)
    dlng = transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng, lat):
    if out_of_china(lng, lat):
        return lng, lat
    gcj_lng, gcj_lat = wgs84_to_gcj02(lng, lat)
    return lng * 2 - gcj_lng, lat * 2 - gcj_lat


def haversine_meters(lat1, lng1, lat2, lng2):
    radius = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class PedestrianGPSFilter:
    def __init__(self, alpha=0.3, max_jump_meters=15.0):
        self.alpha = alpha
        self.max_jump_meters = max_jump_meters
        self.lat = None
        self.lng = None

    def update(self, lat, lng):
        if self.lat is None or self.lng is None:
            self.lat = lat
            self.lng = lng
            return lat, lng
        if haversine_meters(self.lat, self.lng, lat, lng) > self.max_jump_meters:
            return self.lat, self.lng
        self.lat = self.alpha * lat + (1 - self.alpha) * self.lat
        self.lng = self.alpha * lng + (1 - self.alpha) * self.lng
        return self.lat, self.lng


class SerialGPSProvider:
    def __init__(self, baudrate=115200, coord_system="gcj02"):
        self.baudrate = baudrate
        self.coord_system = coord_system
        self.filter = PedestrianGPSFilter()
        self.data = {
            "status": "disconnected",
            "port": "",
            "lat": None,
            "lng": None,
            "raw_wgs84_lat": None,
            "raw_wgs84_lng": None,
            "speed_kmh": 0.0,
            "num_sats": 0,
            "hdop": 0.0,
            "altitude": 0.0,
            "angle": 0.0,
            "raw_nmea": "",
            "coord_system": coord_system,
            "updated_at": 0.0,
        }
        self._lock = threading.Lock()
        self._thread = None
        self._running = False

    def _candidate_ports(self):
        seen = set()
        ports = []
        for pat in GPS_PORT_CANDIDATES:
            for port in glob.glob(pat):
                if port not in seen:
                    ports.append(port)
                    seen.add(port)
        return ports

    def _detect_port(self):
        for port in self._candidate_ports():
            try:
                with serial.Serial(port, self.baudrate, timeout=1) as ser:
                    for _ in range(8):
                        line = ser.readline().decode("ascii", errors="ignore").strip()
                        if line.startswith("$GN") or line.startswith("$GP"):
                            print(f"[GPS] using port {port}")
                            return port
            except Exception:
                continue
        return None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def snapshot(self):
        with self._lock:
            return dict(self.data)

    def _update_data(self, **kwargs):
        with self._lock:
            self.data.update(kwargs)

    def _handle_line(self, line):
        if not line.startswith("$"):
            return
        self._update_data(raw_nmea=line)
        try:
            msg = pynmea2.parse(line)
        except pynmea2.ParseError:
            return

        updates = {}
        if getattr(msg, "sentence_type", "") == "RMC" and getattr(msg, "status", "") == "A":
            raw_lat = getattr(msg, "latitude", None)
            raw_lng = getattr(msg, "longitude", None)
            if raw_lat and raw_lng:
                lng, lat = raw_lng, raw_lat
                if self.coord_system.lower() == "gcj02":
                    lng, lat = wgs84_to_gcj02(raw_lng, raw_lat)
                lat, lng = self.filter.update(lat, lng)
                updates.update(
                    {
                        "lat": lat,
                        "lng": lng,
                        "raw_wgs84_lat": raw_lat,
                        "raw_wgs84_lng": raw_lng,
                        "speed_kmh": float(getattr(msg, "spd_over_grnd", 0.0) or 0.0) * 1.852,
                        "angle": float(getattr(msg, "true_course", 0.0) or 0.0),
                        "status": "ok",
                        "updated_at": time.time(),
                    }
                )
        if getattr(msg, "sentence_type", "") == "GGA":
            updates.update(
                {
                    "num_sats": int(getattr(msg, "num_sats", 0) or 0),
                    "hdop": float(getattr(msg, "horizontal_dil", 0.0) or 0.0),
                    "altitude": float(getattr(msg, "altitude", 0.0) or 0.0),
                }
            )
        if updates:
            self._update_data(**updates)

    def _read_loop(self):
        while self._running:
            port = self._detect_port()
            if not port:
                self._update_data(status="no_port")
                time.sleep(2)
                continue
            self._update_data(port=port, status="connecting")
            try:
                with serial.Serial(port, self.baudrate, timeout=1) as ser:
                    self._update_data(status="connected", port=port)
                    while self._running:
                        line = ser.readline().decode("ascii", errors="ignore").strip()
                        if line:
                            self._handle_line(line)
            except Exception as exc:
                print(f"[GPS] port {port} error: {exc}")
                self._update_data(status="error")
                time.sleep(2)


def build_default_roi(frame_w, frame_h):
    top_y = int(frame_h * 0.55)
    bottom_y = int(frame_h * 0.94)
    top_left = [int(frame_w * 0.25), top_y]
    top_right = [int(frame_w * 0.75), top_y]
    bottom_right = [int(frame_w * 0.95), bottom_y]
    bottom_left = [int(frame_w * 0.05), bottom_y]
    return np.array([top_left, top_right, bottom_right, bottom_left], dtype=np.int32)


def point_in_roi(roi_pts, point):
    return cv2.pointPolygonTest(roi_pts.reshape((-1, 1, 2)), point, False) >= 0


class YOLOv8ONNXDetector:
    def __init__(self, model_path, input_size=640, conf_threshold=0.35, iou_threshold=0.45):
        self.model_path = model_path
        self.input_size = int(input_size)
        self.conf_threshold = float(conf_threshold)
        self.iou_threshold = float(iou_threshold)
        self.net = cv2.dnn.readNetFromONNX(model_path)
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

    def _letterbox(self, image):
        h, w = image.shape[:2]
        scale = min(self.input_size / w, self.input_size / h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        resized = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        dw = (self.input_size - nw) // 2
        dh = (self.input_size - nh) // 2
        canvas[dh : dh + nh, dw : dw + nw] = resized
        return canvas, scale, dw, dh

    def detect(self, image):
        blob_img, scale, dw, dh = self._letterbox(image)
        blob = cv2.dnn.blobFromImage(blob_img, 1 / 255.0, (self.input_size, self.input_size), swapRB=True, crop=False)
        self.net.setInput(blob)
        outputs = self.net.forward()
        preds = outputs[0]
        if preds.ndim == 3:
            preds = preds[0]
        if preds.shape[0] < preds.shape[1]:
            preds = preds.T

        boxes = []
        scores = []
        class_ids = []
        for row in preds:
            class_scores = row[4:]
            class_id = int(np.argmax(class_scores))
            score = float(class_scores[class_id])
            if score < self.conf_threshold:
                continue
            cx, cy, w, h = row[:4]
            x = (cx - w / 2 - dw) / scale
            y = (cy - h / 2 - dh) / scale
            bw = w / scale
            bh = h / scale
            boxes.append([int(x), int(y), int(bw), int(bh)])
            scores.append(score)
            class_ids.append(class_id)

        detections = []
        if boxes:
            indices = cv2.dnn.NMSBoxes(boxes, scores, self.conf_threshold, self.iou_threshold)
            if len(indices) > 0:
                for idx in np.array(indices).reshape(-1):
                    x, y, w, h = boxes[int(idx)]
                    detections.append(
                        {
                            "class_id": class_ids[int(idx)],
                            "class_name": CLASS_NAMES.get(class_ids[int(idx)], f"class_{class_ids[int(idx)]}"),
                            "score": scores[int(idx)],
                            "box": [x, y, x + w, y + h],
                        }
                    )
        return detections


def open_camera(preferred=None, width=640, height=360, fps=15, use_mjpg=True, buffer_size=1):
    candidates = []
    if preferred:
        candidates.append(preferred)
    candidates.extend(["/dev/video0", "/dev/video1", 0, 1])

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        cap = cv2.VideoCapture(candidate, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            continue
        if int(buffer_size) > 0:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, int(buffer_size))
        if use_mjpg:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        ok, frame = cap.read()
        if ok and frame is not None:
            print(f"[CAMERA] using {candidate} {frame.shape}")
            return cap, str(candidate)
        cap.release()
    raise RuntimeError("unable to open USB camera")


class CameraReader:
    def __init__(self, preferred=None, width=640, height=360, fps=15, use_mjpg=True, buffer_size=1):
        self.preferred = preferred
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.use_mjpg = bool(use_mjpg)
        self.buffer_size = int(buffer_size)
        self.cap = None
        self.label = ""
        self._frame = None
        self._frame_id = 0
        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        self._read_count = 0

    def start(self):
        if self._running:
            return
        self.cap, self.label = open_camera(
            preferred=self.preferred,
            width=self.width,
            height=self.height,
            fps=self.fps,
            use_mjpg=self.use_mjpg,
            buffer_size=self.buffer_size,
        )
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        while self._running:
            ok, frame = self.cap.read()
            if not ok or frame is None:
                time.sleep(0.02)
                continue
            with self._lock:
                self._frame = frame
                self._read_count += 1
                self._frame_id = self._read_count

    def snapshot(self):
        frame, _ = self.snapshot_with_id()
        return frame

    def snapshot_with_id(self):
        with self._lock:
            if self._frame is None:
                return None, 0
            return self._frame.copy(), self._frame_id

    def read_count(self):
        with self._lock:
            return self._read_count

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self.cap:
            self.cap.release()


class BlindOccupancyEdgeApp:
    def __init__(self, args):
        self.args = args
        self.event_mapping = dict(DEFAULT_EVENT_MAPPING)
        self.detector = YOLOv8ONNXDetector(
            model_path=args.model_path,
            input_size=args.input_size,
            conf_threshold=args.conf_threshold,
            iou_threshold=args.iou_threshold,
        )
        self.gps = SerialGPSProvider(coord_system=args.gps_coord_system)
        self.gps.start()
        self.cloud = DirectCloudUploader(
            url=args.cloud_url,
            token=args.cloud_token,
            timeout_sec=args.cloud_timeout_sec,
            queue_size=args.cloud_queue_size,
        )
        self.event_count = 0
        self.daily_event_count = 0
        self.last_event_date = today_text()
        self.last_event_ts = 0.0
        self.last_heartbeat_ts = 0.0
        self.violation_streak = 0
        self.active_event = None
        self.occupancy_state = OccupancyState(
            window_frames=args.confirm_window_frames,
            required_hits=args.confirm_required_hits,
            min_confirm_duration_sec=args.confirm_min_duration_sec,
            clear_miss_frames=args.clear_miss_frames,
            clear_duration_sec=args.clear_duration_sec,
            evidence_interval_sec=args.evidence_interval_sec,
            cooldown_sec=args.event_cooldown,
            spatial_dedup_meters=args.spatial_dedup_meters,
        )
        self.last_runtime = {
            "camera_fps": 0.0,
            "inference_ms": 0,
            "inference_fps": 0.0,
            "inference_period_ms": 0,
            "roi_occupied": 0,
            "camera_status": "init",
            "edge_status": "init",
            "alert_level": "normal",
            "last_event_id": "",
            "event_state": "idle",
            "last_snapshot_name": "",
            "last_obstacle_type": "",
            "last_raw_class_name": "",
            "last_confidence": 0,
            "last_event_epoch": 0,
            "snapshot_ready": 0,
            "detections_in_frame": 0,
            "target_detections_in_frame": 0,
            "trigger_streak": 0,
            "dispatch_status": "idle" if bool(args.workflow_api_key) else "disabled",
            "dispatch_action": "",
            "dispatch_priority": "",
            "dispatch_summary": "",
            "last_detections": [],
        }
        self.snapshots_dir = pathlib.Path(args.snapshots_dir)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.gps_history = deque(maxlen=int(args.gps_history_max_points))
        self.event_history = deque(maxlen=int(args.event_history_max_points))
        self.history_lock = threading.Lock()
        self.last_gps_sample_ts = 0.0
        self.preview_lock = threading.Lock()
        self.preview_jpeg = b""
        self.preview_raw_frame = b""
        self.preview_raw_sequence = 0
        self.preview_server = None
        self.preview_thread = None
        self.preview_pump_thread = None
        self.running = True
        self.camera = None
        self.dispatch_client = WorkflowDispatchClient(
            api_base_url=args.workflow_api_base_url,
            api_key=args.workflow_api_key,
            user=args.workflow_user or args.device_id,
            timeout_sec=args.workflow_timeout_sec,
        )
        self.dispatch_lock = threading.Lock()
        self.last_dispatch = {
            "enabled": 1 if self.dispatch_client.enabled else 0,
            "status": "idle" if self.dispatch_client.enabled else "disabled",
            "event_id": "",
            "action": "",
            "priority": "",
            "summary": "",
            "suggestion": "",
            "updated_at": "",
            "error": "",
        }
        self.last_notification = {
            "received": 0,
            "received_at": "",
            "content_type": "",
            "payload": None,
        }

    def start_preview_server(self):
        if not self.args.web_preview:
            return

        app = Flask(__name__)

        @app.after_request
        def add_cors_headers(response):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            return response

        @app.route("/")
        def index():
            return (
                "<html><body style='margin:0;background:#111;color:#eee;font-family:Arial'>"
                "<div style='padding:8px 12px'>blind_occupancy_edge preview</div>"
                "<div style='padding:0 12px 8px'><a href='/status.json' "
                "style='color:#7dd3fc;text-decoration:none'>runtime status json</a></div>"
                "<div style='padding:0 12px 8px'><a href='/gps-heatmap' "
                "style='color:#86efac;text-decoration:none'>gps route + hotspot map</a></div>"
                "<img src='/stream.mjpg' style='width:100%;height:auto;display:block' />"
                "</body></html>"
            )

        @app.route("/status.json")
        def status_json():
            body = json.dumps(
                self.build_status_snapshot(external_host=request.host),
                ensure_ascii=False,
            )
            return Response(body, mimetype="application/json")

        @app.route("/notify-hook", methods=["POST"])
        def notify_hook():
            raw_text = request.get_data(cache=False, as_text=True)
            payload = request.get_json(silent=True)
            if raw_text:
                if payload is None:
                    try:
                        payload = json.loads(raw_text)
                    except json.JSONDecodeError:
                        payload = {"raw_text": raw_text}
            self.last_notification = {
                "received": 1,
                "received_at": iso_ts(),
                "content_type": request.content_type or "",
                "payload": payload,
            }
            body = json.dumps(
                {
                    "ok": 1,
                    "received_at": self.last_notification["received_at"],
                },
                ensure_ascii=False,
            )
            return Response(body, mimetype="application/json")

        @app.route("/snapshots/<path:filename>")
        def snapshot_file(filename):
            return send_from_directory(str(self.snapshots_dir), filename)

        @app.route("/gps-heatmap")
        def gps_heatmap_page():
            return Response(self.build_heatmap_html(), mimetype="text/html")

        @app.route("/gps-heatmap.json")
        def gps_heatmap_json():
            body = json.dumps(
                self.build_heatmap_payload(external_host=request.host),
                ensure_ascii=False,
            )
            return Response(body, mimetype="application/json")

        @app.route("/gps-czml.json")
        def gps_czml_json():
            body = json.dumps(
                self.build_czml_payload(),
                ensure_ascii=False,
            )
            return Response(body, mimetype="application/json")

        @app.route("/search", methods=["GET", "POST"])
        def simplejson_search():
            body = json.dumps(self.build_simplejson_search(), ensure_ascii=False)
            return Response(body, mimetype="application/json")

        @app.route("/query", methods=["POST"])
        def simplejson_query():
            payload = request.get_json(silent=True) or {}
            body = json.dumps(
                self.build_simplejson_query(payload),
                ensure_ascii=False,
            )
            return Response(body, mimetype="application/json")

        @app.route("/annotations", methods=["POST"])
        def simplejson_annotations():
            return Response("[]", mimetype="application/json")

        @app.route("/stream.mjpg")
        def stream():
            def generate():
                last = None
                while True:
                    with self.preview_lock:
                        frame = self.preview_jpeg
                    if frame and frame != last:
                        last = frame
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                        )
                    time.sleep(0.05)

            return Response(
                generate(),
                mimetype="multipart/x-mixed-replace; boundary=frame",
            )

        @app.route("/stream.raw")
        def raw_stream():
            def generate():
                last_sequence = -1
                while self.running:
                    with self.preview_lock:
                        frame = self.preview_raw_frame
                        sequence = self.preview_raw_sequence
                    if frame and sequence != last_sequence:
                        last_sequence = sequence
                        yield frame
                    else:
                        time.sleep(0.002)

            return Response(
                generate(),
                mimetype="application/octet-stream",
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
            )

        self.preview_server = make_server(
            self.args.web_preview_host,
            self.args.web_preview_port,
            app,
            threaded=True,
        )
        self.preview_thread = threading.Thread(
            target=self.preview_server.serve_forever,
            daemon=True,
        )
        self.preview_thread.start()
        print(
            f"[PREVIEW] http://{self.args.web_preview_host}:{self.args.web_preview_port}/"
        )
        self.preview_pump_thread = threading.Thread(
            target=self.preview_pump_loop,
            daemon=True,
        )
        self.preview_pump_thread.start()

    def preview_pump_loop(self):
        interval = 1.0 / max(self.args.web_preview_fps, 1)
        jpeg_interval = 1.0 / max(self.args.web_preview_jpeg_fps, 1)
        last_jpeg_ts = 0.0
        last_frame_id = -1
        while self.running:
            if not self.camera:
                time.sleep(0.1)
                continue
            frame, frame_id = self.camera.snapshot_with_id()
            if frame is None:
                time.sleep(0.02)
                continue
            if frame_id == last_frame_id:
                time.sleep(0.005)
                continue
            last_frame_id = frame_id
            annotated = self.annotate_frame(frame)
            if self.args.web_preview_max_width > 0 and annotated.shape[1] > self.args.web_preview_max_width:
                new_h = int(round(annotated.shape[0] * self.args.web_preview_max_width / annotated.shape[1]))
                annotated = cv2.resize(annotated, (self.args.web_preview_max_width, new_h), interpolation=cv2.INTER_AREA)
            self.update_preview_raw_frame(annotated)
            now = time.time()
            if now - last_jpeg_ts >= jpeg_interval:
                self.update_preview_frame(annotated)
                last_jpeg_ts = now
            time.sleep(interval)

    def update_preview_raw_frame(self, frame):
        if not self.args.web_preview:
            return
        raw = np.ascontiguousarray(frame).tobytes()
        with self.preview_lock:
            self.preview_raw_frame = raw
            self.preview_raw_sequence += 1

    def update_preview_frame(self, frame):
        if not self.args.web_preview:
            return
        ok, encoded = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(self.args.web_preview_quality)],
        )
        if ok:
            with self.preview_lock:
                self.preview_jpeg = encoded.tobytes()

    def build_status_snapshot(self, external_host=None):
        gps = self.gps.snapshot()
        runtime = dict(self.last_runtime)
        runtime["last_detections"] = [dict(det) for det in runtime.get("last_detections", [])]
        with self.dispatch_lock:
            dispatch = dict(self.last_dispatch)
        preview_url = ""
        status_url = ""
        if self.args.web_preview:
            host = external_host or f"{self.args.web_preview_host}:{self.args.web_preview_port}"
            preview_url = f"http://{host}/"
            status_url = f"{preview_url.rstrip('/')}/status.json"
        heatmap_url = f"{preview_url.rstrip('/')}/gps-heatmap" if preview_url else ""
        heatmap_json_url = f"{preview_url.rstrip('/')}/gps-heatmap.json" if preview_url else ""
        return {
            "ts": iso_ts(),
            "epoch": epoch_ts(),
            "device": {
                "gateway_id": self.args.gateway_id,
                "device_id": self.args.device_id,
                "point_name": self.args.point_name,
                "device_source": self.args.device_source,
            },
            "preview": {
                "enabled": bool(self.args.web_preview),
                "host": self.args.web_preview_host,
                "port": int(self.args.web_preview_port),
                "url": preview_url,
                "status_url": status_url,
                "heatmap_url": heatmap_url,
                "heatmap_json_url": heatmap_json_url,
                "jpeg_ready": 1 if self.preview_jpeg else 0,
            },
            "counts": {
                "event_count": int(self.event_count),
                "daily_event_count": int(self.daily_event_count),
                "violation_streak": int(self.violation_streak),
                "camera_read_count": int(self.camera.read_count()) if self.camera else 0,
            },
            "dispatch": dispatch,
            "notification_hook": dict(self.last_notification),
            "runtime": runtime,
            "gps": gps,
            "iot_properties": {
                key: value["value"]
                for key, value in self.build_properties(event=None).items()
            },
        }

    def normalize_map_coords(self, lat, lng):
        if lat is None or lng is None:
            return None, None
        if str(self.args.gps_coord_system).lower() == "gcj02":
            map_lng, map_lat = gcj02_to_wgs84(lng, lat)
            return map_lat, map_lng
        return lat, lng

    def normalize_amap_coords(self, lat, lng):
        if lat is None or lng is None:
            return None, None
        if str(self.args.gps_coord_system).lower() == "gcj02":
            return float(lat), float(lng)
        amap_lng, amap_lat = wgs84_to_gcj02(float(lng), float(lat))
        return float(amap_lat), float(amap_lng)

    def build_simplejson_search(self):
        return [
            "gps_route_heatmap",
            "gps_current_location",
            "gps_event_hotspots",
            "gps_event_points",
        ]

    def downsample_rows(self, rows, max_points):
        if len(rows) <= max_points:
            return rows
        step = max(1, int(math.ceil(len(rows) / float(max_points))))
        sampled = rows[::step]
        if sampled[-1] != rows[-1]:
            sampled.append(rows[-1])
        return sampled

    def make_simplejson_table(self, rows):
        return {
            "type": "table",
            "columns": [
                {"text": "hostname", "type": "string"},
                {"text": "latitude", "type": "number"},
                {"text": "longitude", "type": "number"},
                {"text": "metric", "type": "number"},
                {"text": "ts", "type": "string"},
                {"text": "detail", "type": "string"},
            ],
            "rows": rows,
        }

    def build_route_heatmap_rows(self):
        with self.history_lock:
            route = list(self.gps_history)
        route = self.downsample_rows(route, 400)
        rows = []
        for index, item in enumerate(route):
            amap_lat, amap_lng = self.normalize_amap_coords(item.get("lat"), item.get("lng"))
            if amap_lat is None or amap_lng is None:
                continue
            confidence = int(item.get("confidence") or 0)
            event_active = int(item.get("event_active") or 0)
            metric = max(1, confidence if event_active else 1)
            detail = (
                f"speed={float(item.get('speed_kmh') or 0.0):.1f}km/h,"
                f"sats={int(item.get('num_sats') or 0)},"
                f"alert={item.get('alert_level', 'normal')}"
            )
            rows.append(
                [
                    f"{self.args.point_name}-route-{index + 1}",
                    round(amap_lat, 6),
                    round(amap_lng, 6),
                    metric,
                    item.get("ts", ""),
                    detail,
                ]
            )
        return rows

    def build_current_location_rows(self):
        gps = self.gps.snapshot()
        amap_lat, amap_lng = self.normalize_amap_coords(gps.get("lat"), gps.get("lng"))
        if amap_lat is None or amap_lng is None:
            return []
        detail = (
            f"speed={float(gps.get('speed_kmh') or 0.0):.1f}km/h,"
            f"sats={int(gps.get('num_sats') or 0)},"
            f"hdop={float(gps.get('hdop') or 0.0):.1f}"
        )
        return [[
            self.args.point_name,
            round(amap_lat, 6),
            round(amap_lng, 6),
            max(1, int(round(float(self.last_runtime.get("last_confidence", 0.0)) * 100))),
            iso_ts(),
            detail,
        ]]

    def build_event_point_rows(self):
        with self.history_lock:
            events = list(self.event_history)
        rows = []
        for item in events[-200:]:
            amap_lat, amap_lng = self.normalize_amap_coords(item.get("lat"), item.get("lng"))
            if amap_lat is None or amap_lng is None:
                continue
            detail_parts = [
                str(item.get("obstacle_type", "")),
                f"confidence={int(item.get('confidence') or 0)}",
            ]
            if item.get("dispatch_action"):
                detail_parts.append(f"dispatch={item['dispatch_action']}")
            if item.get("dispatch_priority"):
                detail_parts.append(f"priority={item['dispatch_priority']}")
            detail = ",".join(detail_parts)
            rows.append(
                [
                    item.get("event_id", self.args.point_name),
                    round(amap_lat, 6),
                    round(amap_lng, 6),
                    max(1, int(item.get("confidence") or 1)),
                    item.get("ts", ""),
                    detail,
                ]
            )
        return rows

    def build_hotspot_rows(self):
        payload = self.build_heatmap_payload()
        rows = []
        for index, item in enumerate(payload.get("hotspots", [])[:100]):
            amap_lat, amap_lng = self.normalize_amap_coords(item.get("lat"), item.get("lng"))
            if amap_lat is None or amap_lng is None:
                continue
            detail = ",".join(item.get("obstacle_types") or [])
            rows.append(
                [
                    f"hotspot-{index + 1}",
                    round(amap_lat, 6),
                    round(amap_lng, 6),
                    max(1, int(round(float(item.get("weight") or 0.0) * 100))),
                    item.get("last_ts", ""),
                    detail,
                ]
            )
        return rows

    def build_simplejson_query(self, payload):
        results = []
        targets = payload.get("targets") or []
        for target in targets:
            target_name = str(
                target.get("target")
                or target.get("query")
                or target.get("displayName")
                or ""
            ).strip()
            if not target_name:
                continue
            if target_name == "gps_route_heatmap":
                table = self.make_simplejson_table(self.build_route_heatmap_rows())
            elif target_name == "gps_current_location":
                table = self.make_simplejson_table(self.build_current_location_rows())
            elif target_name == "gps_event_hotspots":
                table = self.make_simplejson_table(self.build_hotspot_rows())
            elif target_name == "gps_event_points":
                table = self.make_simplejson_table(self.build_event_point_rows())
            else:
                continue
            table["target"] = target_name
            table["refId"] = target.get("refId", "")
            results.append(table)
        return results

    def record_gps_sample(self, gps):
        lat = gps.get("lat")
        lng = gps.get("lng")
        if lat is None or lng is None:
            return
        map_lat, map_lng = self.normalize_map_coords(lat, lng)
        sample = {
            "ts": iso_ts(),
            "epoch": epoch_ts(),
            "lat": float(lat),
            "lng": float(lng),
            "map_lat": float(map_lat),
            "map_lng": float(map_lng),
            "speed_kmh": float(gps.get("speed_kmh") or 0.0),
            "angle": float(gps.get("angle") or 0.0),
            "num_sats": int(gps.get("num_sats") or 0),
            "hdop": float(gps.get("hdop") or 0.0),
            "event_active": int(self.last_runtime.get("roi_occupied", 0)),
            "confidence": int(round(float(self.last_runtime.get("last_confidence", 0.0)) * 100)),
            "alert_level": self.last_runtime.get("alert_level", "normal"),
        }
        with self.history_lock:
            self.gps_history.append(sample)

    def record_event_history(self, event):
        lat = event.get("lat")
        lng = event.get("lng")
        if lat is None or lng is None:
            return
        map_lat, map_lng = self.normalize_map_coords(lat, lng)
        record = {
            "event_id": event["event_id"],
            "ts": event["capture_time"],
            "epoch": epoch_ts(),
            "lat": float(lat),
            "lng": float(lng),
            "map_lat": float(map_lat),
            "map_lng": float(map_lng),
            "obstacle_type": event["obstacle_type"],
            "confidence": int(round(float(event["confidence"]) * 100)),
            "snapshot_name": event["snapshot_name"],
            "snapshot_url": event.get("snapshot_url", ""),
            "dispatch_status": "pending" if self.dispatch_client.enabled else "disabled",
            "dispatch_action": "",
            "dispatch_priority": "",
            "dispatch_summary": "",
            "dispatch_suggestion": "",
            "dispatch_updated_at": "",
            "dispatch_error": "",
        }
        with self.history_lock:
            self.event_history.append(record)

    def build_snapshot_url(self, snapshot_name):
        if not snapshot_name:
            return ""
        base_url = str(self.args.workflow_snapshot_base_url or "").strip().rstrip("/")
        if not base_url:
            return ""
        return f"{base_url}/snapshots/{snapshot_name}"

    def build_dispatch_inputs(self, event, gps, runtime):
        alert_level = str(runtime.get("alert_level") or "normal").strip().lower()
        gps_lat = gps.get("lat")
        gps_lng = gps.get("lng")
        return {
            "deviceId": str(self.args.device_id),
            "timestamp": str(event.get("capture_time") or iso_ts()),
            "gpsLat": "" if gps_lat is None else f"{float(gps_lat):.6f}",
            "gpsLng": "" if gps_lng is None else f"{float(gps_lng):.6f}",
            "eventActive": "1",
            "obstacleConfidence": f"{float(event.get('confidence') or 0.0):.2f}",
            "alertLevelCode": alert_level,
            "triggerStreak": str(int(runtime.get("trigger_streak") or 0)),
            "snapshotUrl": str(event.get("snapshot_url") or ""),
        }

    def update_dispatch_state(self, **kwargs):
        with self.dispatch_lock:
            self.last_dispatch.update(kwargs)
            dispatch = dict(self.last_dispatch)
        self.last_runtime.update(
            {
                "dispatch_status": dispatch.get("status", ""),
                "dispatch_action": dispatch.get("action", ""),
                "dispatch_priority": dispatch.get("priority", ""),
                "dispatch_summary": dispatch.get("summary", ""),
            }
        )

    def update_event_dispatch(self, event_id, **kwargs):
        with self.history_lock:
            for record in reversed(self.event_history):
                if record.get("event_id") == event_id:
                    record.update(kwargs)
                    break

    def dispatch_event_workflow(self, event, gps):
        if not self.dispatch_client.enabled:
            return
        event_id = str(event.get("event_id") or "")
        inputs = self.build_dispatch_inputs(event, gps, dict(self.last_runtime))
        self.update_dispatch_state(
            status="running",
            event_id=event_id,
            action="",
            priority="",
            summary="workflow_running",
            suggestion="",
            updated_at=iso_ts(),
            error="",
        )
        self.update_event_dispatch(
            event_id,
            dispatch_status="running",
            dispatch_updated_at=iso_ts(),
            dispatch_error="",
        )

        def worker():
            try:
                dispatch = self.dispatch_client.run(inputs)
            except Exception as exc:
                error_text = str(exc)
                print(f"[WORKFLOW] event {event_id} failed: {error_text}")
                self.update_dispatch_state(
                    status="failed",
                    event_id=event_id,
                    summary="workflow_failed",
                    error=error_text,
                    updated_at=iso_ts(),
                )
                self.update_event_dispatch(
                    event_id,
                    dispatch_status="failed",
                    dispatch_error=error_text,
                    dispatch_updated_at=iso_ts(),
                )
                try:
                    self.send_status(event=event)
                except Exception as publish_exc:
                    print(f"[WORKFLOW] publish after failure failed: {publish_exc}")
                return

            print(f"[WORKFLOW] event {event_id} -> {dispatch['action']} / {dispatch['priority']}")
            self.update_dispatch_state(
                status="succeeded",
                event_id=event_id,
                action=dispatch["action"],
                priority=dispatch["priority"],
                summary=dispatch["summary"],
                suggestion=dispatch["suggestion"],
                updated_at=iso_ts(),
                error="",
            )
            self.update_event_dispatch(
                event_id,
                dispatch_status="succeeded",
                dispatch_action=dispatch["action"],
                dispatch_priority=dispatch["priority"],
                dispatch_summary=dispatch["summary"],
                dispatch_suggestion=dispatch["suggestion"],
                dispatch_updated_at=iso_ts(),
                dispatch_error="",
            )
            try:
                self.send_status(event=event)
            except Exception as publish_exc:
                print(f"[WORKFLOW] publish after success failed: {publish_exc}")

        threading.Thread(
            target=worker,
            name=f"dispatch-{event_id or uuid.uuid4().hex[:8]}",
            daemon=True,
        ).start()

    def build_heatmap_payload(self, external_host=None):
        gps = self.gps.snapshot()
        current_map_lat, current_map_lng = self.normalize_map_coords(gps.get("lat"), gps.get("lng"))
        preview_base = ""
        if self.args.web_preview:
            host = external_host or f"{self.args.web_preview_host}:{self.args.web_preview_port}"
            preview_base = f"http://{host}"
        with self.history_lock:
            route = list(self.gps_history)
            events = list(self.event_history)

        grid = {}
        for item in events:
            if item["map_lat"] is None or item["map_lng"] is None:
                continue
            lat_key = round(item["map_lat"], 4)
            lng_key = round(item["map_lng"], 4)
            key = (lat_key, lng_key)
            bucket = grid.setdefault(
                key,
                {
                    "lat": lat_key,
                    "lng": lng_key,
                    "weight": 0.0,
                    "count": 0,
                    "max_confidence": 0,
                    "last_ts": item["ts"],
                    "obstacle_types": set(),
                },
            )
            confidence = max(1, int(item.get("confidence", 0)))
            bucket["weight"] += confidence / 100.0
            bucket["count"] += 1
            bucket["max_confidence"] = max(bucket["max_confidence"], confidence)
            bucket["last_ts"] = item["ts"]
            bucket["obstacle_types"].add(item["obstacle_type"])

        hotspots = []
        for bucket in grid.values():
            hotspots.append(
                {
                    "lat": bucket["lat"],
                    "lng": bucket["lng"],
                    "weight": round(bucket["weight"], 2),
                    "count": int(bucket["count"]),
                    "max_confidence": int(bucket["max_confidence"]),
                    "last_ts": bucket["last_ts"],
                    "obstacle_types": sorted(bucket["obstacle_types"]),
                }
            )
        hotspots.sort(key=lambda item: (item["weight"], item["count"]), reverse=True)

        normalized_events = []
        for item in events:
            record = dict(item)
            if preview_base and record.get("snapshot_name"):
                record["snapshot_url"] = f"{preview_base}/snapshots/{record['snapshot_name']}"
            normalized_events.append(record)

        return {
            "ts": iso_ts(),
            "coord_system": str(self.args.gps_coord_system),
            "device": {
                "gateway_id": self.args.gateway_id,
                "device_id": self.args.device_id,
                "point_name": self.args.point_name,
            },
            "current": {
                "lat": gps.get("lat"),
                "lng": gps.get("lng"),
                "map_lat": current_map_lat,
                "map_lng": current_map_lng,
                "speed_kmh": float(gps.get("speed_kmh") or 0.0),
                "angle": float(gps.get("angle") or 0.0),
                "num_sats": int(gps.get("num_sats") or 0),
                "hdop": float(gps.get("hdop") or 0.0),
                "status": gps.get("status", ""),
            },
            "summary": {
                "route_points": len(route),
                "events": len(normalized_events),
                "hotspots": len(hotspots),
            },
            "route": route,
            "events": normalized_events,
            "hotspots": hotspots,
            "urls": {
                "preview": f"{preview_base}/" if preview_base else "",
                "status": f"{preview_base}/status.json" if preview_base else "",
            },
        }

    def build_czml_payload(self):
        with self.history_lock:
            route = list(self.gps_history)

        route = self.downsample_rows(route, 400)
        route_coords = []
        for item in route:
            lat = item.get("map_lat")
            lng = item.get("map_lng")
            if lat is None or lng is None:
                continue
            route_coords.extend(
                [
                    round(float(lng), 6),
                    round(float(lat), 6),
                    0,
                ]
            )

        gps = self.gps.snapshot()
        current_lat, current_lng = self.normalize_map_coords(gps.get("lat"), gps.get("lng"))

        packets = [
            {
                "id": "document",
                "name": "blindway-route",
                "version": "1.0",
            }
        ]

        if len(route_coords) >= 6:
            packets.append(
                {
                    "id": "route",
                    "name": "GPS Route",
                    "polyline": {
                        "positions": {
                            "cartographicDegrees": route_coords,
                        },
                        "material": {
                            "solidColor": {
                                "color": {
                                    "rgba": [56, 189, 248, 255],
                                }
                            }
                        },
                        "width": 4,
                    },
                }
            )

        if current_lat is not None and current_lng is not None:
            packets.append(
                {
                    "id": "current",
                    "name": self.args.point_name,
                    "position": {
                        "cartographicDegrees": [
                            round(float(current_lng), 6),
                            round(float(current_lat), 6),
                            0,
                        ]
                    },
                    "point": {
                        "pixelSize": 12,
                        "color": {"rgba": [34, 197, 94, 255]},
                        "outlineColor": {"rgba": [255, 255, 255, 255]},
                        "outlineWidth": 2,
                    },
                    "label": {
                        "text": self.args.point_name,
                        "fillColor": {"rgba": [255, 255, 255, 255]},
                        "showBackground": True,
                        "backgroundColor": {"rgba": [15, 23, 42, 200]},
                        "pixelOffset": {"cartesian2": [0, -24]},
                    },
                }
            )

        return packets

    def build_heatmap_html(self):
        return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GPS Route Hotspot Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body { margin: 0; padding: 0; height: 100%; background: #0f172a; color: #e2e8f0; font-family: Arial, sans-serif; }
    #map { width: 100%; height: 100%; }
    .panel {
      position: absolute;
      top: 12px;
      left: 12px;
      z-index: 1000;
      width: 340px;
      padding: 12px 14px;
      border-radius: 12px;
      background: rgba(15, 23, 42, 0.88);
      box-shadow: 0 10px 30px rgba(2, 6, 23, 0.4);
      backdrop-filter: blur(6px);
    }
    .panel h1 { margin: 0 0 8px; font-size: 18px; }
    .muted { color: #94a3b8; font-size: 12px; line-height: 1.5; }
    .stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
    .card { background: rgba(30, 41, 59, 0.86); border-radius: 10px; padding: 8px 10px; }
    .card .label { font-size: 11px; color: #94a3b8; }
    .card .value { font-size: 18px; font-weight: bold; color: #f8fafc; margin-top: 4px; }
    .legend {
      position: absolute;
      right: 12px;
      bottom: 12px;
      z-index: 1000;
      padding: 10px 12px;
      border-radius: 10px;
      background: rgba(15, 23, 42, 0.82);
      line-height: 1.7;
      font-size: 12px;
    }
    .dot { display: inline-block; width: 10px; height: 10px; border-radius: 999px; margin-right: 6px; }
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="panel">
    <h1>盲道巡检 GPS 热点图</h1>
    <div class="muted" id="summary">正在加载数据...</div>
    <div class="stats">
      <div class="card"><div class="label">轨迹点</div><div class="value" id="routeCount">0</div></div>
      <div class="card"><div class="label">事件数</div><div class="value" id="eventCount">0</div></div>
      <div class="card"><div class="label">热点簇</div><div class="value" id="hotspotCount">0</div></div>
    </div>
  </div>
  <div class="legend">
    <div><span class="dot" style="background:#38bdf8"></span>巡检轨迹</div>
    <div><span class="dot" style="background:#f59e0b"></span>事件点</div>
    <div><span class="dot" style="background:#ef4444"></span>热点强区</div>
    <div><span class="dot" style="background:#22c55e"></span>当前设备位置</div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.heat/dist/leaflet-heat.js"></script>
  <script>
    const map = L.map('map', { zoomControl: true });
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors',
      maxZoom: 19
    }).addTo(map);

    const routeLayer = L.polyline([], { color: '#38bdf8', weight: 4, opacity: 0.85 }).addTo(map);
    const eventLayer = L.layerGroup().addTo(map);
    const hotspotLayer = L.layerGroup().addTo(map);
    let heatLayer = L.heatLayer([], { radius: 28, blur: 22, maxZoom: 18, minOpacity: 0.4 }).addTo(map);
    let currentMarker = null;
    let firstFitDone = false;

    function setText(id, value) {
      document.getElementById(id).textContent = value;
    }

    async function loadMap() {
      try {
        const res = await fetch('/gps-heatmap.json', { cache: 'no-store' });
        const payload = await res.json();
        const route = (payload.route || []).filter(p => Number.isFinite(p.map_lat) && Number.isFinite(p.map_lng));
        const events = (payload.events || []).filter(p => Number.isFinite(p.map_lat) && Number.isFinite(p.map_lng));
        const hotspots = (payload.hotspots || []).filter(p => Number.isFinite(p.lat) && Number.isFinite(p.lng));
        const current = payload.current || {};

        setText('routeCount', payload.summary ? payload.summary.route_points : route.length);
        setText('eventCount', payload.summary ? payload.summary.events : events.length);
        setText('hotspotCount', payload.summary ? payload.summary.hotspots : hotspots.length);
        setText(
          'summary',
          '设备 ' + (payload.device ? payload.device.device_id : '-') +
          '，坐标系 ' + (payload.coord_system || '-') +
          '，当前卫星数 ' + (current.num_sats ?? '-') +
          '，HDOP ' + (current.hdop ?? '-')
        );

        routeLayer.setLatLngs(route.map(p => [p.map_lat, p.map_lng]));
        eventLayer.clearLayers();
        hotspotLayer.clearLayers();
        if (heatLayer) {
          heatLayer.setLatLngs([]);
          map.removeLayer(heatLayer);
        }
        heatLayer = L.heatLayer(
          hotspots.map(p => [p.lat, p.lng, Math.max(0.4, Number(p.weight) || 0.4)]),
          { radius: 30, blur: 24, maxZoom: 18, minOpacity: 0.35 }
        ).addTo(map);

        hotspots.forEach(p => {
          const circle = L.circleMarker([p.lat, p.lng], {
            radius: Math.min(18, 6 + Number(p.weight || 0)),
            color: '#ef4444',
            weight: 2,
            fillColor: '#f97316',
            fillOpacity: 0.5
          });
          circle.bindPopup(
            '<b>热点簇</b><br>' +
            '聚合事件数: ' + p.count + '<br>' +
            '最大置信度: ' + p.max_confidence + '%<br>' +
            '类型: ' + (p.obstacle_types || []).join(', ') + '<br>' +
            '最近时间: ' + p.last_ts
          );
          hotspotLayer.addLayer(circle);
        });

        events.forEach(p => {
          const marker = L.circleMarker([p.map_lat, p.map_lng], {
            radius: 6,
            color: '#f59e0b',
            weight: 2,
            fillColor: '#fde68a',
            fillOpacity: 0.9
          });
          let popup = '<b>' + p.obstacle_type + '</b><br>' +
            '时间: ' + p.ts + '<br>' +
            '置信度: ' + p.confidence + '%';
          if (p.snapshot_url) {
            popup += '<br><a href=\"' + p.snapshot_url + '\" target=\"_blank\">查看抓拍</a>';
          }
          marker.bindPopup(popup);
          eventLayer.addLayer(marker);
        });

        if (currentMarker) {
          map.removeLayer(currentMarker);
          currentMarker = null;
        }
        if (Number.isFinite(current.map_lat) && Number.isFinite(current.map_lng)) {
          currentMarker = L.circleMarker([current.map_lat, current.map_lng], {
            radius: 8,
            color: '#16a34a',
            weight: 3,
            fillColor: '#4ade80',
            fillOpacity: 1
          }).addTo(map);
          currentMarker.bindPopup(
            '<b>当前设备位置</b><br>' +
            '速度: ' + (current.speed_kmh || 0).toFixed(2) + ' km/h<br>' +
            '航向: ' + (current.angle || 0).toFixed(1) + '°<br>' +
            'GPS 状态: ' + (current.status || '-')
          );
        }

        if (!firstFitDone) {
          const fitPoints = [];
          route.forEach(p => fitPoints.push([p.map_lat, p.map_lng]));
          hotspots.forEach(p => fitPoints.push([p.lat, p.lng]));
          if (Number.isFinite(current.map_lat) && Number.isFinite(current.map_lng)) {
            fitPoints.push([current.map_lat, current.map_lng]);
          }
          if (fitPoints.length > 0) {
            map.fitBounds(fitPoints, { padding: [30, 30] });
          } else {
            map.setView([31.2304, 121.4737], 13);
          }
          firstFitDone = true;
        }
      } catch (err) {
        setText('summary', '热点图加载失败: ' + err);
      }
    }

    map.setView([31.2304, 121.4737], 13);
    loadMap();
    setInterval(loadMap, 5000);
  </script>
</body>
</html>
"""

    def close(self):
        self.running = False
        self.cloud.close()
        self.gps.stop()
        if self.camera:
            self.camera.stop()
        if self.preview_server:
            self.preview_server.shutdown()
            self.preview_server.server_close()

    def annotate_frame(self, frame):
        annotated = frame.copy()
        frame_h, frame_w = annotated.shape[:2]
        for det in self.last_runtime["last_detections"]:
            x1, y1, x2, y2 = clamp_box(det["box"], frame_w, frame_h)
            is_target = det.get("is_target", False)
            in_roi = det.get("in_roi", False)
            color = (255, 200, 0)
            if is_target and not in_roi:
                color = (0, 200, 255)
            if is_target and in_roi:
                color = (0, 80, 255)
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            label = f"{det['class_name']} {det['score']:.2f}"
            if in_roi:
                label += " ROI"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2)
            text_y = max(18, y1 - 6)
            cv2.rectangle(
                annotated,
                (x1, max(0, text_y - th - 6)),
                (x1 + tw + 8, text_y + 2),
                color,
                -1,
            )
            cv2.putText(
                annotated,
                label,
                (x1 + 4, text_y - 2),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (255, 255, 255),
                1,
            )
        cv2.putText(
            annotated,
            f"FPS {self.last_runtime['camera_fps']:.1f}  INF {self.last_runtime['inference_ms']}ms",
            (10, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 255),
            2,
        )
        cv2.putText(
            annotated,
            f"GPS {self.gps.snapshot()['status']}  ROI {self.last_runtime['roi_occupied']}  DET {self.last_runtime['detections_in_frame']}",
            (10, 50),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 0),
            2,
        )
        return annotated

    def build_properties(self, *, event=None):
        gps = self.gps.snapshot()
        runtime = self.last_runtime
        obstacle_type = event["obstacle_type"] if event else runtime["last_obstacle_type"]
        _ = event["capture_time"] if event else iso_ts()
        dispatch_status = str(runtime.get("dispatch_status") or "").strip().lower()
        dispatch_action = str(runtime.get("dispatch_action") or "").strip().lower()
        dispatch_priority = str(runtime.get("dispatch_priority") or "").strip().lower()
        gps_age_sec = 0
        if gps.get("updated_at"):
            gps_age_sec = max(0, int(time.time() - float(gps["updated_at"])))
        last_event_age_sec = 0
        if runtime["last_event_epoch"] > 0:
            last_event_age_sec = max(0, int(time.time() - runtime["last_event_epoch"]))
        props = {
            "cameraStatusCode": make_property(
                encode_status_code(runtime["camera_status"], "streaming", CAMERA_STATUS_CODES),
                "int",
            ),
            "gpsStatusCode": make_property(GPS_STATUS_CODES.get(gps["status"], 0), "int"),
            "edgeStatusCode": make_property(EDGE_STATUS_CODES.get(runtime["edge_status"], 0), "int"),
            "alertLevelCode": make_property(ALERT_LEVEL_CODES.get(runtime["alert_level"], 0), "int"),
            "obstacleTypeCode": make_property(OBSTACLE_TYPE_CODES.get(obstacle_type, 0), "int"),
            "rawObstacleClassCode": make_property(MODEL_CLASS_CODES.get(runtime["last_raw_class_name"], 0), "int"),
            "obstacleConfidence": make_property(
                int(round((event["confidence"] if event else runtime["last_confidence"]) * 100)),
                "int",
            ),
            "eventActive": make_property(1 if event else 0, "int"),
            "eventCount": make_property(self.event_count, "int"),
            "dailyEventCount": make_property(self.daily_event_count, "int"),
            "lastEventAgeSec": make_property(last_event_age_sec, "int"),
            "captureEpoch": make_property(epoch_ts(), "int"),
            "gpsLatE6": make_property(scaled_int(gps["lat"], 1_000_000), "int"),
            "gpsLngE6": make_property(scaled_int(gps["lng"], 1_000_000), "int"),
            "speedKmhX100": make_property(scaled_int(gps["speed_kmh"], 100), "int"),
            "gpsAngleDegX100": make_property(scaled_int(gps["angle"], 100), "int"),
            "gpsAgeSec": make_property(gps_age_sec, "int"),
            "numSats": make_property(int(gps["num_sats"] or 0), "int"),
            "hdopX100": make_property(scaled_int(gps["hdop"], 100), "int"),
            "altitudeCm": make_property(scaled_int(gps["altitude"], 100), "int"),
            "roiOccupied": make_property(runtime["roi_occupied"], "int"),
            "detectionsInFrame": make_property(runtime["detections_in_frame"], "int"),
            "targetDetectionsInFrame": make_property(runtime["target_detections_in_frame"], "int"),
            "triggerStreak": make_property(runtime["trigger_streak"], "int"),
            "snapshotReady": make_property(runtime["snapshot_ready"], "int"),
            "dispatchStatusCode": make_property(DISPATCH_STATUS_CODES.get(dispatch_status, 0), "int"),
            "dispatchActionCode": make_property(DISPATCH_ACTION_CODES.get(dispatch_action, 0), "int"),
            "dispatchPriorityCode": make_property(DISPATCH_PRIORITY_CODES.get(dispatch_priority, 0), "int"),
            "cameraFpsX100": make_property(scaled_int(runtime["camera_fps"], 100), "int"),
            "inferenceMs": make_property(int(runtime["inference_ms"]), "int"),
            "cameraWidth": make_property(int(self.args.camera_width), "int"),
            "cameraHeight": make_property(int(self.args.camera_height), "int"),
            "pointCode": make_property(1, "int"),
            "deviceSourceCode": make_property(1, "int"),
            "modelVersionCode": make_property(1, "int"),
        }
        return props

    def send_status(self, *, event=None, include_snapshot=True):
        props = self.build_properties(event=event)
        payload = self.build_status_snapshot()
        payload["iot_properties"] = {key: value["value"] for key, value in props.items()}
        if event and include_snapshot:
            snapshot_name = pathlib.Path(str(event.get("snapshot_name") or "")).name
            snapshot_path = self.snapshots_dir / snapshot_name
            if snapshot_name and snapshot_path.is_file():
                size = snapshot_path.stat().st_size
                if size <= self.args.cloud_max_snapshot_bytes:
                    payload["snapshot_filename"] = snapshot_name
                    payload["snapshot_b64"] = base64.b64encode(snapshot_path.read_bytes()).decode("ascii")
                else:
                    print(f"[CLOUD] skip oversized snapshot {snapshot_name}: {size} bytes")
        self.cloud.submit(payload)
        self.last_heartbeat_ts = time.time()

    def create_event(self, frame, detection, gps):
        self.event_count += 1
        current_day = today_text()
        if current_day != self.last_event_date:
            self.daily_event_count = 0
            self.last_event_date = current_day
        self.daily_event_count += 1

        event_id = f"evt-{uuid.uuid4().hex[:12]}"
        obstacle_type = self.event_mapping.get(detection["class_name"], detection["class_name"])
        capture_time = iso_ts()
        snapshot_name = f"{capture_time[:19].replace(':', '').replace('-', '')}_{obstacle_type}_{event_id}.jpg"
        snapshot_path = self.snapshots_dir / snapshot_name
        cv2.imwrite(str(snapshot_path), frame)

        event = {
            "event_id": event_id,
            "obstacle_type": obstacle_type,
            "confidence": float(detection["score"]),
            "capture_time": capture_time,
            "snapshot_name": snapshot_name,
            "snapshot_url": self.build_snapshot_url(snapshot_name),
            "lat": gps["lat"],
            "lng": gps["lng"],
        }
        self.last_runtime.update(
            {
                "alert_level": "warning",
                "last_event_id": event_id,
                "last_snapshot_name": snapshot_name,
                "last_obstacle_type": obstacle_type,
                "last_raw_class_name": detection["class_name"],
                "last_confidence": float(detection["score"]),
                "last_event_epoch": time.time(),
                "snapshot_ready": 1,
                "edge_status": "alert",
                "event_state": "active",
            }
        )
        self.record_event_history(event)
        return event

    def refresh_event_evidence(self, frame, detection, event):
        """Capture a fresh frame for the same incident without duplicating it."""
        capture_time = iso_ts()
        snapshot_name = f"{capture_time[:19].replace(':', '').replace('-', '')}_{event['obstacle_type']}_{event['event_id']}_evidence.jpg"
        cv2.imwrite(str(self.snapshots_dir / snapshot_name), frame)
        event.update(
            {
                "confidence": max(float(event.get("confidence", 0)), float(detection.get("score", 0))),
                "capture_time": capture_time,
                "snapshot_name": snapshot_name,
                "snapshot_url": self.build_snapshot_url(snapshot_name),
            }
        )
        self.last_runtime.update(
            {
                "last_snapshot_name": snapshot_name,
                "last_confidence": event["confidence"],
                "snapshot_ready": 1,
                "event_state": "active",
            }
        )
        return event

    def run(self):
        self.cloud.start()
        print(f"[CLOUD] direct upload enabled: {self.args.cloud_url}")
        self.start_preview_server()
        self.camera = CameraReader(
            preferred=self.args.camera,
            width=self.args.camera_width,
            height=self.args.camera_height,
            fps=self.args.camera_target_fps,
            use_mjpg=self.args.camera_use_mjpg,
            buffer_size=self.args.camera_buffer_size,
        )
        self.camera.start()
        self.last_runtime["camera_status"] = f"streaming:{self.camera.label}"
        fps_window_start = time.time()
        fps_window_start_count = 0
        last_infer_ts = 0.0
        last_infer_frame_id = -1
        last_frame_id = -1

        try:
            while True:
                frame, frame_id = self.camera.snapshot_with_id()
                if frame is None:
                    self.last_runtime["camera_status"] = "read_failed"
                    self.last_runtime["edge_status"] = "camera_error"
                    time.sleep(0.1)
                    continue
                if frame_id == last_frame_id:
                    time.sleep(0.005)
                    continue
                last_frame_id = frame_id
                self.last_runtime["camera_status"] = f"streaming:{self.camera.label}"

                now = time.time()
                if now - fps_window_start >= 1.0:
                    current_count = self.camera.read_count()
                    self.last_runtime["camera_fps"] = (current_count - fps_window_start_count) / max(now - fps_window_start, 1e-6)
                    fps_window_start_count = current_count
                    fps_window_start = now

                gps = self.gps.snapshot()
                if time.time() - self.last_gps_sample_ts >= self.args.gps_sample_interval:
                    self.record_gps_sample(gps)
                    self.last_gps_sample_ts = time.time()
                roi = build_default_roi(frame.shape[1], frame.shape[0])
                frame_gap_ready = (
                    last_infer_frame_id < 0
                    or frame_id - last_infer_frame_id >= max(self.args.infer_every_n_frames, 1)
                )
                interval_ready = now - last_infer_ts >= max(self.args.infer_interval_sec, 0.0)
                if frame_gap_ready and interval_ready:
                    start = time.time()
                    if last_infer_ts > 0:
                        inference_period = start - last_infer_ts
                        self.last_runtime["inference_period_ms"] = int(inference_period * 1000)
                        self.last_runtime["inference_fps"] = round(1.0 / max(inference_period, 1e-6), 2)
                    last_infer_ts = start
                    last_infer_frame_id = frame_id
                    detections = self.detector.detect(frame)
                    inference_ms = int((time.time() - start) * 1000)
                    self.last_runtime["inference_ms"] = inference_ms
                    monitored = []
                    display_detections = []
                    for det in detections:
                        x1, y1, x2, y2 = det["box"]
                        bottom_center = (int((x1 + x2) / 2), int(y2))
                        in_roi = point_in_roi(roi, bottom_center)
                        is_target = det["class_name"] in self.event_mapping
                        display_detections.append(
                            {
                                "class_name": det["class_name"],
                                "score": float(det["score"]),
                                "box": [int(x1), int(y1), int(x2), int(y2)],
                                "in_roi": bool(in_roi),
                                "is_target": bool(is_target),
                            }
                        )
                        if is_target and in_roi:
                            monitored.append(det)
                    monitored.sort(key=lambda item: item["score"], reverse=True)
                    self.last_runtime["last_detections"] = display_detections[:12]
                    self.last_runtime["detections_in_frame"] = len(display_detections)
                    self.last_runtime["target_detections_in_frame"] = len(monitored)
                    self.last_runtime["roi_occupied"] = 1 if monitored else 0
                    if monitored:
                        self.violation_streak += 1
                        self.last_runtime["alert_level"] = "warning"
                    else:
                        self.last_runtime["alert_level"] = "normal"
                        self.last_runtime["edge_status"] = "running"
                    if monitored:
                        self.last_runtime["last_raw_class_name"] = monitored[0]["class_name"]
                        self.last_runtime["last_confidence"] = float(monitored[0]["score"])
                    lat = gps.get("lat")
                    lng = gps.get("lng")
                    location = (float(lat), float(lng)) if lat is not None and lng is not None else None
                    decision = self.occupancy_state.update(
                        now=time.time(),
                        detection=monitored[0] if monitored else None,
                        location=location,
                    )
                    self.violation_streak = self.occupancy_state.hit_count
                    self.last_runtime["trigger_streak"] = self.violation_streak
                    if decision.action == "confirmed" and decision.detection:
                        self.active_event = self.create_event(frame, decision.detection, gps)
                        self.send_status(event=self.active_event)
                        self.last_event_ts = time.time()
                    elif decision.action == "evidence" and decision.detection and self.active_event:
                        self.refresh_event_evidence(frame, decision.detection, self.active_event)
                        self.send_status(event=self.active_event)
                    elif decision.action == "cleared" and self.active_event:
                        self.last_runtime.update(
                            {"event_state": "cleared", "alert_level": "normal", "edge_status": "running"}
                        )
                        self.send_status()
                        self.active_event = None
                        self.last_runtime["event_state"] = "idle"
                if time.time() - self.last_heartbeat_ts >= self.args.heartbeat_interval:
                    if not self.active_event:
                        self.last_runtime["edge_status"] = "running"
                    self.send_status(event=self.active_event, include_snapshot=False)

                if self.args.preview:
                    annotated = self.annotate_frame(frame)
                    cv2.imshow("blind_occupancy_edge", annotated)
                    if (cv2.waitKey(1) & 0xFF) == ord("q"):
                        break
                time.sleep(0.01)
        finally:
            if self.args.preview:
                cv2.destroyAllWindows()
            self.close()


def parse_args():
    parser = argparse.ArgumentParser(description="VisionBridge edge client with direct cloud upload")
    parser.add_argument("--model-path", required=True, help="Path to YOLOv8 ONNX model")
    parser.add_argument("--model-version", default="blind-occupancy-yolov8n-onnx-v1")
    parser.add_argument("--cloud-url", default=os.environ.get("VISIONBRIDGE_CLOUD_URL"))
    parser.add_argument("--cloud-token", default=os.environ.get("VISIONBRIDGE_INGEST_TOKEN"))
    parser.add_argument("--cloud-timeout-sec", type=float, default=float(os.environ.get("VISIONBRIDGE_CLOUD_TIMEOUT_SEC", "12")))
    parser.add_argument("--cloud-queue-size", type=int, default=int(os.environ.get("VISIONBRIDGE_CLOUD_QUEUE_SIZE", "128")))
    parser.add_argument("--cloud-max-snapshot-bytes", type=int, default=int(os.environ.get("VISIONBRIDGE_MAX_SNAPSHOT_BYTES", str(4 * 1024 * 1024))))
    parser.add_argument("--gateway-id", default=os.environ.get("VISIONBRIDGE_GATEWAY_ID", "visionbridge-gateway-01"))
    parser.add_argument("--device-id", default=os.environ.get("VISIONBRIDGE_DEVICE_ID", "visionbridge-edge-01"))
    parser.add_argument("--camera", default=os.environ.get("USB_CAMERA_DEVICE", ""))
    parser.add_argument("--snapshots-dir", default=os.environ.get("SNAPSHOTS_DIR", "./snapshots"))
    parser.add_argument("--point-name", default=os.environ.get("POINT_NAME", "blindway-point-01"))
    parser.add_argument("--device-source", default=os.environ.get("DEVICE_SOURCE", "usb_camera+lc76g"))
    parser.add_argument("--gps-coord-system", default=os.environ.get("GPS_COORD_SYSTEM", "gcj02"))
    parser.add_argument("--heartbeat-interval", type=int, default=20)
    parser.add_argument("--event-cooldown", type=float, default=float(os.environ.get("EVENT_COOLDOWN_SEC", "20")))
    parser.add_argument("--confirm-window-frames", type=int, default=int(os.environ.get("CONFIRM_WINDOW_FRAMES", "8")))
    parser.add_argument("--confirm-required-hits", type=int, default=int(os.environ.get("CONFIRM_REQUIRED_HITS", "5")))
    parser.add_argument("--confirm-min-duration-sec", type=float, default=float(os.environ.get("CONFIRM_MIN_DURATION_SEC", "2")))
    parser.add_argument("--clear-miss-frames", type=int, default=int(os.environ.get("CLEAR_MISS_FRAMES", "5")))
    parser.add_argument("--clear-duration-sec", type=float, default=float(os.environ.get("CLEAR_DURATION_SEC", "3")))
    parser.add_argument("--evidence-interval-sec", type=float, default=float(os.environ.get("EVIDENCE_INTERVAL_SEC", "30")))
    parser.add_argument("--spatial-dedup-meters", type=float, default=float(os.environ.get("SPATIAL_DEDUP_METERS", "15")))
    parser.add_argument("--infer-every-n-frames", type=int, default=2)
    parser.add_argument("--infer-interval-sec", type=float, default=float(os.environ.get("INFER_INTERVAL_SEC", "0.8")))
    parser.add_argument("--input-size", type=int, default=int(os.environ.get("INPUT_SIZE", "320")))
    parser.add_argument("--camera-width", type=int, default=int(os.environ.get("CAMERA_WIDTH", "640")))
    parser.add_argument("--camera-height", type=int, default=int(os.environ.get("CAMERA_HEIGHT", "360")))
    parser.add_argument("--camera-target-fps", type=int, default=int(os.environ.get("CAMERA_TARGET_FPS", "15")))
    parser.add_argument("--camera-use-mjpg", type=int, default=int(os.environ.get("CAMERA_USE_MJPG", "1")))
    parser.add_argument("--camera-buffer-size", type=int, default=int(os.environ.get("CAMERA_BUFFER_SIZE", "1")))
    parser.add_argument("--gps-sample-interval", type=float, default=float(os.environ.get("GPS_SAMPLE_INTERVAL", "2.0")))
    parser.add_argument("--gps-history-max-points", type=int, default=int(os.environ.get("GPS_HISTORY_MAX_POINTS", "1800")))
    parser.add_argument("--event-history-max-points", type=int, default=int(os.environ.get("EVENT_HISTORY_MAX_POINTS", "300")))
    parser.add_argument("--conf-threshold", type=float, default=0.35)
    parser.add_argument("--iou-threshold", type=float, default=0.45)
    parser.add_argument("--web-preview", action="store_true", default=os.environ.get("WEB_PREVIEW", "1") == "1")
    parser.add_argument("--web-preview-host", default=os.environ.get("WEB_PREVIEW_HOST", "0.0.0.0"))
    parser.add_argument("--web-preview-port", type=int, default=int(os.environ.get("WEB_PREVIEW_PORT", "8090")))
    parser.add_argument("--web-preview-quality", type=int, default=int(os.environ.get("WEB_PREVIEW_QUALITY", "70")))
    parser.add_argument("--web-preview-fps", type=int, default=int(os.environ.get("WEB_PREVIEW_FPS", "8")))
    parser.add_argument("--web-preview-jpeg-fps", type=int, default=int(os.environ.get("WEB_PREVIEW_JPEG_FPS", "5")))
    parser.add_argument("--web-preview-max-width", type=int, default=int(os.environ.get("WEB_PREVIEW_MAX_WIDTH", "640")))
    parser.add_argument("--workflow-api-base-url", default="")
    parser.add_argument("--workflow-api-key", default="")
    parser.add_argument("--workflow-user", default=os.environ.get("WORKFLOW_USER", ""))
    parser.add_argument("--workflow-timeout-sec", type=float, default=float(os.environ.get("WORKFLOW_TIMEOUT_SEC", "20")))
    parser.add_argument("--workflow-snapshot-base-url", default=os.environ.get("WORKFLOW_SNAPSHOT_BASE_URL", ""))
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()

    missing = [
        name
        for name, value in (
            ("cloud-url", args.cloud_url),
            ("cloud-token", args.cloud_token),
            ("gateway-id", args.gateway_id),
            ("device-id", args.device_id),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"missing required args/env: {', '.join(missing)}")
    if not os.path.exists(args.model_path):
        raise SystemExit(f"model not found: {args.model_path}")
    return args


if __name__ == "__main__":
    BlindOccupancyEdgeApp(parse_args()).run()
