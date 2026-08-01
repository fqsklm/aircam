import csv
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

import aircam


def config(root: str) -> dict:
    return {
        "camera": {
            "device": str(Path(root) / "video0"),
            "input_format": "mjpeg",
            "controls": {},
        },
        "capture": {
            "width": 1920,
            "height": 1080,
            "source_fps": 30,
            "interval_seconds": 1,
            "jpeg_quality": 2,
            "auto_restart": True,
            "max_restarts": 3,
            "restart_delay_seconds": 0.1,
        },
        "storage": {
            "data_dir": str(Path(root) / "photos"),
            "min_free_mb": 16,
        },
        "server": {"host": "127.0.0.1", "port": 8080, "token": "test"},
        "gpio": {"enabled": False},
    }


class AirCamTests(unittest.TestCase):
    def test_load_config_rejects_missing_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"camera": {}}), encoding="utf-8")
            with self.assertRaises(aircam.AirCamError):
                aircam.load_config(path)

    def test_ffmpeg_command_contains_capture_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = aircam.CameraService(config(tmp))
            command = service.build_ffmpeg_command(
                Path(tmp) / "photo_%08d.jpg", 0.5
            )
            self.assertIn("1920x1080", command)
            self.assertIn("mjpeg", command)
            self.assertIn("-copyts", command)
            self.assertEqual(command[command.index("-timestamps") + 1], "abs")
            video_filter = command[command.index("-vf") + 1]
            self.assertIn("select=", video_filter)
            self.assertIn("/0.5", video_filter)
            self.assertIn("showinfo", video_filter)
            self.assertEqual(command[command.index("-fps_mode") + 1], "vfr")
            self.assertEqual(command[command.index("-atomic_writing") + 1], "1")
            self.assertEqual(command[command.index("-start_number") + 1], "1")
            self.assertEqual(command[-1], str(Path(tmp) / "photo_%08d.jpg"))

    def test_set_controls_uses_one_safe_argument_per_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Mock()
            runner.return_value.returncode = 0
            runner.return_value.stdout = ""
            runner.return_value.stderr = ""
            service = aircam.CameraService(config(tmp), run=runner)
            result = service.set_controls(
                {"exposure_auto": 1, "exposure_absolute": 120}
            )
            self.assertTrue(all(item["ok"] for item in result))
            calls = [call.args[0] for call in runner.call_args_list]
            self.assertEqual(
                calls[0],
                [
                    "v4l2-ctl",
                    "-d",
                    str(Path(tmp) / "video0"),
                    "-c",
                    "exposure_auto=1",
                ],
            )

    def test_set_controls_rejects_non_integer(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = aircam.CameraService(config(tmp))
            with self.assertRaises(aircam.AirCamError):
                service.set_controls({"exposure_absolute": "100"})

    def test_number_bounds(self):
        self.assertEqual(aircam.check_number(0.05, "x", 0.05, 10), 0.05)
        with self.assertRaises(aircam.AirCamError):
            aircam.check_number(0, "x", 0.05, 10)
        with self.assertRaises(aircam.AirCamError):
            aircam.check_number(True, "x", 0.05, 10)

    def test_validate_config_rejects_bad_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = config(tmp)
            value["server"]["port"] = 70000
            with self.assertRaises(aircam.AirCamError):
                aircam.validate_config(value)

    def test_validate_config_uses_30_fps_interval_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = config(tmp)
            value["capture"]["interval_seconds"] = 0.0334
            aircam.validate_config(value)
            value["capture"]["interval_seconds"] = 0.0333
            with self.assertRaises(aircam.AirCamError):
                aircam.validate_config(value)

    def test_manifest_records_each_photo_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = aircam.CameraService(config(tmp))
            session = Path(tmp) / "photos" / "session"
            session.mkdir()
            manifest = session / "manifest.csv"
            manifest.write_text(
                ",".join(service._manifest_header()) + "\n", encoding="utf-8"
            )
            service.active_controls = {"exposure_absolute": 100}
            service.control_history = [
                (0.0, dict(service.active_controls))
            ]
            (session / "photo_00000001.jpg").write_bytes(b"jpeg")
            service._record_new_photos(
                session,
                1.0,
                include_recent=True,
            )
            service._record_new_photos(
                session,
                1.0,
                include_recent=True,
            )
            lines = manifest.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("photo_00000001.jpg", lines[1])
            self.assertIn("exposure_absolute", lines[1])

    def test_latest_photo_ignores_zero_byte_stop_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = aircam.CameraService(config(tmp))
            session = Path(tmp) / "photos" / "session"
            session.mkdir()
            (session / "photo_00000001.jpg").write_bytes(b"valid-jpeg")
            (session / "photo_00000002.jpg").write_bytes(b"")
            service.status.session = "session"

            latest = service.latest_photo()

            self.assertIsNotNone(latest)
            self.assertEqual(latest.name, "photo_00000001.jpg")

            # The latest stored photo remains available after a service
            # restart, before a new session has started.
            service.status.session = None
            latest_after_restart = service.latest_photo()
            self.assertIsNotNone(latest_after_restart)
            self.assertEqual(
                latest_after_restart.name, "photo_00000001.jpg"
            )

    def test_next_photo_number_never_overwrites_after_a_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "photo_00000001.jpg").touch()
            (session / "photo_00000007.jpg").touch()
            (session / "photo_20240309T160000.123456Z_00000009.jpg").touch()
            (session / "pending_00000011.jpg").touch()
            self.assertEqual(aircam.CameraService._next_photo_number(session), 12)

    def test_pending_photo_uses_exact_v4l2_timestamp_in_name_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = aircam.CameraService(config(tmp))
            session = Path(tmp) / "photos" / "session"
            session.mkdir()
            manifest = session / "manifest.csv"
            with manifest.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(service._manifest_header())

            pending = session / "pending_00000001.jpg"
            pending.write_bytes(b"jpeg")
            service.frame_timestamps_us[pending.name] = 1_710_000_000_123_456
            service.control_history = [
                (1_709_999_999.0, {"exposure_absolute": 80})
            ]

            service._record_new_photos(session, 0.5, include_recent=True)

            expected = (
                session / "photo_20240309T160000.123456Z_00000001.jpg"
            )
            self.assertTrue(expected.exists())
            self.assertFalse(pending.exists())
            with manifest.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["filename"], expected.name)
            self.assertEqual(row["capture_unix_us"], "1710000000123456")
            self.assertEqual(row["capture_time_source"], "v4l2_pts_abs")
            self.assertIn("exposure_absolute", row["camera_controls_json"])

    def test_pts_timebase_conversion_preserves_microseconds(self):
        self.assertEqual(
            aircam.CameraService._pts_to_unix_us(
                1_710_000_000_123_456, 1, 1_000_000
            ),
            1_710_000_000_123_456,
        )

    def test_manifest_uses_controls_active_at_photo_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = aircam.CameraService(config(tmp))
            session = Path(tmp) / "photos" / "session"
            session.mkdir()
            manifest = session / "manifest.csv"
            manifest.write_text(
                ",".join(service._manifest_header()) + "\n", encoding="utf-8"
            )
            early = session / "photo_00000001.jpg"
            late = session / "photo_00000002.jpg"
            early.write_bytes(b"a")
            late.write_bytes(b"b")
            early_time = time.time() - 20
            late_time = time.time() - 10
            import os

            os.utime(early, (early_time, early_time))
            os.utime(late, (late_time, late_time))
            service.control_history = [
                (early_time - 1, {"exposure_absolute": 80}),
                (late_time - 1, {"exposure_absolute": 160}),
            ]
            service._record_new_photos(session, 1.0, include_recent=True)
            text = manifest.read_text(encoding="utf-8")
            first, second = text.splitlines()[1:]
            self.assertIn('exposure_absolute"":80', first)
            self.assertIn('exposure_absolute"":160', second)

    def test_control_change_writes_session_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Mock()
            runner.return_value.returncode = 0
            runner.return_value.stdout = ""
            runner.return_value.stderr = ""
            service = aircam.CameraService(config(tmp), run=runner)
            session = Path(tmp) / "photos" / "session"
            session.mkdir()
            service.session_dir = session
            service.status.state = "capturing"
            service.set_controls({"exposure_absolute": 123})
            events = (session / "control-events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(events), 1)
            event = json.loads(events[0])
            self.assertEqual(event["controls"]["exposure_absolute"], 123)
            self.assertEqual(
                service.get_status()["active_controls"]["exposure_absolute"],
                123,
            )

    def test_low_disk_space_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = config(tmp)
            Path(value["camera"]["device"]).touch()
            service = aircam.CameraService(value)
            with patch(
                "aircam.shutil.disk_usage",
                return_value=SimpleNamespace(
                    total=100 * 1024 * 1024,
                    used=99 * 1024 * 1024,
                    free=1 * 1024 * 1024,
                ),
            ):
                with self.assertRaises(aircam.AirCamError) as context:
                    service.start()
            self.assertIn("磁盘剩余空间不足", str(context.exception))

    def test_unexpected_exit_recovers_capture(self):
        class FakeProcess:
            def __init__(self, pid, return_code):
                self.pid = pid
                self.return_code = return_code

            def poll(self):
                return self.return_code

            def terminate(self):
                self.return_code = 0

            def kill(self):
                self.return_code = -9

            def wait(self, timeout=None):
                return self.return_code

        with tempfile.TemporaryDirectory() as tmp:
            value = config(tmp)
            Path(value["camera"]["device"]).touch()
            service = aircam.CameraService(value)
            failed = FakeProcess(101, 1)
            replacement = FakeProcess(102, None)
            service._spawn_ffmpeg = Mock(side_effect=[failed, replacement])
            service._signal_process = lambda process: setattr(
                process, "return_code", 0
            )

            service.start()
            deadline = time.time() + 2
            while time.time() < deadline:
                status = service.get_status()
                if status["pid"] == 102:
                    break
                time.sleep(0.02)
            self.assertEqual(service.get_status()["state"], "capturing")
            self.assertEqual(service.get_status()["pid"], 102)
            self.assertEqual(service.get_status()["restart_count"], 1)
            service.stop()
            self.assertEqual(service.get_status()["state"], "idle")

    def test_http_api_requires_token_and_returns_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = config(tmp)
            camera = Mock()
            camera.get_status.return_value = {
                "state": "idle",
                "photo_count": 0,
                "disk": {"free_bytes": 1, "total_bytes": 2},
            }
            camera.start.return_value = camera.get_status.return_value
            camera.stop.return_value = camera.get_status.return_value
            index = Path(tmp) / "index.html"
            index.write_text("<p>AirCam</p>", encoding="utf-8")
            server = aircam.AirCamHttpServer(
                ("127.0.0.1", 0), camera, value, index
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(base + "/api/status", timeout=2)
                self.assertEqual(context.exception.code, 401)

                request = urllib.request.Request(
                    base + "/api/status",
                    headers={"X-AirCam-Token": "test"},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    body = json.loads(response.read())
                self.assertTrue(body["ok"])
                self.assertEqual(body["status"]["state"], "idle")

                start_request = urllib.request.Request(
                    base + "/api/start",
                    method="POST",
                    data=json.dumps({"interval_seconds": 0.5}).encode(),
                    headers={
                        "X-AirCam-Token": "test",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(start_request, timeout=2) as response:
                    self.assertTrue(json.loads(response.read())["ok"])
                camera.start.assert_called_once_with(0.5)

                stop_request = urllib.request.Request(
                    base + "/api/stop",
                    method="POST",
                    data=b"{}",
                    headers={
                        "X-AirCam-Token": "test",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(stop_request, timeout=2) as response:
                    self.assertTrue(json.loads(response.read())["ok"])
                camera.stop.assert_called_once_with()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_systemd_unit_enables_boot_and_failure_restart(self):
        unit = (
            Path(__file__).resolve().parents[1] / "systemd" / "aircam.service"
        ).read_text(encoding="utf-8")
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertIn("ExecStart=/usr/bin/python3 /home/pi/AirCam/aircam.py", unit)


if __name__ == "__main__":
    unittest.main()
