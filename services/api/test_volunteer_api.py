from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="visionbridge-volunteer-test-"))
os.environ["VISIONBRIDGE_DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["VISIONBRIDGE_EMAIL_DEBUG"] = "1"
os.environ["VISIONBRIDGE_AUTH_SECRET"] = "test-only-auth-secret"
os.environ["VISIONBRIDGE_MEDIA_PUBLISH_SECRET"] = "test-only-media-secret"
os.environ["VISIONBRIDGE_INGEST_TOKEN"] = "test-only-ingest-token"

from fastapi.testclient import TestClient

from services.api.app import app


class VolunteerApiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

    def login(self, email: str, display_name: str) -> str:
        requested = self.client.post("/api/v1/auth/email/request", json={"email": email})
        self.assertEqual(requested.status_code, 200, requested.text)
        code = requested.json()["debugCode"]
        verified = self.client.post(
            "/api/v1/auth/email/verify",
            json={"email": email, "code": code, "displayName": display_name},
        )
        self.assertEqual(verified.status_code, 200, verified.text)
        return verified.json()["token"]

    def test_edge_event_is_cleaned_deduplicated_and_cleared(self) -> None:
        event_id = "edge-state-test-1"
        payload = {
            "ts": "2026-08-10T17:00:00+08:00",
            "device": {"device_id": "edge-test-01", "point_name": "test-point"},
            "runtime": {
                "last_event_id": event_id,
                "last_obstacle_type": "construction_obstacle",
                "last_confidence": 0.89,
                "triggerStreak": 5,
                "camera_fps": 8.0,
                "inference_ms": 320,
            },
            "gps": {"lat": 28.629306, "lng": 121.434412, "hdop": 1.2},
            "iot_properties": {"eventActive": 1, "alertLevelCode": 2},
        }
        headers = {"Authorization": "Bearer test-only-ingest-token"}
        first = self.client.post("/api/v1/telemetry", json=payload, headers=headers)
        self.assertEqual(first.status_code, 202, first.text)
        repeated = self.client.post("/api/v1/telemetry", json=payload, headers=headers)
        self.assertEqual(repeated.status_code, 202, repeated.text)
        active = self.client.get("/api/v1/events?status=active").json()["items"]
        matched = [item for item in active if item["analysisSummary"].startswith("test-point")]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["analysisStatus"], "local_validated")
        clear_payload = dict(payload)
        clear_payload["runtime"] = {**payload["runtime"], "event_state": "cleared"}
        clear_payload["iot_properties"] = {"eventActive": 0}
        cleared = self.client.post("/api/v1/telemetry", json=clear_payload, headers=headers)
        self.assertEqual(cleared.status_code, 202, cleared.text)
        closed = self.client.get("/api/v1/events?status=cleared").json()["items"]
        self.assertTrue(any(item["id"] == matched[0]["id"] for item in closed))

    def test_single_character_nickname_and_friendly_validation(self) -> None:
        token = self.login("short-name@example.com", "李")
        profile = self.client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["user"]["displayName"], "李")

        invalid = self.client.post(
            "/api/v1/auth/email/verify",
            json={"email": "short-name@example.com", "code": "12"},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["detail"], "请输入邮件中的 6 位验证码")

    def test_overview_and_analysis_status_never_inject_demo_data(self) -> None:
        overview = self.client.get("/api/v1/overview")
        self.assertEqual(overview.status_code, 200, overview.text)
        payload = overview.json()
        self.assertIn(payload["dataMode"], {"empty", "live"})
        self.assertNotIn("demo", payload["dataMode"])
        self.assertGreaterEqual(payload["kpis"]["totalDevices"], 0)
        self.assertEqual(payload["device"] is None, payload["kpis"]["totalDevices"] == 0)
        self.assertEqual(len(payload["trends"]["events"]), len(payload["trends"]["labels"]))
        analysis = self.client.get("/api/v1/admin/analysis/status")
        self.assertEqual(analysis.status_code, 200, analysis.text)
        self.assertIn("provider", analysis.json())

    def test_media_auth_is_readable_but_publish_is_device_scoped(self) -> None:
        readable = self.client.post(
            "/api/v1/media/auth",
            json={"action": "read", "path": "devices/uno-cloud-gateway-01", "protocol": "webrtc"},
        )
        self.assertEqual(readable.status_code, 200, readable.text)
        trailing_slash = self.client.post(
            "/api/v1/media/auth",
            json={"action": "read", "path": "/devices/uno-cloud-gateway-01/", "protocol": "hls"},
        )
        self.assertEqual(trailing_slash.status_code, 200, trailing_slash.text)

        publish = self.client.post(
            "/api/v1/media/auth",
            json={
                "user": "uno-cloud-gateway-01",
                "password": "test-only-media-secret",
                "action": "publish",
                "path": "devices/uno-cloud-gateway-01",
                "protocol": "rtsp",
            },
        )
        self.assertEqual(publish.status_code, 200, publish.text)

        wrong_path = self.client.post(
            "/api/v1/media/auth",
            json={
                "user": "uno-cloud-gateway-01",
                "password": "test-only-media-secret",
                "action": "publish",
                "path": "devices/other-device",
                "protocol": "rtsp",
            },
        )
        self.assertEqual(wrong_path.status_code, 401, wrong_path.text)

    def test_complete_report_review_assignment_flow(self) -> None:
        reporter_token = self.login("reporter@example.com", "上报志愿者")
        helper_token = self.login("helper@example.com", "处置志愿者")
        second_helper_token = self.login("second@example.com", "第二志愿者")

        created = self.client.post(
            "/api/v1/volunteer/reports",
            headers={"Authorization": f"Bearer {reporter_token}"},
            data={
                "category": "road_damage",
                "cleanupReason": "unsafe_to_clear",
                "description": "盲道路面有明显坑洼，雨天存在积水风险。",
                "address": "学院路东侧盲道",
                "lat": "28.632112",
                "lng": "121.138923",
            },
            files={"photo": ("damage.jpg", b"fake-jpeg-report", "image/jpeg")},
        )
        self.assertEqual(created.status_code, 201, created.text)
        report = created.json()["report"]
        self.assertEqual(report["status"], "pending")

        pending = self.client.get("/api/v1/admin/reports?status=pending")
        self.assertEqual(pending.status_code, 200)
        self.assertEqual(pending.json()["count"], 1)

        reviewed = self.client.patch(
            f"/api/v1/admin/reports/{report['id']}",
            json={"action": "approve", "note": "位置清楚，发布市政协助任务", "publishTask": True, "priority": "urgent"},
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        obstacle = reviewed.json()["obstacle"]
        task = reviewed.json()["task"]
        self.assertEqual(obstacle["status"], "open")
        self.assertEqual(task["status"], "open")

        map_response = self.client.get("/api/v1/map/obstacles")
        self.assertEqual(map_response.status_code, 200)
        self.assertEqual(map_response.json()["items"][0]["id"], obstacle["id"])
        self.assertEqual(map_response.json()["items"][0]["taskId"], task["id"])
        self.assertEqual(map_response.json()["items"][0]["taskStatus"], "open")
        active_overview = self.client.get("/api/v1/overview").json()
        overview_event = next(
            item for item in active_overview["recentEvents"]
            if item["id"] == obstacle["eventId"]
        )
        self.assertEqual(overview_event["statusLabel"], "未接单")

        tasks = self.client.get(
            "/api/v1/volunteer/tasks",
            headers={"Authorization": f"Bearer {helper_token}"},
        )
        self.assertEqual(tasks.status_code, 200)
        self.assertEqual(tasks.json()["items"][0]["id"], task["id"])

        claimed = self.client.post(
            f"/api/v1/volunteer/tasks/{task['id']}/claim",
            headers={"Authorization": f"Bearer {helper_token}"},
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(claimed.json()["task"]["status"], "claimed")
        claimed_map = self.client.get("/api/v1/map/obstacles").json()["items"]
        claimed_obstacle = next(item for item in claimed_map if item["id"] == obstacle["id"])
        self.assertEqual(claimed_obstacle["taskStatus"], "claimed")
        summary = self.client.get("/api/v1/admin/operations/summary")
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertTrue(summary.json()["consistent"], summary.text)
        self.assertEqual(summary.json()["tasks"]["claimed"], 1)

        conflict = self.client.post(
            f"/api/v1/volunteer/tasks/{task['id']}/claim",
            headers={"Authorization": f"Bearer {second_helper_token}"},
        )
        self.assertEqual(conflict.status_code, 409)

        submitted = self.client.post(
            f"/api/v1/volunteer/tasks/{task['id']}/complete",
            headers={"Authorization": f"Bearer {helper_token}"},
            data={"note": "已设置醒目围挡并上报市政维修。"},
            files={"photo": ("completion.jpg", b"fake-jpeg-completion", "image/jpeg")},
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["task"]["status"], "submitted")

        verified = self.client.patch(
            f"/api/v1/admin/tasks/{task['id']}",
            json={"action": "verify", "note": "处置凭证有效"},
        )
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(verified.json()["task"]["status"], "verified")

        all_obstacles = self.client.get("/api/v1/map/obstacles?includeResolved=true")
        resolved = next(item for item in all_obstacles.json()["items"] if item["id"] == obstacle["id"])
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["taskStatus"], "verified")
        active_obstacles = self.client.get("/api/v1/map/obstacles").json()["items"]
        self.assertFalse(any(item["id"] == obstacle["id"] for item in active_obstacles))
        active_events = self.client.get("/api/v1/events").json()["items"]
        self.assertFalse(any(item["id"] == obstacle["eventId"] for item in active_events))
        self.assertFalse(any(item["id"] == obstacle["eventId"] for item in self.client.get("/api/v1/overview").json()["recentEvents"]))
        cleared_events = self.client.get("/api/v1/events?status=cleared")
        event = next(item for item in cleared_events.json()["items"] if item["id"] == obstacle["eventId"])
        self.assertEqual(event["status"], "cleared")
        final_summary = self.client.get("/api/v1/admin/operations/summary")
        self.assertTrue(final_summary.json()["consistent"], final_summary.text)
        self.assertEqual(final_summary.json()["tasks"]["verified"], 1)

    def test_report_owner_can_delete_only_unapproved_reports(self) -> None:
        owner_token = self.login("delete-owner@example.com", "删除测试")
        other_token = self.login("delete-other@example.com", "其他志愿者")

        def create_report(description: str):
            response = self.client.post(
                "/api/v1/volunteer/reports",
                headers={"Authorization": f"Bearer {owner_token}"},
                data={
                    "category": "temporary_obstacle",
                    "cleanupReason": "unable_now",
                    "description": description,
                    "address": "测试道路盲道",
                    "lat": "28.632112",
                    "lng": "121.138923",
                },
                files={"photo": ("report.jpg", b"deletable-image", "image/jpeg")},
            )
            self.assertEqual(response.status_code, 201, response.text)
            return response.json()["report"]

        report = create_report("这是一条可以由上报者撤回的待审核记录。")
        self.assertTrue(report["canDelete"])
        forbidden = self.client.delete(
            f"/api/v1/volunteer/reports/{report['id']}",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        self.assertEqual(forbidden.status_code, 404)
        deleted = self.client.delete(
            f"/api/v1/volunteer/reports/{report['id']}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        mine = self.client.get(
            "/api/v1/volunteer/reports/mine",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertFalse(any(item["id"] == report["id"] for item in mine.json()["items"]))

        approved_report = create_report("这条记录审核通过后应成为公共数据，个人不能删除。")
        approved = self.client.patch(
            f"/api/v1/admin/reports/{approved_report['id']}",
            json={"action": "approve", "publishTask": False, "priority": "normal"},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        blocked = self.client.delete(
            f"/api/v1/volunteer/reports/{approved_report['id']}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)


if __name__ == "__main__":
    unittest.main()
