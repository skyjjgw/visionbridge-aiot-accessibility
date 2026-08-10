#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${VISIONBRIDGE_EDGE_ENV_FILE:-/etc/visionbridge/edge.env}"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "missing env file: ${ENV_FILE}" >&2
    exit 1
fi

set -a
source "${ENV_FILE}"
set +a

mkdir -p "${SNAPSHOTS_DIR:-${SCRIPT_DIR}/snapshots}"

ARGS=(
    "--model-path" "${MODEL_PATH}"
    "--cloud-url" "${VISIONBRIDGE_CLOUD_URL}"
    "--cloud-token" "${VISIONBRIDGE_INGEST_TOKEN}"
    "--cloud-timeout-sec" "${VISIONBRIDGE_CLOUD_TIMEOUT_SEC:-12}"
    "--cloud-queue-size" "${VISIONBRIDGE_CLOUD_QUEUE_SIZE:-128}"
    "--cloud-max-snapshot-bytes" "${VISIONBRIDGE_MAX_SNAPSHOT_BYTES:-4194304}"
    "--gateway-id" "${VISIONBRIDGE_GATEWAY_ID:-visionbridge-gateway-01}"
    "--device-id" "${VISIONBRIDGE_DEVICE_ID:-visionbridge-edge-01}"
    "--camera" "${USB_CAMERA_DEVICE:-}"
    "--snapshots-dir" "${SNAPSHOTS_DIR:-${SCRIPT_DIR}/snapshots}"
    "--point-name" "${POINT_NAME:-blindway-point-01}"
    "--device-source" "${DEVICE_SOURCE:-usb_camera+lc76g}"
    "--gps-coord-system" "${GPS_COORD_SYSTEM:-gcj02}"
    "--heartbeat-interval" "${HEARTBEAT_INTERVAL:-20}"
    "--event-cooldown" "${EVENT_COOLDOWN_SEC:-${EVENT_COOLDOWN:-20}}"
    "--confirm-window-frames" "${CONFIRM_WINDOW_FRAMES:-8}"
    "--confirm-required-hits" "${CONFIRM_REQUIRED_HITS:-5}"
    "--confirm-min-duration-sec" "${CONFIRM_MIN_DURATION_SEC:-2}"
    "--clear-miss-frames" "${CLEAR_MISS_FRAMES:-5}"
    "--clear-duration-sec" "${CLEAR_DURATION_SEC:-3}"
    "--evidence-interval-sec" "${EVIDENCE_INTERVAL_SEC:-30}"
    "--spatial-dedup-meters" "${SPATIAL_DEDUP_METERS:-15}"
    "--infer-every-n-frames" "${INFER_EVERY_N_FRAMES:-2}"
    "--infer-interval-sec" "${INFER_INTERVAL_SEC:-1.5}"
    "--input-size" "${INPUT_SIZE:-320}"
    "--camera-width" "${CAMERA_WIDTH:-320}"
    "--camera-height" "${CAMERA_HEIGHT:-240}"
    "--camera-target-fps" "${CAMERA_TARGET_FPS:-20}"
    "--camera-use-mjpg" "${CAMERA_USE_MJPG:-1}"
    "--camera-buffer-size" "${CAMERA_BUFFER_SIZE:-1}"
    "--conf-threshold" "${CONF_THRESHOLD:-0.35}"
    "--iou-threshold" "${IOU_THRESHOLD:-0.45}"
)

if [[ -n "${MODEL_VERSION:-}" ]]; then
    ARGS+=("--model-version" "${MODEL_VERSION}")
fi

if [[ -n "${GPS_SAMPLE_INTERVAL:-}" ]]; then
    ARGS+=("--gps-sample-interval" "${GPS_SAMPLE_INTERVAL}")
fi

if [[ -n "${GPS_HISTORY_MAX_POINTS:-}" ]]; then
    ARGS+=("--gps-history-max-points" "${GPS_HISTORY_MAX_POINTS}")
fi

if [[ -n "${EVENT_HISTORY_MAX_POINTS:-}" ]]; then
    ARGS+=("--event-history-max-points" "${EVENT_HISTORY_MAX_POINTS}")
fi

if [[ "${WEB_PREVIEW_ENABLE:-0}" == "1" || "${WEB_PREVIEW:-0}" == "1" ]]; then
    ARGS+=(
        "--web-preview"
        "--web-preview-host" "${WEB_PREVIEW_HOST:-0.0.0.0}"
        "--web-preview-port" "${WEB_PREVIEW_PORT:-8090}"
        "--web-preview-quality" "${WEB_PREVIEW_QUALITY:-50}"
        "--web-preview-fps" "${WEB_PREVIEW_FPS:-12}"
        "--web-preview-jpeg-fps" "${WEB_PREVIEW_JPEG_FPS:-5}"
        "--web-preview-max-width" "${WEB_PREVIEW_MAX_WIDTH:-480}"
    )
fi

exec python3 "${SCRIPT_DIR}/visionbridge_edge_agent.py" "${ARGS[@]}"
