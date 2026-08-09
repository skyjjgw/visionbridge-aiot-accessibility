"""Deploy a tested VisionBridge release with server-side rollback.

Usage:
  set VISIONBRIDGE_SSH_PASSWORD=...
  python deploy_release.py visionbridge-release-YYYYMMDD-HHMMSS.tar.gz

The password is read only from the process environment and is never written
to the project or sent in command output.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

import paramiko


HOST = os.environ.get("VISIONBRIDGE_CLOUD_HOST", "").strip()
SSH_USER = os.environ.get("VISIONBRIDGE_CLOUD_USER", "root").strip()
SSH_PORT = int(os.environ.get("VISIONBRIDGE_CLOUD_SSH_PORT", "22"))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: deploy_release.py <release.tar.gz>")
    archive = Path(sys.argv[1]).resolve()
    match = re.fullmatch(r"visionbridge-release-(\d{8}-\d{6})\.tar\.gz", archive.name)
    if not archive.is_file() or match is None:
        raise SystemExit("invalid release archive")
    stamp = match.group(1)
    password = os.environ.get("VISIONBRIDGE_SSH_PASSWORD", "")
    if not HOST:
        raise SystemExit("VISIONBRIDGE_CLOUD_HOST is not set")
    if not password:
        raise SystemExit("VISIONBRIDGE_SSH_PASSWORD is not set")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    remote_archive = f"/tmp/{archive.name}"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=SSH_PORT, username=SSH_USER, password=password, timeout=15)
    sftp = client.open_sftp()
    sftp.put(str(archive), remote_archive)
    sftp.close()

    script = f"""set -Eeuo pipefail
STAMP={stamp}
ARCHIVE={remote_archive}
RELEASE=/opt/visionbridge/releases/$STAMP
NGINX=/etc/nginx/sites-available/visionbridge
BACKUP=/opt/visionbridge/backups/$STAMP
SWAPPED=0

rollback() {{
  code=$?
  set +e
  echo "rollback-start code=$code"
  if [ "$SWAPPED" = 1 ]; then
    systemctl stop visionbridge-api
    for name in server static-deploy volunteer; do
      current="/opt/visionbridge/$name"
      previous="/opt/visionbridge/$name.prev-$STAMP"
      if [ -e "$previous" ]; then
        [ -e "$current" ] && mv "$current" "/opt/visionbridge/$name.failed-$STAMP"
        mv "$previous" "$current"
      fi
    done
    [ -f "$NGINX.bak-$STAMP" ] && cp -a "$NGINX.bak-$STAMP" "$NGINX"
    systemctl start visionbridge-api
    nginx -t && systemctl reload nginx
  fi
  echo rollback-finished
  exit "$code"
}}
trap rollback ERR

echo "{digest}  $ARCHIVE" | sha256sum -c -
[ ! -e "$RELEASE" ]
mkdir -p "$RELEASE" "$BACKUP"
tar -xzf "$ARCHIVE" -C "$RELEASE"
python3 -m py_compile "$RELEASE/services/api/app.py"
test -s "$RELEASE/apps/dashboard/static-deploy/index.html"
test -s "$RELEASE/apps/volunteer/build/web/main.dart.js"
test -s "$RELEASE/deploy/nginx/visionbridge.conf"

cp -a /opt/visionbridge/data/visionbridge.db "$BACKUP/visionbridge.db"
[ -f /opt/visionbridge/data/visionbridge.db-wal ] && cp -a /opt/visionbridge/data/visionbridge.db-wal "$BACKUP/visionbridge.db-wal" || true
[ -f /opt/visionbridge/data/visionbridge.db-shm ] && cp -a /opt/visionbridge/data/visionbridge.db-shm "$BACKUP/visionbridge.db-shm" || true
cp -a "$NGINX" "$NGINX.bak-$STAMP"
cp -a /etc/systemd/system/visionbridge-api.service "$BACKUP/visionbridge-api.service"

cp -a "$RELEASE/services/api" "/opt/visionbridge/server.next-$STAMP"
cp -a "$RELEASE/apps/dashboard/static-deploy" "/opt/visionbridge/static-deploy.next-$STAMP"
cp -a "$RELEASE/apps/volunteer/build/web" "/opt/visionbridge/volunteer.next-$STAMP"
chown -R visionbridge:visionbridge "/opt/visionbridge/server.next-$STAMP"
chmod -R a+rX,u+w "/opt/visionbridge/static-deploy.next-$STAMP" "/opt/visionbridge/volunteer.next-$STAMP"

systemctl stop visionbridge-api
mv /opt/visionbridge/server "/opt/visionbridge/server.prev-$STAMP"
mv /opt/visionbridge/static-deploy "/opt/visionbridge/static-deploy.prev-$STAMP"
mv /opt/visionbridge/volunteer "/opt/visionbridge/volunteer.prev-$STAMP"
mv "/opt/visionbridge/server.next-$STAMP" /opt/visionbridge/server
mv "/opt/visionbridge/static-deploy.next-$STAMP" /opt/visionbridge/static-deploy
mv "/opt/visionbridge/volunteer.next-$STAMP" /opt/visionbridge/volunteer
cp -a "$RELEASE/deploy/nginx/visionbridge.conf" "$NGINX"
SWAPPED=1

nginx -t
systemctl start visionbridge-api
systemctl reload nginx
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  curl -fsS http://127.0.0.1:8000/api/v1/health >/tmp/visionbridge-health-$STAMP.json && break
  sleep 1
done
curl -fsS http://127.0.0.1:8000/api/v1/health
printf '\n'
curl -fsS http://127.0.0.1:8000/api/v1/admin/operations/summary
printf '\n'
code=$(curl -sS -o /tmp/visionbridge-admin-$STAMP.json -w '%{{http_code}}' 'http://127.0.0.1:8088/api/v1/admin/reports?status=pending')
[ "$code" = 401 ]
echo "admin-without-login-http=$code"
code=$(curl -sS -o /tmp/visionbridge-volunteer-$STAMP.html -w '%{{http_code}}' http://127.0.0.1:8088/volunteer/)
[ "$code" = 200 ]
echo "volunteer-http=$code"
systemctl is-active visionbridge-api nginx
sha256sum /opt/visionbridge/volunteer/main.dart.js
trap - ERR
echo "deployment-ok release=$STAMP"
"""

    stdin, stdout, stderr = client.exec_command("bash -s", timeout=180)
    stdin.write(script)
    stdin.channel.shutdown_write()
    output = stdout.read().decode("utf-8", "replace")
    error = stderr.read().decode("utf-8", "replace")
    exit_code = stdout.channel.recv_exit_status()
    client.close()
    print(output)
    if error:
        print(error, file=sys.stderr)
    if exit_code:
        raise SystemExit(f"deployment failed with exit code {exit_code}")


if __name__ == "__main__":
    main()
