from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from .analysis import AnalysisDecision, AnalysisProviderConfig, ExternalAnalysisClient, local_quality_analysis
except ImportError:  # Production release imports app.py as a top-level module.
    from analysis import AnalysisDecision, AnalysisProviderConfig, ExternalAnalysisClient, local_quality_analysis


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("VISIONBRIDGE_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "visionbridge.db"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
VOLUNTEER_UPLOAD_DIR = DATA_DIR / "volunteer_uploads"
INGEST_TOKEN = os.getenv("VISIONBRIDGE_INGEST_TOKEN", "")
AUTH_SECRET = os.getenv("VISIONBRIDGE_AUTH_SECRET", INGEST_TOKEN or "visionbridge-development-only")
DEFAULT_LNG = float(os.getenv("VISIONBRIDGE_DEFAULT_LNG", "121.138923"))
DEFAULT_LAT = float(os.getenv("VISIONBRIDGE_DEFAULT_LAT", "28.632112"))
SMTP_HOST = os.getenv("VISIONBRIDGE_SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("VISIONBRIDGE_SMTP_PORT", "465"))
SMTP_USER = os.getenv("VISIONBRIDGE_SMTP_USER", "")
SMTP_AUTH_CODE = os.getenv("VISIONBRIDGE_SMTP_AUTH_CODE", "")
SMTP_FROM_NAME = os.getenv("VISIONBRIDGE_SMTP_FROM_NAME", "视桥志愿者平台")
MEDIA_API_URL = os.getenv("VISIONBRIDGE_MEDIA_API_URL", "http://127.0.0.1:9997").rstrip("/")
MEDIA_PUBLISH_SECRET = os.getenv("VISIONBRIDGE_MEDIA_PUBLISH_SECRET", "")
EMAIL_DEBUG = os.getenv("VISIONBRIDGE_EMAIL_DEBUG", "0") == "1"
AUTH_CODE_TTL_MINUTES = 10
AUTH_TOKEN_TTL_DAYS = 30
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
TZ = timezone(timedelta(hours=8))
DB_LOCK = threading.RLock()
ANALYSIS_CONFIG = AnalysisProviderConfig.from_env()
ANALYSIS_CLIENT = ExternalAnalysisClient(ANALYSIS_CONFIG)
ANALYSIS_STOP = threading.Event()
ANALYSIS_THREAD: threading.Thread | None = None
REALTIME_LOCK = threading.Lock()
REALTIME_VERSION = 0
REALTIME_EVENT: dict[str, Any] = {"type": "system.ready", "time": ""}

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
REPORT_CATEGORIES = {
    "temporary_obstacle": "临时杂物/堆放",
    "shop_step": "店铺台阶/固定高差",
    "construction": "临时施工",
    "road_damage": "路面坑洼/破损",
    "vehicle": "车辆占用",
    "other": "其他障碍",
}
CLEANUP_REASONS = {
    "unable_now": "当时不方便清理",
    "fixed_barrier": "固定障碍无法移动",
    "unsafe_to_clear": "不具备安全处理条件",
}
PRIORITY_SEVERITY = {"low": "attention", "normal": "warning", "urgent": "critical"}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(TZ)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)
    except ValueError:
        return datetime.now(TZ)


def publish_realtime(event_type: str, payload: dict[str, Any] | None = None) -> None:
    global REALTIME_VERSION, REALTIME_EVENT
    with REALTIME_LOCK:
        REALTIME_VERSION += 1
        REALTIME_EVENT = {
            "type": event_type,
            "version": REALTIME_VERSION,
            "time": now_iso(),
            "payload": payload or {},
        }


def realtime_snapshot() -> tuple[int, dict[str, Any]]:
    with REALTIME_LOCK:
        return REALTIME_VERSION, dict(REALTIME_EVENT)


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
    finally:
        connection.close()


def ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    VOLUNTEER_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with DB_LOCK, db() as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS devices (
              device_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              point_name TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              status TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telemetry (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              device_id TEXT NOT NULL,
              received_at TEXT NOT NULL,
              source_ts TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_telemetry_device_time ON telemetry(device_id, received_at DESC);
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY,
              source_event_id TEXT,
              device_id TEXT NOT NULL,
              type TEXT NOT NULL,
              type_label TEXT NOT NULL,
              status TEXT NOT NULL,
              severity TEXT NOT NULL,
              confidence INTEGER NOT NULL DEFAULT 0,
              point_name TEXT NOT NULL,
              address TEXT NOT NULL,
              lat REAL NOT NULL,
              lng REAL NOT NULL,
              snapshot_url TEXT,
              source TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              duration_sec INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_source ON events(source_event_id) WHERE source_event_id IS NOT NULL AND source_event_id <> '';
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY,
              email TEXT NOT NULL UNIQUE,
              display_name TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT 'volunteer',
              status TEXT NOT NULL DEFAULT 'active',
              email_verified_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS email_verification_codes (
              email TEXT NOT NULL,
              purpose TEXT NOT NULL,
              code_hash TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              consumed_at TEXT,
              last_sent_at TEXT NOT NULL,
              window_started_at TEXT NOT NULL,
              request_count INTEGER NOT NULL DEFAULT 1,
              PRIMARY KEY(email, purpose)
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
              token_hash TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              revoked_at TEXT,
              FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, expires_at DESC);
            CREATE TABLE IF NOT EXISTS volunteer_reports (
              id TEXT PRIMARY KEY,
              reporter_id TEXT NOT NULL,
              category TEXT NOT NULL,
              cleanup_reason TEXT NOT NULL,
              description TEXT NOT NULL,
              address TEXT NOT NULL,
              lat REAL NOT NULL,
              lng REAL NOT NULL,
              photo_filename TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              priority TEXT NOT NULL DEFAULT 'normal',
              review_note TEXT NOT NULL DEFAULT '',
              reviewed_by TEXT,
              reviewed_at TEXT,
              obstacle_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(reporter_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_reports_status_time ON volunteer_reports(status, created_at DESC);
            CREATE TABLE IF NOT EXISTS obstacles (
              id TEXT PRIMARY KEY,
              report_id TEXT NOT NULL UNIQUE,
              event_id TEXT NOT NULL UNIQUE,
              category TEXT NOT NULL,
              category_label TEXT NOT NULL,
              description TEXT NOT NULL,
              address TEXT NOT NULL,
              lat REAL NOT NULL,
              lng REAL NOT NULL,
              photo_filename TEXT NOT NULL,
              priority TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open',
              source TEXT NOT NULL DEFAULT 'volunteer',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              resolved_at TEXT,
              FOREIGN KEY(report_id) REFERENCES volunteer_reports(id),
              FOREIGN KEY(event_id) REFERENCES events(id)
            );
            CREATE INDEX IF NOT EXISTS idx_obstacles_status_time ON obstacles(status, created_at DESC);
            CREATE TABLE IF NOT EXISTS public_tasks (
              id TEXT PRIMARY KEY,
              obstacle_id TEXT NOT NULL,
              title TEXT NOT NULL,
              description TEXT NOT NULL,
              priority TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open',
              assignee_id TEXT,
              completion_note TEXT NOT NULL DEFAULT '',
              completion_photo_filename TEXT,
              review_note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              claimed_at TEXT,
              submitted_at TEXT,
              verified_at TEXT,
              FOREIGN KEY(obstacle_id) REFERENCES obstacles(id),
              FOREIGN KEY(assignee_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status_time ON public_tasks(status, created_at DESC);
            CREATE TABLE IF NOT EXISTS task_activity (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id TEXT NOT NULL,
              actor_id TEXT,
              action TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              FOREIGN KEY(task_id) REFERENCES public_tasks(id)
            );
            CREATE TABLE IF NOT EXISTS raw_ingest (
              id TEXT PRIMARY KEY,
              source TEXT NOT NULL,
              source_id TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              content_type TEXT NOT NULL DEFAULT 'application/json',
              payload TEXT NOT NULL,
              photo_path TEXT,
              local_decision TEXT NOT NULL,
              received_at TEXT NOT NULL,
              UNIQUE(source, source_id),
              UNIQUE(source, payload_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_raw_ingest_time ON raw_ingest(received_at DESC);
            CREATE TABLE IF NOT EXISTS analysis_jobs (
              id TEXT PRIMARY KEY,
              raw_ingest_id TEXT NOT NULL UNIQUE,
              provider TEXT NOT NULL,
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt_at TEXT NOT NULL,
              last_error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(raw_ingest_id) REFERENCES raw_ingest(id)
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_jobs_status ON analysis_jobs(status, next_attempt_at);
            CREATE TABLE IF NOT EXISTS analysis_results (
              id TEXT PRIMARY KEY,
              job_id TEXT NOT NULL UNIQUE,
              raw_ingest_id TEXT NOT NULL,
              provider TEXT NOT NULL,
              workflow_run_id TEXT NOT NULL DEFAULT '',
              valid INTEGER NOT NULL,
              canonical_category TEXT NOT NULL,
              priority TEXT NOT NULL,
              quality_score REAL NOT NULL,
              duplicate_risk REAL NOT NULL,
              needs_manual_review INTEGER NOT NULL,
              summary TEXT NOT NULL,
              quality_flags TEXT NOT NULL,
              raw_output TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES analysis_jobs(id),
              FOREIGN KEY(raw_ingest_id) REFERENCES raw_ingest(id)
            );
            CREATE TABLE IF NOT EXISTS data_quality_flags (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              raw_ingest_id TEXT NOT NULL,
              code TEXT NOT NULL,
              severity TEXT NOT NULL,
              message TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(raw_ingest_id, code),
              FOREIGN KEY(raw_ingest_id) REFERENCES raw_ingest(id)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              entity_type TEXT NOT NULL,
              entity_id TEXT NOT NULL,
              actor TEXT NOT NULL,
              action TEXT NOT NULL,
              detail TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id, created_at DESC);
            """
        )
        ensure_column(connection, "events", "analysis_status", "TEXT NOT NULL DEFAULT 'local_validated'")
        ensure_column(connection, "events", "quality_score", "REAL")
        ensure_column(connection, "events", "analysis_summary", "TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "events", "analysis_provider", "TEXT NOT NULL DEFAULT 'local'")
        ensure_column(connection, "volunteer_reports", "analysis_status", "TEXT NOT NULL DEFAULT 'local_validated'")
        ensure_column(connection, "volunteer_reports", "quality_score", "REAL")
        ensure_column(connection, "volunteer_reports", "analysis_summary", "TEXT NOT NULL DEFAULT ''")
        connection.execute("DELETE FROM events WHERE source='历史演示样例' OR id LIKE 'VB-DEMO-%'")
        backfill_analysis_records(connection)
        sync_analysis_results(connection)
        connection.commit()


def event_label(event_type: str) -> str:
    return {
        "non_motor_vehicle": "非机动车占用",
        "motor_vehicle": "两轮机动车占用",
        "construction_obstacle": "施工杂物占用",
        "person": "行人滞留",
    }.get(event_type, "其他障碍物占用")


def severity_for(alert_code: int, confidence: int) -> str:
    if alert_code >= 3 or confidence >= 90:
        return "critical"
    if alert_code >= 2 or confidence >= 70:
        return "warning"
    return "attention"


def status_label(status: str) -> str:
    return {"suspected": "疑似", "active": "未接单", "dispatched": "处置中", "cleared": "已闭环"}.get(status, status)


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(status_code=422, detail="invalid email address")
    return email


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def haversine_meters(lat_a: float, lng_a: float, lat_b: float, lng_b: float) -> float:
    radius = 6_371_000.0
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    d_phi = math.radians(lat_b - lat_a)
    d_lng = math.radians(lng_b - lng_a)
    value = math.sin(d_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lng / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def audit(
    connection: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    action: str,
    detail: dict[str, Any] | str | None = None,
    actor: str = "system",
) -> None:
    serialized = detail if isinstance(detail, str) else json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":"))
    connection.execute(
        "INSERT INTO audit_log(entity_type,entity_id,actor,action,detail,created_at) VALUES(?,?,?,?,?,?)",
        (entity_type, entity_id, actor, action, serialized, now_iso()),
    )


def recent_duplicate_risk(connection: sqlite3.Connection, inputs: dict[str, Any]) -> float:
    try:
        lat, lng = float(inputs.get("lat")), float(inputs.get("lng"))
    except (TypeError, ValueError):
        return 0.0
    category = str(inputs.get("category") or "")
    cutoff = (datetime.now(TZ) - timedelta(minutes=30)).isoformat(timespec="seconds")
    rows = connection.execute(
        "SELECT payload FROM raw_ingest WHERE received_at>=? ORDER BY received_at DESC LIMIT 100",
        (cutoff,),
    ).fetchall()
    closest = 10_000.0
    for row in rows:
        try:
            previous = json.loads(row["payload"]).get("analysisInput") or {}
            if str(previous.get("category") or "") != category:
                continue
            distance = haversine_meters(lat, lng, float(previous.get("lat")), float(previous.get("lng")))
            closest = min(closest, distance)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    if closest <= 10:
        return 0.95
    if closest <= 20:
        return 0.8
    if closest <= 50:
        return 0.45
    return 0.0


def register_raw_ingest(
    connection: sqlite3.Connection,
    *,
    source: Literal["edge", "volunteer"],
    source_id: str,
    analysis_input: dict[str, Any],
    content_type: str = "application/json",
    photo_path: str | None = None,
    photo_sha256: str = "",
) -> tuple[str, AnalysisDecision, bool]:
    normalized = dict(analysis_input)
    normalized["source"] = source
    normalized["sourceId"] = source_id
    normalized["duplicateRisk"] = recent_duplicate_risk(connection, normalized)
    envelope = {"analysisInput": normalized, "photoSha256": photo_sha256}
    canonical = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload_hash = digest_text(canonical)
    decision = local_quality_analysis(normalized)
    raw_id = f"RAW-{uuid.uuid4().hex[:16].upper()}"
    timestamp = now_iso()
    inserted = connection.execute(
        "INSERT OR IGNORE INTO raw_ingest(id,source,source_id,payload_hash,content_type,payload,photo_path,local_decision,received_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            raw_id,
            source,
            source_id,
            payload_hash,
            content_type,
            canonical,
            photo_path,
            json.dumps(decision.as_dict(), ensure_ascii=False, separators=(",", ":")),
            timestamp,
        ),
    ).rowcount
    if not inserted:
        existing = connection.execute(
            "SELECT id,local_decision FROM raw_ingest WHERE (source=? AND source_id=?) OR (source=? AND payload_hash=?) LIMIT 1",
            (source, source_id, source, payload_hash),
        ).fetchone()
        if existing is None:
            raise RuntimeError("raw ingest deduplication failed")
        return existing["id"], AnalysisDecision.from_data(json.loads(existing["local_decision"])), True

    messages = {
        "unknown_category": "障碍类别不在标准字典中",
        "description_too_short": "描述过短，建议人工补充",
        "invalid_coordinates": "坐标超出合法范围",
        "missing_coordinates": "缺少有效坐标",
        "low_model_confidence": "边缘模型置信度偏低",
        "probable_duplicate": "相近时间与位置存在同类上报",
    }
    for code in decision.quality_flags:
        connection.execute(
            "INSERT OR IGNORE INTO data_quality_flags(raw_ingest_id,code,severity,message,created_at) VALUES(?,?,?,?,?)",
            (raw_id, code, "warning" if code not in {"invalid_coordinates", "missing_coordinates"} else "error", messages.get(code, code), timestamp),
        )
    job_id = f"AN-{uuid.uuid4().hex[:14].upper()}"
    job_status = "queued" if ANALYSIS_CONFIG.enabled else "pending_config"
    connection.execute(
        "INSERT INTO analysis_jobs(id,raw_ingest_id,provider,status,attempts,next_attempt_at,last_error,created_at,updated_at) "
        "VALUES(?,?,?,?,0,?,'',?,?)",
        (job_id, raw_id, ANALYSIS_CONFIG.mode, job_status, timestamp, timestamp, timestamp),
    )
    audit(connection, "raw_ingest", raw_id, "accepted", {"source": source, "sourceId": source_id, "quality": decision.quality_score})
    return raw_id, decision, False


def backfill_analysis_records(connection: sqlite3.Connection) -> None:
    """Queue real pre-migration records exactly once without inventing data."""
    reports = connection.execute(
        "SELECT r.* FROM volunteer_reports r LEFT JOIN raw_ingest raw "
        "ON raw.source='volunteer' AND raw.source_id=r.id "
        "WHERE r.status<>'deleted' AND raw.id IS NULL"
    ).fetchall()
    for report in reports:
        _, decision, _ = register_raw_ingest(
            connection,
            source="volunteer",
            source_id=report["id"],
            analysis_input={
                "category": report["category"],
                "cleanupReason": report["cleanup_reason"],
                "description": report["description"],
                "address": report["address"],
                "lat": report["lat"],
                "lng": report["lng"],
                "confidence": 0,
                "durationSec": 0,
                "timestamp": report["created_at"],
                "reporterId": report["reporter_id"],
                "snapshotUrl": f"/api/v1/admin/reports/{report['id']}/photo",
                "priority": report["priority"],
            },
            content_type="image/unknown",
            photo_path=report["photo_filename"],
        )
        connection.execute(
            "UPDATE volunteer_reports SET analysis_status=?,quality_score=?,analysis_summary=? WHERE id=?",
            (
                "queued" if ANALYSIS_CONFIG.enabled else "local_validated",
                decision.quality_score,
                decision.summary,
                report["id"],
            ),
        )

    events = connection.execute(
        "SELECT e.* FROM events e LEFT JOIN raw_ingest raw "
        "ON raw.source='edge' AND raw.source_id=e.source_event_id "
        "WHERE e.device_id<>'volunteer-app' AND e.source_event_id IS NOT NULL "
        "AND e.source_event_id<>'' AND raw.id IS NULL"
    ).fetchall()
    for event in events:
        _, decision, _ = register_raw_ingest(
            connection,
            source="edge",
            source_id=event["source_event_id"],
            analysis_input={
                "category": event["type"],
                "description": f"{event['point_name']} 历史真实边缘事件回填",
                "address": event["address"],
                "lat": event["lat"],
                "lng": event["lng"],
                "confidence": event["confidence"],
                "durationSec": event["duration_sec"],
                "timestamp": event["created_at"],
                "deviceId": event["device_id"],
                "snapshotUrl": event["snapshot_url"] or "",
            },
            photo_path=event["snapshot_url"],
        )
        connection.execute(
            "UPDATE events SET analysis_status=?,quality_score=?,analysis_summary=?,analysis_provider='local' WHERE id=?",
            (
                "queued" if ANALYSIS_CONFIG.enabled else "local_validated",
                decision.quality_score,
                decision.summary,
                event["id"],
            ),
        )


def sync_analysis_results(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT raw.source,raw.source_id,result.quality_score,result.summary,result.provider "
        "FROM analysis_results result JOIN raw_ingest raw ON raw.id=result.raw_ingest_id"
    ).fetchall()
    for row in rows:
        if row["source"] == "edge":
            connection.execute(
                "UPDATE events SET analysis_status='succeeded',quality_score=?,analysis_summary=?,analysis_provider=? WHERE source_event_id=?",
                (row["quality_score"], row["summary"], row["provider"], row["source_id"]),
            )
        else:
            connection.execute(
                "UPDATE volunteer_reports SET analysis_status='succeeded',quality_score=?,analysis_summary=? WHERE id=?",
                (row["quality_score"], row["summary"], row["source_id"]),
            )
            connection.execute(
                "UPDATE events SET analysis_status='succeeded',quality_score=?,analysis_summary=?,analysis_provider=? WHERE source_event_id=?",
                (row["quality_score"], row["summary"], row["provider"], row["source_id"]),
            )

def analysis_worker_loop() -> None:
    while not ANALYSIS_STOP.wait(1.0):
        timestamp = now_iso()
        with DB_LOCK, db() as connection:
            row = connection.execute(
                "SELECT j.*,r.source,r.source_id,r.payload FROM analysis_jobs j "
                "JOIN raw_ingest r ON r.id=j.raw_ingest_id "
                "WHERE j.status IN ('queued','retry') AND j.next_attempt_at<=? ORDER BY j.created_at LIMIT 1",
                (timestamp,),
            ).fetchone()
            if row is None:
                continue
            connection.execute(
                "UPDATE analysis_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",
                (timestamp, row["id"]),
            )
            connection.commit()
        try:
            payload = json.loads(row["payload"])
            decision, run_id, raw_output = ANALYSIS_CLIENT.run(payload["analysisInput"])
            completed = now_iso()
            with DB_LOCK, db() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO analysis_results(id,job_id,raw_ingest_id,provider,workflow_run_id,valid,canonical_category,priority,quality_score,duplicate_risk,needs_manual_review,summary,quality_flags,raw_output,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"AR-{uuid.uuid4().hex[:14].upper()}", row["id"], row["raw_ingest_id"], ANALYSIS_CONFIG.mode,
                        run_id, int(decision.valid), decision.canonical_category, decision.priority,
                        decision.quality_score, decision.duplicate_risk, int(decision.needs_manual_review), decision.summary,
                        json.dumps(decision.quality_flags, ensure_ascii=False),
                        json.dumps(raw_output, ensure_ascii=False, separators=(",", ":")), completed,
                    ),
                )
                connection.execute(
                    "UPDATE analysis_jobs SET status='succeeded',last_error='',updated_at=? WHERE id=?",
                    (completed, row["id"]),
                )
                if row["source"] == "edge":
                    connection.execute(
                        "UPDATE events SET analysis_status='succeeded',quality_score=?,analysis_summary=?,analysis_provider=? WHERE source_event_id=?",
                        (decision.quality_score, decision.summary, ANALYSIS_CONFIG.mode, row["source_id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE volunteer_reports SET analysis_status='succeeded',quality_score=?,analysis_summary=? WHERE id=?",
                        (decision.quality_score, decision.summary, row["source_id"]),
                    )
                    connection.execute(
                        "UPDATE events SET analysis_status='succeeded',quality_score=?,analysis_summary=?,analysis_provider=? WHERE source_event_id=?",
                        (decision.quality_score, decision.summary, ANALYSIS_CONFIG.mode, row["source_id"]),
                    )
                audit(connection, "analysis_job", row["id"], "succeeded", {"runId": run_id, "provider": ANALYSIS_CONFIG.mode})
                connection.commit()
            publish_realtime("analysis.completed", {"source": row["source"], "sourceId": row["source_id"]})
        except Exception as exc:
            attempts = int(row["attempts"]) + 1
            retry = attempts < 5
            delay = min(300, 2 ** attempts * 5)
            next_attempt = (datetime.now(TZ) + timedelta(seconds=delay)).isoformat(timespec="seconds")
            with DB_LOCK, db() as connection:
                connection.execute(
                    "UPDATE analysis_jobs SET status=?,next_attempt_at=?,last_error=?,updated_at=? WHERE id=?",
                    ("retry" if retry else "failed", next_attempt, str(exc)[:1000], now_iso(), row["id"]),
                )
                audit(connection, "analysis_job", row["id"], "retry" if retry else "failed", {"error": str(exc)[:500]})
                connection.commit()


def start_analysis_worker() -> None:
    global ANALYSIS_THREAD
    ANALYSIS_STOP.clear()
    if not ANALYSIS_CONFIG.enabled:
        return
    ANALYSIS_THREAD = threading.Thread(target=analysis_worker_loop, name="visionbridge-analysis", daemon=True)
    ANALYSIS_THREAD.start()


def stop_analysis_worker() -> None:
    ANALYSIS_STOP.set()
    if ANALYSIS_THREAD:
        ANALYSIS_THREAD.join(timeout=3)


def verification_digest(email: str, purpose: str, code: str) -> str:
    message = f"{email}:{purpose}:{code}".encode("utf-8")
    return hmac.new(AUTH_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()


def send_verification_email(recipient: str, code: str) -> None:
    if EMAIL_DEBUG:
        return
    if not SMTP_USER or not SMTP_AUTH_CODE:
        raise HTTPException(status_code=503, detail="email service is not configured")
    message = EmailMessage()
    message["Subject"] = "视桥志愿者平台登录验证码"
    message["From"] = f"{SMTP_FROM_NAME} <{SMTP_USER}>"
    message["To"] = recipient
    message.set_content(
        f"您的验证码是：{code}\n\n验证码 {AUTH_CODE_TTL_MINUTES} 分钟内有效。若非本人操作，请忽略此邮件。"
    )
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=12, context=context) as client:
            client.login(SMTP_USER, SMTP_AUTH_CODE)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise HTTPException(status_code=502, detail="verification email could not be sent") from exc


def user_from_authorization(authorization: str | None) -> sqlite3.Row:
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing user token")
    token_hash = digest_text(token)
    with DB_LOCK, db() as connection:
        row = connection.execute(
            "SELECT u.* FROM auth_sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.revoked_at IS NULL AND u.status='active'",
            (token_hash,),
        ).fetchone()
        session = connection.execute(
            "SELECT expires_at FROM auth_sessions WHERE token_hash=? AND revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
    if row is None or session is None or parse_time(session["expires_at"]) <= datetime.now(TZ):
        raise HTTPException(status_code=401, detail="invalid or expired user token")
    return row


def current_user(authorization: str | None = Header(default=None)) -> sqlite3.Row:
    return user_from_authorization(authorization)


def user_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "displayName": row["display_name"],
        "role": row["role"],
        "createdAt": row["created_at"],
    }


def report_payload(row: sqlite3.Row, photo_scope: str = "volunteer") -> dict[str, Any]:
    report_id = row["id"]
    photo_url = (
        f"/api/v1/admin/reports/{report_id}/photo"
        if photo_scope == "admin"
        else f"/api/v1/volunteer/reports/{report_id}/photo"
    )
    return {
        "id": report_id,
        "reporterId": row["reporter_id"],
        "category": row["category"],
        "categoryLabel": REPORT_CATEGORIES.get(row["category"], "其他障碍"),
        "cleanupReason": row["cleanup_reason"],
        "cleanupReasonLabel": CLEANUP_REASONS.get(row["cleanup_reason"], row["cleanup_reason"]),
        "description": row["description"],
        "address": row["address"],
        "lat": row["lat"],
        "lng": row["lng"],
        "photoUrl": photo_url,
        "status": row["status"],
        "canDelete": row["status"] in {"pending", "rejected"},
        "priority": row["priority"],
        "reviewNote": row["review_note"],
        "obstacleId": row["obstacle_id"],
        "analysisStatus": row["analysis_status"] if "analysis_status" in row.keys() else "local_validated",
        "qualityScore": row["quality_score"] if "quality_score" in row.keys() else None,
        "analysisSummary": row["analysis_summary"] if "analysis_summary" in row.keys() else "",
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def obstacle_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "eventId": row["event_id"],
        "category": row["category"],
        "categoryLabel": row["category_label"],
        "description": row["description"],
        "address": row["address"],
        "lat": row["lat"],
        "lng": row["lng"],
        "photoUrl": f"/api/v1/obstacles/{row['id']}/photo",
        "priority": row["priority"],
        "status": row["status"],
        "source": row["source"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "taskId": row["task_id"] if "task_id" in row.keys() else None,
        "taskStatus": row["task_status"] if "task_status" in row.keys() else None,
    }


def task_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "obstacleId": row["obstacle_id"],
        "title": row["title"],
        "description": row["description"],
        "priority": row["priority"],
        "status": row["status"],
        "assigneeId": row["assignee_id"],
        "assigneeName": row["assignee_name"] if "assignee_name" in row.keys() else None,
        "assigneeEmail": row["assignee_email"] if "assignee_email" in row.keys() else None,
        "completionNote": row["completion_note"],
        "reviewNote": row["review_note"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "claimedAt": row["claimed_at"],
        "submittedAt": row["submitted_at"],
        "verifiedAt": row["verified_at"],
        "category": row["category"],
        "categoryLabel": row["category_label"],
        "address": row["address"],
        "lat": row["lat"],
        "lng": row["lng"],
        "photoUrl": f"/api/v1/obstacles/{row['obstacle_id']}/photo",
    }


def save_upload(upload: UploadFile, content: bytes, prefix: str) -> str:
    content_type = (upload.content_type or "").lower()
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(content_type)
    if suffix is None:
        raise HTTPException(status_code=415, detail="only JPEG, PNG and WebP images are supported")
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="image must be between 1 byte and 8 MiB")
    filename = f"{prefix}-{uuid.uuid4().hex}{suffix}"
    (VOLUNTEER_UPLOAD_DIR / filename).write_bytes(content)
    return filename


def task_join_query(where: str = "") -> str:
    return (
        "SELECT t.*,o.category,o.category_label,o.address,o.lat,o.lng,"
        "u.display_name AS assignee_name,u.email AS assignee_email "
        "FROM public_tasks t JOIN obstacles o ON o.id=t.obstacle_id "
        "LEFT JOIN users u ON u.id=t.assignee_id " + where
    )


def row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "typeLabel": row["type_label"],
        "status": row["status"],
        "statusLabel": status_label(row["status"]),
        "severity": row["severity"],
        "confidence": row["confidence"],
        "pointName": row["point_name"],
        "address": row["address"],
        "lat": row["lat"],
        "lng": row["lng"],
        "snapshotUrl": row["snapshot_url"],
        "source": row["source"],
        "createdAt": row["created_at"],
        "durationSec": row["duration_sec"],
        "analysisStatus": row["analysis_status"] if "analysis_status" in row.keys() else "local_validated",
        "qualityScore": row["quality_score"] if "quality_score" in row.keys() else None,
        "analysisSummary": row["analysis_summary"] if "analysis_summary" in row.keys() else "",
        "analysisProvider": row["analysis_provider"] if "analysis_provider" in row.keys() else "local",
    }


def stream_path_for_device(device_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "-", device_id).strip("-") or "unknown"
    return f"devices/{safe_id}"


def default_device_name(device_id: str) -> str:
    match = re.search(r"(\d+)$", device_id)
    suffix = f"{int(match.group(1)):02d}" if match else device_id[-6:].upper()
    return f"视桥移动巡检终端 {suffix}"


def media_paths() -> dict[str, dict[str, Any]]:
    try:
        request = urllib.request.Request(
            f"{MEDIA_API_URL}/v3/paths/list",
            headers={"Accept": "application/json", "User-Agent": "VisionBridge-API/1.0"},
        )
        with urllib.request.urlopen(request, timeout=1.2) as response:
            body = json.loads(response.read().decode("utf-8"))
        return {
            str(item.get("name")): item
            for item in body.get("items", [])
            if isinstance(item, dict) and item.get("name")
        }
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def normalize_device(row: sqlite3.Row | None, paths: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    if row is None:
        stream_path = stream_path_for_device("uno-cloud-gateway-01")
        return {
            "id": "uno-cloud-gateway-01", "name": "视桥移动巡检终端 01", "status": "offline",
            "pointName": "blindway-point-01", "lastSeen": "", "cameraStatus": "等待接入", "gpsStatus": "等待接入",
            "cameraFps": 0, "inferenceMs": 0, "inferenceFps": 0, "sats": 0, "hdop": 0, "lat": DEFAULT_LAT, "lng": DEFAULT_LNG,
            "model": "YOLOv8 · ONNX v1", "streamPath": stream_path, "streamStatus": "offline",
            "streamReaders": 0, "webRtcUrl": f"/webrtc/{stream_path}/", "hlsUrl": f"/hls/{stream_path}/",
        }
    payload = json.loads(row["payload"])
    runtime = payload.get("runtime", {})
    gps = payload.get("gps", {})
    props = payload.get("iot_properties", {})
    seen = parse_time(row["last_seen"])
    online = (datetime.now(TZ) - seen.astimezone(TZ)).total_seconds() < 90
    lat = gps.get("lat") or (props.get("gpsLatE6", 0) / 1_000_000) or DEFAULT_LAT
    lng = gps.get("lng") or (props.get("gpsLngE6", 0) / 1_000_000) or DEFAULT_LNG
    stream_path = stream_path_for_device(row["device_id"])
    media_path = (paths or {}).get(stream_path, {})
    stream_ready = bool(media_path.get("ready"))
    readers = media_path.get("readers") or []
    return {
        "id": row["device_id"], "name": row["name"], "status": "online" if online else "offline",
        "pointName": row["point_name"], "lastSeen": row["last_seen"],
        "cameraStatus": "streaming" if props.get("cameraStatusCode") == 1 else runtime.get("camera_status", "unknown"),
        "gpsStatus": "connected" if props.get("gpsStatusCode") == 1 else gps.get("status", "unknown"),
        "cameraFps": round(float(runtime.get("camera_fps") or props.get("cameraFpsX100", 0) / 100), 1),
        "inferenceMs": int(runtime.get("inference_ms") or props.get("inferenceMs", 0)),
        "inferenceFps": round(float(runtime.get("inference_fps") or 0), 2),
        "sats": int(gps.get("num_sats") or props.get("numSats", 0)),
        "hdop": round(float(gps.get("hdop") or props.get("hdopX100", 0) / 100), 2),
        "lat": float(lat), "lng": float(lng), "model": "YOLOv8 · ONNX v1",
        "streamPath": stream_path, "streamStatus": "live" if stream_ready else "offline",
        "streamReaders": len(readers) if isinstance(readers, list) else 0,
        "webRtcUrl": f"/webrtc/{stream_path}/", "hlsUrl": f"/hls/{stream_path}/",
    }


class EventAction(BaseModel):
    action: Literal["dispatch", "clear"]


class EmailCodeRequest(BaseModel):
    email: str
    purpose: Literal["login"] = "login"


class EmailCodeVerify(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)
    purpose: Literal["login"] = "login"
    display_name: str | None = Field(default=None, min_length=1, max_length=30, alias="displayName")


class AdminReportReview(BaseModel):
    action: Literal["approve", "reject"]
    note: str = Field(default="", max_length=500)
    publish_task: bool = Field(default=True, alias="publishTask")
    priority: Literal["low", "normal", "urgent"] = "normal"


class AdminTaskReview(BaseModel):
    action: Literal["verify", "return", "cancel", "reopen"]
    note: str = Field(default="", max_length=500)


class TelemetryEnvelope(BaseModel):
    ts: str | None = None
    device: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    gps: dict[str, Any] = Field(default_factory=dict)
    iot_properties: dict[str, Any] = Field(default_factory=dict)
    counts: dict[str, Any] = Field(default_factory=dict)
    dispatch: dict[str, Any] = Field(default_factory=dict)
    snapshot_b64: str | None = None
    snapshot_filename: str | None = None


class MediaAuthRequest(BaseModel):
    user: str = ""
    password: str = ""
    token: str = ""
    ip: str = ""
    action: str = ""
    path: str = ""
    protocol: str = ""
    id: str = ""
    query: str = ""


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    start_analysis_worker()
    publish_realtime("system.started", {"analysisProvider": ANALYSIS_CONFIG.mode})
    try:
        yield
    finally:
        stop_analysis_worker()


app = FastAPI(title="VisionBridge Cloud API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("VISIONBRIDGE_CORS_ORIGINS", "*").split(",")],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    location = ".".join(str(part) for part in (errors[0].get("loc", []) if errors else []))
    if "displayName" in location or "display_name" in location:
        detail = "志愿者昵称请填写 1–30 个字符，也可以留空使用默认昵称"
    elif "code" in location:
        detail = "请输入邮件中的 6 位验证码"
    elif "email" in location:
        detail = "请输入有效的邮箱地址"
    else:
        detail = "提交内容格式不正确，请检查后重试"
    return JSONResponse(status_code=422, content={"detail": detail})


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    with DB_LOCK, db() as connection:
        pending = connection.execute(
            "SELECT COUNT(*) FROM analysis_jobs WHERE status IN ('queued','retry','running','pending_config')"
        ).fetchone()[0]
    return {
        "status": "ok",
        "service": "visionbridge-api",
        "time": now_iso(),
        "analysis": {
            "provider": ANALYSIS_CONFIG.mode,
            "configured": ANALYSIS_CONFIG.enabled,
            "pending": pending,
        },
    }


@app.post("/api/v1/media/auth")
def authorize_media(request: MediaAuthRequest) -> dict[str, bool]:
    clean_path = (request.path or "").strip("/")
    valid_path = re.fullmatch(r"devices/[a-zA-Z0-9_-]+", clean_path) is not None
    if request.action in {"read", "playback"} and valid_path:
        return {"authorized": True}
    if request.action == "publish" and request.protocol == "rtsp" and valid_path:
        expected_path = stream_path_for_device(request.user)
        if (
            MEDIA_PUBLISH_SECRET
            and hmac.compare_digest(request.password, MEDIA_PUBLISH_SECRET)
            and hmac.compare_digest(clean_path, expected_path)
        ):
            return {"authorized": True}
    raise HTTPException(status_code=401, detail="media authorization denied")


@app.get("/api/v1/config/public")
def public_config() -> dict[str, Any]:
    return {
        "amapKey": os.getenv("AMAP_JS_KEY", ""),
        "amapSecurityCode": os.getenv("AMAP_SECURITY_CODE", ""),
        "defaultCenter": [DEFAULT_LNG, DEFAULT_LAT],
    }


@app.post("/api/v1/auth/email/request")
def request_email_code(payload: EmailCodeRequest) -> dict[str, Any]:
    email = normalize_email(payload.email)
    purpose = payload.purpose
    current = datetime.now(TZ)
    with DB_LOCK, db() as connection:
        row = connection.execute(
            "SELECT * FROM email_verification_codes WHERE email=? AND purpose=?",
            (email, purpose),
        ).fetchone()
    request_count = 1
    window_started = current
    if row is not None:
        last_sent = parse_time(row["last_sent_at"])
        if (current - last_sent).total_seconds() < 60:
            raise HTTPException(status_code=429, detail="please wait before requesting another code")
        window_started = parse_time(row["window_started_at"])
        if (current - window_started).total_seconds() >= 3600:
            window_started = current
        else:
            request_count = int(row["request_count"]) + 1
            if request_count > 5:
                raise HTTPException(status_code=429, detail="too many verification emails")

    code = f"{secrets.randbelow(1_000_000):06d}"
    send_verification_email(email, code)
    sent_at = current.isoformat(timespec="seconds")
    expires_at = (current + timedelta(minutes=AUTH_CODE_TTL_MINUTES)).isoformat(timespec="seconds")
    with DB_LOCK, db() as connection:
        connection.execute(
            "INSERT INTO email_verification_codes(email,purpose,code_hash,expires_at,attempts,consumed_at,last_sent_at,window_started_at,request_count) "
            "VALUES(?,?,?,?,0,NULL,?,?,?) ON CONFLICT(email,purpose) DO UPDATE SET "
            "code_hash=excluded.code_hash,expires_at=excluded.expires_at,attempts=0,consumed_at=NULL,last_sent_at=excluded.last_sent_at,"
            "window_started_at=excluded.window_started_at,request_count=excluded.request_count",
            (email, purpose, verification_digest(email, purpose, code), expires_at, sent_at, window_started.isoformat(timespec="seconds"), request_count),
        )
        connection.commit()
    response: dict[str, Any] = {"sent": True, "expiresIn": AUTH_CODE_TTL_MINUTES * 60}
    if EMAIL_DEBUG:
        response["debugCode"] = code
    return response


@app.post("/api/v1/auth/email/verify")
def verify_email_code(payload: EmailCodeVerify) -> dict[str, Any]:
    email = normalize_email(payload.email)
    code = payload.code.strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=422, detail="verification code must contain 6 digits")
    current = datetime.now(TZ)
    with DB_LOCK, db() as connection:
        row = connection.execute(
            "SELECT * FROM email_verification_codes WHERE email=? AND purpose=?",
            (email, payload.purpose),
        ).fetchone()
        if row is None or row["consumed_at"]:
            raise HTTPException(status_code=400, detail="verification code is unavailable")
        if parse_time(row["expires_at"]) <= current:
            raise HTTPException(status_code=400, detail="verification code has expired")
        if int(row["attempts"]) >= 5:
            raise HTTPException(status_code=429, detail="verification attempts exceeded")
        expected = verification_digest(email, payload.purpose, code)
        if not hmac.compare_digest(expected, row["code_hash"]):
            connection.execute(
                "UPDATE email_verification_codes SET attempts=attempts+1 WHERE email=? AND purpose=?",
                (email, payload.purpose),
            )
            connection.commit()
            raise HTTPException(status_code=400, detail="verification code is incorrect")

        timestamp = current.isoformat(timespec="seconds")
        connection.execute(
            "UPDATE email_verification_codes SET consumed_at=? WHERE email=? AND purpose=?",
            (timestamp, email, payload.purpose),
        )
        user = connection.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        display_name = (payload.display_name or "").strip()
        if user is None:
            display_name = display_name or email.split("@", 1)[0][:30] or "视桥志愿者"
            user_id = f"USR-{uuid.uuid4().hex[:12].upper()}"
            connection.execute(
                "INSERT INTO users(id,email,display_name,role,status,email_verified_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (user_id, email, display_name, "volunteer", "active", timestamp, timestamp, timestamp),
            )
        else:
            user_id = user["id"]
            if display_name:
                connection.execute("UPDATE users SET display_name=?,updated_at=? WHERE id=?", (display_name, timestamp, user_id))

        token = secrets.token_urlsafe(32)
        token_hash = digest_text(token)
        token_expires = (current + timedelta(days=AUTH_TOKEN_TTL_DAYS)).isoformat(timespec="seconds")
        connection.execute(
            "INSERT INTO auth_sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
            (token_hash, user_id, token_expires, timestamp),
        )
        connection.execute("DELETE FROM auth_sessions WHERE expires_at<?", (timestamp,))
        connection.commit()
        user = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return {"token": token, "tokenType": "Bearer", "expiresAt": token_expires, "user": user_payload(user)}


@app.get("/api/v1/auth/me")
def auth_me(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    return {"user": user_payload(user)}


@app.post("/api/v1/auth/logout")
def auth_logout(
    authorization: str | None = Header(default=None),
    _: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    token = (authorization or "").removeprefix("Bearer ").strip()
    with DB_LOCK, db() as connection:
        connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=?", (now_iso(), digest_text(token)))
        connection.commit()
    return {"loggedOut": True}


@app.post("/api/v1/volunteer/reports", status_code=201)
async def create_volunteer_report(
    category: str = Form(...),
    cleanup_reason: str = Form(..., alias="cleanupReason"),
    description: str = Form(...),
    address: str = Form(default=""),
    lat: float = Form(...),
    lng: float = Form(...),
    photo: UploadFile = File(...),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    if category not in REPORT_CATEGORIES:
        raise HTTPException(status_code=422, detail="unsupported report category")
    if cleanup_reason not in CLEANUP_REASONS:
        raise HTTPException(status_code=422, detail="unsupported cleanup reason")
    description = description.strip()
    if not 5 <= len(description) <= 500:
        raise HTTPException(status_code=422, detail="description must contain 5 to 500 characters")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(status_code=422, detail="invalid coordinates")
    content = await photo.read(MAX_UPLOAD_BYTES + 1)
    filename = save_upload(photo, content, "report")
    report_id = f"VBR-{datetime.now(TZ).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
    timestamp = now_iso()
    with DB_LOCK, db() as connection:
        connection.execute(
            "INSERT INTO volunteer_reports(id,reporter_id,category,cleanup_reason,description,address,lat,lng,photo_filename,status,priority,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'pending','normal',?,?)",
            (report_id, user["id"], category, cleanup_reason, description, address.strip() or "志愿者现场上报点位", lat, lng, filename, timestamp, timestamp),
        )
        _, decision, _ = register_raw_ingest(
            connection,
            source="volunteer",
            source_id=report_id,
            analysis_input={
                "category": category,
                "cleanupReason": cleanup_reason,
                "description": description,
                "address": address.strip(),
                "lat": lat,
                "lng": lng,
                "confidence": 0,
                "durationSec": 0,
                "timestamp": timestamp,
                "reporterId": user["id"],
                "snapshotUrl": f"/api/v1/admin/reports/{report_id}/photo",
            },
            content_type=photo.content_type or "application/octet-stream",
            photo_path=filename,
            photo_sha256=hashlib.sha256(content).hexdigest(),
        )
        connection.execute(
            "UPDATE volunteer_reports SET analysis_status=?,quality_score=?,analysis_summary=? WHERE id=?",
            (
                "queued" if ANALYSIS_CONFIG.enabled else "local_validated",
                decision.quality_score,
                decision.summary,
                report_id,
            ),
        )
        audit(connection, "volunteer_report", report_id, "created", {"category": category}, actor=user["id"])
        connection.commit()
        row = connection.execute("SELECT * FROM volunteer_reports WHERE id=?", (report_id,)).fetchone()
    publish_realtime("report.created", {"reportId": report_id})
    return {"report": report_payload(row)}


@app.get("/api/v1/volunteer/reports/mine")
def my_volunteer_reports(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with DB_LOCK, db() as connection:
        rows = connection.execute(
            "SELECT * FROM volunteer_reports WHERE reporter_id=? AND status<>'deleted' ORDER BY created_at DESC LIMIT 200",
            (user["id"],),
        ).fetchall()
    return {"items": [report_payload(row) for row in rows], "count": len(rows)}


@app.delete("/api/v1/volunteer/reports/{report_id}")
def delete_volunteer_report(report_id: str, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    """Delete an unapproved report owned by the current volunteer.

    Approved reports have already become public obstacle data and must be
    revoked by an operator instead of disappearing from the audit trail.
    """
    filename = ""
    with DB_LOCK, db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        report = connection.execute(
            "SELECT * FROM volunteer_reports WHERE id=? AND reporter_id=?",
            (report_id, user["id"]),
        ).fetchone()
        if report is None:
            connection.rollback()
            raise HTTPException(status_code=404, detail="report not found")
        if report["status"] not in {"pending", "rejected"}:
            connection.rollback()
            raise HTTPException(
                status_code=409,
                detail="approved reports are public records and cannot be deleted by the reporter",
            )
        filename = Path(report["photo_filename"]).name
        audit(connection, "volunteer_report", report_id, "deleted", {"previousStatus": report["status"]}, actor=user["id"])
        connection.execute("DELETE FROM volunteer_reports WHERE id=?", (report_id,))
        connection.commit()
    if filename:
        (VOLUNTEER_UPLOAD_DIR / filename).unlink(missing_ok=True)
    publish_realtime("report.deleted", {"reportId": report_id})
    return {"deleted": True, "reportId": report_id}


@app.get("/api/v1/volunteer/reports/{report_id}/photo")
def volunteer_report_photo(report_id: str, user: sqlite3.Row = Depends(current_user)):
    from fastapi.responses import FileResponse

    with DB_LOCK, db() as connection:
        row = connection.execute("SELECT * FROM volunteer_reports WHERE id=?", (report_id,)).fetchone()
    if row is None or row["reporter_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="report not found")
    path = VOLUNTEER_UPLOAD_DIR / Path(row["photo_filename"]).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="photo not found")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=300"})


@app.get("/api/v1/map/obstacles")
def map_obstacles(include_resolved: bool = Query(default=False, alias="includeResolved")) -> dict[str, Any]:
    query = (
        "SELECT o.*,t.id AS task_id,t.status AS task_status FROM obstacles o "
        "LEFT JOIN public_tasks t ON t.id=(SELECT latest.id FROM public_tasks latest "
        "WHERE latest.obstacle_id=o.id ORDER BY latest.created_at DESC LIMIT 1)"
    )
    if not include_resolved:
        query += " WHERE o.status IN ('open','assigned','resolving')"
    query += " ORDER BY CASE o.priority WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, o.created_at DESC LIMIT 1000"
    with DB_LOCK, db() as connection:
        rows = connection.execute(query).fetchall()
    return {"items": [obstacle_payload(row) for row in rows], "count": len(rows)}


@app.get("/api/v1/obstacles/{obstacle_id}/photo")
def obstacle_photo(obstacle_id: str):
    from fastapi.responses import FileResponse

    with DB_LOCK, db() as connection:
        row = connection.execute("SELECT photo_filename FROM obstacles WHERE id=?", (obstacle_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="obstacle not found")
    path = VOLUNTEER_UPLOAD_DIR / Path(row["photo_filename"]).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="photo not found")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/v1/volunteer/tasks/mine")
def my_volunteer_tasks(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with DB_LOCK, db() as connection:
        rows = connection.execute(
            task_join_query("WHERE t.assignee_id=? ORDER BY t.updated_at DESC LIMIT 200"),
            (user["id"],),
        ).fetchall()
    return {"items": [task_payload(row) for row in rows], "count": len(rows)}


@app.get("/api/v1/volunteer/tasks")
def volunteer_tasks(
    status: Literal["open", "claimed", "submitted", "verified"] = Query(default="open"),
    _: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    with DB_LOCK, db() as connection:
        rows = connection.execute(
            task_join_query(
                "WHERE t.status=? ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, t.created_at DESC LIMIT 300"
            ),
            (status,),
        ).fetchall()
    return {"items": [task_payload(row) for row in rows], "count": len(rows)}


@app.get("/api/v1/volunteer/tasks/{task_id}")
def volunteer_task_detail(task_id: str, _: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with DB_LOCK, db() as connection:
        row = connection.execute(task_join_query("WHERE t.id=?"), (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"task": task_payload(row)}


@app.post("/api/v1/volunteer/tasks/{task_id}/claim")
def claim_volunteer_task(task_id: str, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    timestamp = now_iso()
    with DB_LOCK, db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE public_tasks SET status='claimed',assignee_id=?,claimed_at=?,updated_at=? WHERE id=? AND status='open'",
            (user["id"], timestamp, timestamp, task_id),
        )
        if cursor.rowcount == 0:
            connection.rollback()
            if connection.execute("SELECT 1 FROM public_tasks WHERE id=?", (task_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="task not found")
            raise HTTPException(status_code=409, detail="task has already been claimed")
        task = connection.execute("SELECT obstacle_id FROM public_tasks WHERE id=?", (task_id,)).fetchone()
        connection.execute("UPDATE obstacles SET status='assigned',updated_at=? WHERE id=?", (timestamp, task["obstacle_id"]))
        connection.execute(
            "UPDATE events SET status='dispatched',updated_at=? WHERE id=(SELECT event_id FROM obstacles WHERE id=?)",
            (timestamp, task["obstacle_id"]),
        )
        connection.execute(
            "INSERT INTO task_activity(task_id,actor_id,action,note,created_at) VALUES(?,?,?,'',?)",
            (task_id, user["id"], "claimed", timestamp),
        )
        audit(connection, "task", task_id, "claimed", {"obstacleId": task["obstacle_id"]}, actor=user["id"])
        connection.commit()
        row = connection.execute(task_join_query("WHERE t.id=?"), (task_id,)).fetchone()
    publish_realtime("task.claimed", {"taskId": task_id, "assigneeId": user["id"]})
    return {"task": task_payload(row)}


@app.post("/api/v1/volunteer/tasks/{task_id}/complete")
async def complete_volunteer_task(
    task_id: str,
    note: str = Form(...),
    photo: UploadFile = File(...),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    note = note.strip()
    if not 3 <= len(note) <= 500:
        raise HTTPException(status_code=422, detail="completion note must contain 3 to 500 characters")
    content = await photo.read(MAX_UPLOAD_BYTES + 1)
    filename = save_upload(photo, content, "completion")
    timestamp = now_iso()
    with DB_LOCK, db() as connection:
        task = connection.execute("SELECT * FROM public_tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if task["assignee_id"] != user["id"] or task["status"] != "claimed":
            raise HTTPException(status_code=409, detail="task is not claimable by the current user")
        connection.execute(
            "UPDATE public_tasks SET status='submitted',completion_note=?,completion_photo_filename=?,submitted_at=?,updated_at=? WHERE id=?",
            (note, filename, timestamp, timestamp, task_id),
        )
        obstacle = connection.execute("SELECT * FROM obstacles WHERE id=?", (task["obstacle_id"],)).fetchone()
        connection.execute("UPDATE obstacles SET status='resolving',updated_at=? WHERE id=?", (timestamp, obstacle["id"]))
        connection.execute("UPDATE events SET status='dispatched',updated_at=? WHERE id=?", (timestamp, obstacle["event_id"]))
        connection.execute(
            "INSERT INTO task_activity(task_id,actor_id,action,note,created_at) VALUES(?,?,?,?,?)",
            (task_id, user["id"], "submitted", note, timestamp),
        )
        audit(connection, "task", task_id, "submitted", {"obstacleId": obstacle["id"]}, actor=user["id"])
        connection.commit()
        row = connection.execute(task_join_query("WHERE t.id=?"), (task_id,)).fetchone()
    publish_realtime("task.submitted", {"taskId": task_id})
    return {"task": task_payload(row)}


@app.get("/api/v1/admin/reports")
def admin_reports(status: Literal["pending", "approved", "rejected"] = Query(default="pending")) -> dict[str, Any]:
    with DB_LOCK, db() as connection:
        rows = connection.execute(
            "SELECT r.*,u.email,u.display_name FROM volunteer_reports r JOIN users u ON u.id=r.reporter_id "
            "WHERE r.status=? ORDER BY r.created_at DESC LIMIT 300",
            (status,),
        ).fetchall()
        count_rows = connection.execute(
            "SELECT status,COUNT(*) AS count FROM volunteer_reports WHERE status<>'deleted' GROUP BY status"
        ).fetchall()
    items = []
    for row in rows:
        item = report_payload(row, "admin")
        item["reporter"] = {"email": row["email"], "displayName": row["display_name"]}
        items.append(item)
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    counts.update({row["status"]: row["count"] for row in count_rows if row["status"] in counts})
    return {"items": items, "count": len(items), "counts": counts}


@app.get("/api/v1/admin/reports/{report_id}/photo")
def admin_report_photo(report_id: str):
    from fastapi.responses import FileResponse

    with DB_LOCK, db() as connection:
        row = connection.execute("SELECT photo_filename FROM volunteer_reports WHERE id=?", (report_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    path = VOLUNTEER_UPLOAD_DIR / Path(row["photo_filename"]).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="photo not found")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=300"})


@app.patch("/api/v1/admin/reports/{report_id}")
def review_volunteer_report(report_id: str, review: AdminReportReview) -> dict[str, Any]:
    timestamp = now_iso()
    with DB_LOCK, db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        report = connection.execute("SELECT * FROM volunteer_reports WHERE id=?", (report_id,)).fetchone()
        if report is None:
            connection.rollback()
            raise HTTPException(status_code=404, detail="report not found")
        if report["status"] != "pending":
            connection.rollback()
            raise HTTPException(status_code=409, detail="report has already been reviewed")
        if review.action == "reject":
            if len(review.note.strip()) < 2:
                connection.rollback()
                raise HTTPException(status_code=422, detail="a rejection note is required")
            connection.execute(
                "UPDATE volunteer_reports SET status='rejected',priority=?,review_note=?,reviewed_by='operator',reviewed_at=?,updated_at=? WHERE id=?",
                (review.priority, review.note.strip(), timestamp, timestamp, report_id),
            )
            audit(connection, "volunteer_report", report_id, "rejected", {"note": review.note.strip()}, actor="operator")
            connection.commit()
            row = connection.execute("SELECT * FROM volunteer_reports WHERE id=?", (report_id,)).fetchone()
            publish_realtime("report.reviewed", {"reportId": report_id, "status": "rejected"})
            return {"report": report_payload(row, "admin"), "obstacle": None, "task": None}

        obstacle_id = f"OBS-{uuid.uuid4().hex[:10].upper()}"
        event_id = f"VB-VOL-{datetime.now(TZ).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        category_label = REPORT_CATEGORIES.get(report["category"], "其他障碍")
        photo_url = f"/api/v1/obstacles/{obstacle_id}/photo"
        connection.execute(
            "INSERT INTO events(id,source_event_id,device_id,type,type_label,status,severity,confidence,point_name,address,lat,lng,snapshot_url,source,created_at,updated_at,duration_sec) "
            "VALUES(?,?,?,?,?,'active',?,0,?,?,?,?,?,'志愿者审核入库',?,?,0)",
            (event_id, report_id, "volunteer-app", report["category"], category_label, PRIORITY_SEVERITY[review.priority], report["address"], report["address"], report["lat"], report["lng"], photo_url, report["created_at"], timestamp),
        )
        connection.execute(
            "INSERT INTO obstacles(id,report_id,event_id,category,category_label,description,address,lat,lng,photo_filename,priority,status,source,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'open','volunteer',?,?)",
            (obstacle_id, report_id, event_id, report["category"], category_label, report["description"], report["address"], report["lat"], report["lng"], report["photo_filename"], review.priority, timestamp, timestamp),
        )
        connection.execute(
            "UPDATE volunteer_reports SET status='approved',priority=?,review_note=?,reviewed_by='operator',reviewed_at=?,obstacle_id=?,updated_at=? WHERE id=?",
            (review.priority, review.note.strip(), timestamp, obstacle_id, timestamp, report_id),
        )
        task_id = None
        if review.publish_task:
            task_id = f"VBT-{uuid.uuid4().hex[:10].upper()}"
            connection.execute(
                "INSERT INTO public_tasks(id,obstacle_id,title,description,priority,status,created_at,updated_at) VALUES(?,?,?,?,?,'open',?,?)",
                (task_id, obstacle_id, f"协助处理：{category_label}", report["description"], review.priority, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO task_activity(task_id,actor_id,action,note,created_at) VALUES(?,NULL,'published',?,?)",
                (task_id, review.note.strip(), timestamp),
            )
        audit(
            connection,
            "volunteer_report",
            report_id,
            "approved",
            {"obstacleId": obstacle_id, "taskId": task_id, "priority": review.priority},
            actor="operator",
        )
        connection.commit()
        reviewed = connection.execute("SELECT * FROM volunteer_reports WHERE id=?", (report_id,)).fetchone()
        obstacle = connection.execute("SELECT * FROM obstacles WHERE id=?", (obstacle_id,)).fetchone()
        task = connection.execute(task_join_query("WHERE t.id=?"), (task_id,)).fetchone() if task_id else None
    publish_realtime(
        "report.reviewed",
        {"reportId": report_id, "status": "approved", "obstacleId": obstacle_id, "taskId": task_id},
    )
    return {
        "report": report_payload(reviewed, "admin"),
        "obstacle": obstacle_payload(obstacle),
        "task": task_payload(task) if task is not None else None,
    }


@app.get("/api/v1/admin/tasks")
def admin_tasks(status: str | None = Query(default=None)) -> dict[str, Any]:
    where = ""
    params: list[Any] = []
    if status:
        where = "WHERE t.status=? "
        params.append(status)
    where += "ORDER BY t.updated_at DESC LIMIT 500"
    with DB_LOCK, db() as connection:
        rows = connection.execute(task_join_query(where), params).fetchall()
        count_rows = connection.execute(
            "SELECT status,COUNT(*) AS count FROM public_tasks GROUP BY status"
        ).fetchall()
    counts = {"open": 0, "claimed": 0, "submitted": 0, "verified": 0, "cancelled": 0}
    counts.update({row["status"]: row["count"] for row in count_rows if row["status"] in counts})
    return {"items": [task_payload(row) for row in rows], "count": len(rows), "counts": counts}


@app.get("/api/v1/admin/operations/summary")
def admin_operations_summary() -> dict[str, Any]:
    """Return one authoritative snapshot for the volunteer dispatch pipeline."""
    expected = {
        "open": ("open", "active"),
        "claimed": ("assigned", "dispatched"),
        "submitted": ("resolving", "dispatched"),
        "verified": ("resolved", "cleared"),
        "cancelled": ("open", "active"),
    }
    with DB_LOCK, db() as connection:
        report_rows = connection.execute(
            "SELECT status,COUNT(*) AS count FROM volunteer_reports WHERE status<>'deleted' GROUP BY status"
        ).fetchall()
        task_rows = connection.execute(
            "SELECT status,COUNT(*) AS count FROM public_tasks GROUP BY status"
        ).fetchall()
        obstacle_rows = connection.execute(
            "SELECT status,COUNT(*) AS count FROM obstacles GROUP BY status"
        ).fetchall()
        state_rows = connection.execute(
            "SELECT t.id,t.status AS task_status,o.status AS obstacle_status,e.status AS event_status "
            "FROM public_tasks t JOIN obstacles o ON o.id=t.obstacle_id JOIN events e ON e.id=o.event_id"
        ).fetchall()
    issues = []
    for row in state_rows:
        wanted = expected.get(row["task_status"])
        if wanted and (row["obstacle_status"], row["event_status"]) != wanted:
            issues.append({
                "taskId": row["id"],
                "taskStatus": row["task_status"],
                "obstacleStatus": row["obstacle_status"],
                "eventStatus": row["event_status"],
                "expectedObstacleStatus": wanted[0],
                "expectedEventStatus": wanted[1],
            })
    return {
        "reports": {row["status"]: row["count"] for row in report_rows},
        "tasks": {row["status"]: row["count"] for row in task_rows},
        "obstacles": {row["status"]: row["count"] for row in obstacle_rows},
        "consistent": not issues,
        "issueCount": len(issues),
        "issues": issues[:50],
        "generatedAt": now_iso(),
    }


@app.get("/api/v1/admin/tasks/{task_id}/evidence")
def admin_task_evidence(task_id: str):
    from fastapi.responses import FileResponse

    with DB_LOCK, db() as connection:
        row = connection.execute("SELECT completion_photo_filename FROM public_tasks WHERE id=?", (task_id,)).fetchone()
    if row is None or not row["completion_photo_filename"]:
        raise HTTPException(status_code=404, detail="completion evidence not found")
    path = VOLUNTEER_UPLOAD_DIR / Path(row["completion_photo_filename"]).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="completion evidence not found")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=300"})


@app.patch("/api/v1/admin/tasks/{task_id}")
def review_volunteer_task(task_id: str, review: AdminTaskReview) -> dict[str, Any]:
    timestamp = now_iso()
    with DB_LOCK, db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        task = connection.execute("SELECT * FROM public_tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            connection.rollback()
            raise HTTPException(status_code=404, detail="task not found")
        obstacle = connection.execute("SELECT * FROM obstacles WHERE id=?", (task["obstacle_id"],)).fetchone()
        if review.action == "verify":
            if task["status"] != "submitted":
                connection.rollback()
                raise HTTPException(status_code=409, detail="only submitted tasks can be verified")
            connection.execute(
                "UPDATE public_tasks SET status='verified',review_note=?,verified_at=?,updated_at=? WHERE id=?",
                (review.note.strip(), timestamp, timestamp, task_id),
            )
            connection.execute("UPDATE obstacles SET status='resolved',resolved_at=?,updated_at=? WHERE id=?", (timestamp, timestamp, obstacle["id"]))
            connection.execute("UPDATE events SET status='cleared',updated_at=? WHERE id=?", (timestamp, obstacle["event_id"]))
            action = "verified"
        elif review.action == "return":
            if task["status"] != "submitted":
                connection.rollback()
                raise HTTPException(status_code=409, detail="only submitted tasks can be returned")
            if len(review.note.strip()) < 2:
                connection.rollback()
                raise HTTPException(status_code=422, detail="a return note is required")
            connection.execute(
                "UPDATE public_tasks SET status='claimed',review_note=?,submitted_at=NULL,updated_at=? WHERE id=?",
                (review.note.strip(), timestamp, task_id),
            )
            connection.execute("UPDATE obstacles SET status='assigned',updated_at=? WHERE id=?", (timestamp, obstacle["id"]))
            connection.execute("UPDATE events SET status='dispatched',updated_at=? WHERE id=?", (timestamp, obstacle["event_id"]))
            action = "returned"
        elif review.action == "cancel":
            if task["status"] == "verified":
                connection.rollback()
                raise HTTPException(status_code=409, detail="verified tasks cannot be cancelled")
            connection.execute(
                "UPDATE public_tasks SET status='cancelled',review_note=?,updated_at=? WHERE id=?",
                (review.note.strip(), timestamp, task_id),
            )
            connection.execute("UPDATE obstacles SET status='open',updated_at=? WHERE id=?", (timestamp, obstacle["id"]))
            connection.execute("UPDATE events SET status='active',updated_at=? WHERE id=?", (timestamp, obstacle["event_id"]))
            action = "cancelled"
        else:
            if task["status"] != "cancelled":
                connection.rollback()
                raise HTTPException(status_code=409, detail="only cancelled tasks can be reopened")
            connection.execute(
                "UPDATE public_tasks SET status='open',assignee_id=NULL,completion_note='',"
                "completion_photo_filename=NULL,review_note=?,claimed_at=NULL,submitted_at=NULL,verified_at=NULL,updated_at=? WHERE id=?",
                (review.note.strip(), timestamp, task_id),
            )
            connection.execute("UPDATE obstacles SET status='open',resolved_at=NULL,updated_at=? WHERE id=?", (timestamp, obstacle["id"]))
            connection.execute("UPDATE events SET status='active',updated_at=? WHERE id=?", (timestamp, obstacle["event_id"]))
            action = "reopened"
        connection.execute(
            "INSERT INTO task_activity(task_id,actor_id,action,note,created_at) VALUES(?,NULL,?,?,?)",
            (task_id, action, review.note.strip(), timestamp),
        )
        audit(connection, "task", task_id, action, {"obstacleId": obstacle["id"]}, actor="operator")
        connection.commit()
        row = connection.execute(task_join_query("WHERE t.id=?"), (task_id,)).fetchone()
    publish_realtime(f"task.{action}", {"taskId": task_id})
    return {"task": task_payload(row)}


@app.post("/api/v1/telemetry", status_code=202)
def ingest_telemetry(payload: TelemetryEnvelope, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    provided = (authorization or "").removeprefix("Bearer ").strip()
    if not INGEST_TOKEN or not hmac.compare_digest(provided, INGEST_TOKEN):
        raise HTTPException(status_code=401, detail="invalid ingest token")
    body = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    device_id = str(payload.device.get("device_id") or payload.device.get("gateway_id") or "unknown-device")
    point_name = str(payload.device.get("point_name") or "blindway-point-01")
    received = now_iso()
    snapshot_url = None
    if payload.snapshot_b64 and payload.snapshot_filename:
        safe_name = f"{uuid.uuid4().hex[:10]}-{Path(payload.snapshot_filename).name}"
        try:
            decoded = base64.b64decode(payload.snapshot_b64, validate=True)
            if len(decoded) <= 4 * 1024 * 1024:
                (SNAPSHOT_DIR / safe_name).write_bytes(decoded)
                snapshot_url = f"/api/v1/snapshots/{safe_name}"
        except (ValueError, base64.binascii.Error):
            pass
    with DB_LOCK, db() as connection:
        existing_device = connection.execute("SELECT name FROM devices WHERE device_id=?", (device_id,)).fetchone()
        device_name = str(
            payload.device.get("name")
            or (existing_device["name"] if existing_device else "")
            or default_device_name(device_id)
        )
        connection.execute(
            "INSERT INTO telemetry(device_id,received_at,source_ts,payload) VALUES (?,?,?,?)",
            (device_id, received, payload.ts or received, json.dumps(body, ensure_ascii=False, separators=(",", ":"))),
        )
        connection.execute(
            "INSERT INTO devices(device_id,name,point_name,last_seen,status,payload) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET name=excluded.name,point_name=excluded.point_name,last_seen=excluded.last_seen,status=excluded.status,payload=excluded.payload",
            (device_id, device_name, point_name, received, "online", json.dumps(body, ensure_ascii=False, separators=(",", ":"))),
        )
        props = payload.iot_properties
        runtime = payload.runtime
        realtime_type = "telemetry.updated"
        realtime_payload: dict[str, Any] = {"deviceId": device_id}
        if int(props.get("eventActive", 0)) == 1:
            source_event_id = str(runtime.get("last_event_id") or f"{device_id}-{props.get('captureEpoch', 0)}")
            event_type = str(runtime.get("last_obstacle_type") or "construction_obstacle")
            raw_confidence = float(runtime.get("last_confidence") or props.get("obstacleConfidence", 0))
            confidence = int(round(raw_confidence * 100 if raw_confidence <= 1 else raw_confidence))
            lat = float(payload.gps.get("lat") or props.get("gpsLatE6", 0) / 1_000_000 or DEFAULT_LAT)
            lng = float(payload.gps.get("lng") or props.get("gpsLngE6", 0) / 1_000_000 or DEFAULT_LNG)
            event_id = f"VB-{parse_time(payload.ts).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
            created = parse_time(payload.ts).isoformat(timespec="seconds")
            previous = connection.execute(
                "SELECT id FROM events WHERE source_event_id=?", (source_event_id,)
            ).fetchone()
            connection.execute(
                "INSERT INTO events(id,source_event_id,device_id,type,type_label,status,severity,confidence,point_name,address,lat,lng,snapshot_url,source,created_at,updated_at,duration_sec) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_event_id) WHERE source_event_id IS NOT NULL AND source_event_id <> '' DO UPDATE SET confidence=excluded.confidence,updated_at=excluded.updated_at,duration_sec=CAST((julianday(excluded.updated_at)-julianday(events.created_at))*86400 AS INTEGER),snapshot_url=COALESCE(excluded.snapshot_url,events.snapshot_url)",
                (event_id, source_event_id, device_id, event_type, event_label(event_type), "active", severity_for(int(props.get("alertLevelCode", 0)), confidence), confidence, point_name, "移动巡检终端实时上报点位", lat, lng, snapshot_url, "边缘设备实机", created, received, 0),
            )
            current_event = connection.execute(
                "SELECT id FROM events WHERE source_event_id=?", (source_event_id,)
            ).fetchone()
            if previous is None:
                _, decision, _ = register_raw_ingest(
                    connection,
                    source="edge",
                    source_id=source_event_id,
                    analysis_input={
                        "category": event_type,
                        "description": f"{point_name} 连续检测确认的{event_label(event_type)}",
                        "address": "移动巡检终端实时上报点位",
                        "lat": lat,
                        "lng": lng,
                        "confidence": confidence,
                        "durationSec": 0,
                        "timestamp": created,
                        "gpsHdop": payload.gps.get("hdop") or props.get("hdopX100", 0) / 100,
                        "deviceId": device_id,
                        "snapshotUrl": snapshot_url,
                    },
                    photo_path=snapshot_url,
                )
                connection.execute(
                    "UPDATE events SET analysis_status=?,quality_score=?,analysis_summary=?,analysis_provider='local' WHERE source_event_id=?",
                    (
                        "queued" if ANALYSIS_CONFIG.enabled else "local_validated",
                        decision.quality_score,
                        decision.summary,
                        source_event_id,
                    ),
                )
                audit(connection, "event", current_event["id"], "created", {"sourceEventId": source_event_id, "deviceId": device_id})
            realtime_type = "event.updated"
            realtime_payload.update({"eventId": current_event["id"], "sourceEventId": source_event_id})
        elif str(runtime.get("event_state") or "").lower() == "cleared" and runtime.get("last_event_id"):
            source_event_id = str(runtime["last_event_id"])
            cleared = connection.execute(
                "UPDATE events SET status='cleared',updated_at=? WHERE source_event_id=? AND status='active'",
                (received, source_event_id),
            )
            if cleared.rowcount:
                current_event = connection.execute(
                    "SELECT id FROM events WHERE source_event_id=?", (source_event_id,)
                ).fetchone()
                audit(connection, "event", current_event["id"], "edge_cleared", {"deviceId": device_id})
                realtime_type = "event.cleared"
                realtime_payload.update({"eventId": current_event["id"], "sourceEventId": source_event_id})
        connection.execute("DELETE FROM telemetry WHERE id NOT IN (SELECT id FROM telemetry ORDER BY id DESC LIMIT 50000)")
        connection.commit()
    publish_realtime(realtime_type, realtime_payload)
    return {"accepted": True, "deviceId": device_id, "receivedAt": received}


@app.get("/api/v1/overview")
def overview() -> dict[str, Any]:
    paths = media_paths()
    current = datetime.now(TZ)
    today = current.date()
    yesterday = today - timedelta(days=1)
    labels = [f"{hour:02d}" for hour in range(0, 24, 3)]
    event_trend = [0] * len(labels)
    inference_samples: list[list[float]] = [[] for _ in labels]
    camera_samples: list[list[float]] = [[] for _ in labels]
    with DB_LOCK, db() as connection:
        device_rows = connection.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
        device_row = device_rows[0] if device_rows else None
        events = [row_to_event(row) for row in connection.execute(
            "SELECT * FROM events WHERE status IN ('active','dispatched') ORDER BY created_at DESC LIMIT 20"
        ).fetchall()]
        devices = [normalize_device(row, paths) for row in device_rows]
        device = devices[0] if devices else None
        online_devices = sum(1 for item in devices if item["status"] == "online")
        today_prefix = today.isoformat()
        yesterday_prefix = yesterday.isoformat()
        today_count = connection.execute("SELECT COUNT(*) FROM events WHERE created_at LIKE ?", (today_prefix + "%",)).fetchone()[0]
        yesterday_count = connection.execute("SELECT COUNT(*) FROM events WHERE created_at LIKE ?", (yesterday_prefix + "%",)).fetchone()[0]
        total = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        cleared = connection.execute("SELECT COUNT(*) FROM events WHERE status='cleared'").fetchone()[0]
        active = connection.execute("SELECT COUNT(*) FROM events WHERE status IN ('active','dispatched')").fetchone()[0]
        response_rows = connection.execute(
            "SELECT created_at,claimed_at FROM public_tasks WHERE claimed_at IS NOT NULL"
        ).fetchall()
        analysis_rows = connection.execute(
            "SELECT status,COUNT(*) AS count FROM analysis_jobs GROUP BY status"
        ).fetchall()
        today_events = connection.execute(
            "SELECT created_at FROM events WHERE created_at LIKE ?", (today_prefix + "%",)
        ).fetchall()
        telemetry_rows = connection.execute(
            "SELECT received_at,payload FROM telemetry WHERE received_at LIKE ? ORDER BY received_at",
            (today_prefix + "%",),
        ).fetchall()
    for row in today_events:
        event_time = parse_time(row["created_at"]).astimezone(TZ)
        event_trend[min(7, event_time.hour // 3)] += 1
    for row in telemetry_rows:
        try:
            telemetry_time = parse_time(row["received_at"]).astimezone(TZ)
            runtime = json.loads(row["payload"]).get("runtime") or {}
            bucket = min(7, telemetry_time.hour // 3)
            if runtime.get("inference_ms") is not None:
                inference_samples[bucket].append(float(runtime["inference_ms"]))
            if runtime.get("camera_fps") is not None:
                camera_samples[bucket].append(float(runtime["camera_fps"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    response_minutes = [
        max(0.0, (parse_time(row["claimed_at"]) - parse_time(row["created_at"])).total_seconds() / 60)
        for row in response_rows
    ]
    if yesterday_count:
        today_change = round((today_count - yesterday_count) / yesterday_count * 100, 1)
    else:
        today_change = 100.0 if today_count else 0.0
    analysis_counts = {row["status"]: row["count"] for row in analysis_rows}
    return {
        "generatedAt": now_iso(),
        "dataMode": "live" if devices or total else "empty",
        "linkStatus": "online" if online_devices else "offline",
        "kpis": {
            "onlineDevices": online_devices, "totalDevices": len(devices),
            "activeEvents": active, "todayEvents": today_count,
            "closureRate": round(cleared / total * 100) if total else 0,
            "averageResponseMin": round(sum(response_minutes) / len(response_minutes), 1) if response_minutes else None,
            "todayChangePercent": today_change,
        },
        "device": device,
        "recentEvents": events,
        "trends": {
            "labels": labels,
            "events": event_trend,
            "inferenceMs": [round(sum(values) / len(values)) if values else None for values in inference_samples],
            "cameraFps": [round(sum(values) / len(values), 1) if values else None for values in camera_samples],
        },
        "analysis": {
            "provider": ANALYSIS_CONFIG.mode,
            "configured": ANALYSIS_CONFIG.enabled,
            "jobs": analysis_counts,
        },
    }


@app.get("/api/v1/admin/analysis/status")
def analysis_status() -> dict[str, Any]:
    with DB_LOCK, db() as connection:
        rows = connection.execute(
            "SELECT status,COUNT(*) AS count FROM analysis_jobs GROUP BY status"
        ).fetchall()
        flagged = connection.execute("SELECT COUNT(*) FROM data_quality_flags").fetchone()[0]
    return {
        "provider": ANALYSIS_CONFIG.mode,
        "configured": ANALYSIS_CONFIG.enabled,
        "workflow": ANALYSIS_CONFIG.workflow,
        "jobs": {row["status"]: row["count"] for row in rows},
        "qualityFlagCount": flagged,
        "generatedAt": now_iso(),
    }


@app.get("/api/v1/admin/analysis/jobs")
def analysis_jobs(status: str | None = Query(default=None)) -> dict[str, Any]:
    query = (
        "SELECT j.*,r.source,r.source_id,r.received_at FROM analysis_jobs j "
        "JOIN raw_ingest r ON r.id=j.raw_ingest_id"
    )
    params: list[Any] = []
    if status:
        query += " WHERE j.status=?"
        params.append(status)
    query += " ORDER BY j.created_at DESC LIMIT 500"
    with DB_LOCK, db() as connection:
        rows = connection.execute(query, params).fetchall()
    return {"items": [dict(row) for row in rows], "count": len(rows)}


@app.post("/api/v1/admin/analysis/jobs/{job_id}/retry")
def retry_analysis_job(job_id: str) -> dict[str, Any]:
    if not ANALYSIS_CONFIG.enabled:
        raise HTTPException(status_code=409, detail="analysis provider is not configured")
    timestamp = now_iso()
    with DB_LOCK, db() as connection:
        cursor = connection.execute(
            "UPDATE analysis_jobs SET status='queued',next_attempt_at=?,last_error='',updated_at=? WHERE id=?",
            (timestamp, timestamp, job_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="analysis job not found")
        audit(connection, "analysis_job", job_id, "manually_retried", actor="operator")
        connection.commit()
    publish_realtime("analysis.queued", {"jobId": job_id})
    return {"queued": True, "jobId": job_id}


@app.get("/api/v1/events")
def list_events(status: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    query = "SELECT * FROM events"
    params: list[Any] = []
    if status:
        query += " WHERE status=?"
        params.append(status)
    else:
        query += " WHERE status IN ('active','dispatched')"
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with DB_LOCK, db() as connection:
        rows = connection.execute(query, params).fetchall()
    return {"items": [row_to_event(row) for row in rows], "count": len(rows)}


@app.patch("/api/v1/events/{event_id}")
def update_event(event_id: str, action: EventAction) -> dict[str, Any]:
    status = "dispatched" if action.action == "dispatch" else "cleared"
    with DB_LOCK, db() as connection:
        volunteer_obstacle = connection.execute(
            "SELECT 1 FROM obstacles WHERE event_id=? AND source='volunteer'", (event_id,)
        ).fetchone()
        if volunteer_obstacle is not None:
            raise HTTPException(
                status_code=409,
                detail="volunteer events must be closed through task verification",
            )
        cursor = connection.execute("UPDATE events SET status=?,updated_at=? WHERE id=?", (status, now_iso(), event_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="event not found")
        audit(connection, "event", event_id, status, actor="operator")
        connection.commit()
        row = connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    publish_realtime("event.updated", {"eventId": event_id, "status": status})
    return row_to_event(row)


@app.get("/api/v1/devices")
def list_devices() -> dict[str, Any]:
    with DB_LOCK, db() as connection:
        rows = connection.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
    paths = media_paths()
    return {"items": [normalize_device(row, paths) for row in rows], "count": len(rows), "generatedAt": now_iso()}


@app.get("/api/v1/devices/{device_id}")
def get_device(device_id: str) -> dict[str, Any]:
    with DB_LOCK, db() as connection:
        row = connection.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="device not found")
    return normalize_device(row, media_paths())


@app.get("/api/v1/snapshots/{filename}")
def snapshot_metadata(filename: str):
    from fastapi.responses import FileResponse
    safe = Path(filename).name
    path = SNAPSHOT_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="snapshot not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@app.websocket("/ws/realtime")
async def realtime(websocket: WebSocket):
    await websocket.accept()
    last_version = -1
    last_heartbeat = 0.0
    try:
        while True:
            version, event = realtime_snapshot()
            current_monotonic = time.monotonic()
            if version != last_version:
                await websocket.send_json(event)
                last_version = version
                last_heartbeat = current_monotonic
            elif current_monotonic - last_heartbeat >= 15:
                await websocket.send_json({"type": "heartbeat", "version": version, "time": now_iso()})
                last_heartbeat = current_monotonic
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return


if os.getenv("VISIONBRIDGE_SERVE_STATIC", "0") == "1":
    from fastapi.staticfiles import StaticFiles

    repository_root = BASE_DIR.parents[1]
    dashboard_static = repository_root / "apps" / "dashboard" / "static-deploy"
    volunteer_static = repository_root / "apps" / "volunteer" / "build" / "web"
    if volunteer_static.exists():
        app.mount("/volunteer", StaticFiles(directory=volunteer_static, html=True), name="volunteer-app")
    if dashboard_static.exists():
        app.mount("/", StaticFiles(directory=dashboard_static, html=True), name="dashboard")
