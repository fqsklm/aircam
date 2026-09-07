import csv
import io
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
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
        "pwm": {"enabled": False},
        "mavlink": {"enabled": False},
    }


def fake_v4l2_runner(definitions, fail_on=None, clamp=None):
    state = {name: int(item["value"]) for name, item in definitions.items()}

    def render():
        lines = []
        for index, (name, item) in enumerate(definitions.items(), 1):
            control_type = item.get("type", "int")
            details = (
                f"min={item.get('min', 0)} max={item.get('max', 255)} "
                f"step={item.get('step', 1)} default={item.get('default', state[name])} "
                f"value={state[name]}"
            )
            dynamically_inactive = (
                item.get("inactive_when_auto")
                and state.get("auto_exposure") != 1
            )
            if item.get("inactive") or dynamically_inactive:
                details += " flags=inactive"
            lines.append(f"{name} 0x009a{index:04x} ({control_type}) : {details}")
            for value, label in item.get("menu", {}).items():
                lines.append(f"                {value}: {label}")
        return "\n".join(lines) + "\n"

    def execute(command, **_kwargs):
        if "--list-ctrls-menus" in command:
            return SimpleNamespace(returncode=0, stdout=render(), stderr="")
        setting = command[command.index("-c") + 1]
        name, raw_value = setting.split("=", 1)
        if name == fail_on:
            return SimpleNamespace(returncode=1, stdout="", stderr="driver rejected value")
        if (
            definitions[name].get("inactive_when_auto")
            and state.get("auto_exposure") != 1
        ):
            return SimpleNamespace(returncode=1, stdout="", stderr="control is inactive")
        value = int(raw_value)
        state[name] = int(clamp[name](value)) if clamp and name in clamp else value
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return Mock(side_effect=execute), state


def heartbeat_payload(autopilot=3):
    return struct.pack("<IBBBBB", 0, 2, autopilot, 0, 4, 3)


class AirCamTests(unittest.TestCase):
    def test_http_server_uses_dual_stack_for_ipv4_wildcard(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = config(tmp)
            camera = Mock()
            index = Path(tmp) / "index.html"
            index.write_text("<p>AirCam</p>", encoding="utf-8")

            with patch("aircam.socket.has_dualstack_ipv6", return_value=True):
                server = aircam.AirCamHttpServer(
                    ("0.0.0.0", 0), camera, value, index
                )
            try:
                self.assertEqual(server.address_family, socket.AF_INET6)
                self.assertEqual(server.server_address[0], "::")
                self.assertEqual(
                    server.socket.getsockopt(
                        socket.IPPROTO_IPV6,
                        socket.IPV6_V6ONLY,
                    ),
                    0,
                )
            finally:
                server.server_close()

    def test_chunked_writer_sends_an_explicit_final_chunk(self):
        output = io.BytesIO()
        writer = aircam.ChunkedWriter(output)

        self.assertEqual(writer.write(b"abc"), 3)
        self.assertEqual(writer.write(b""), 0)
        writer.finish()
        writer.finish()

        self.assertEqual(output.getvalue(), b"3\r\nabc\r\n0\r\n\r\n")
        with self.assertRaises(ValueError):
            writer.write(b"late")

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
            runner, state = fake_v4l2_runner(
                {
                    "auto_exposure": {
                        "type": "menu",
                        "min": 0,
                        "max": 3,
                        "value": 3,
                        "menu": {"1": "Manual Mode", "3": "Aperture Priority Mode"},
                    },
                    "exposure_time_absolute": {"min": 3, "max": 2047, "value": 100},
                }
            )
            service = aircam.CameraService(config(tmp), run=runner)
            result = service.set_controls(
                {"auto_exposure": 1, "exposure_time_absolute": 120}
            )
            self.assertTrue(all(item["ok"] for item in result))
            calls = [
                call.args[0]
                for call in runner.call_args_list
                if "-c" in call.args[0]
            ]
            self.assertEqual(
                calls[0],
                [
                    "v4l2-ctl",
                    "-d",
                    str(Path(tmp) / "video0"),
                    "-c",
                    "auto_exposure=1",
                ],
            )
            self.assertEqual(state["exposure_time_absolute"], 120)

    def test_set_controls_rejects_non_integer(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = aircam.CameraService(config(tmp))
            with self.assertRaises(aircam.AirCamError):
                service.set_controls({"exposure_absolute": "100"})

    def test_control_listing_parses_menu_ranges_and_inactive_flags(self):
        raw = """auto_exposure 0x009a0901 (menu) : min=0 max=3 default=3 value=1
                1: Manual Mode
                3: Aperture Priority Mode
exposure_time_absolute 0x009a0902 (int) : min=3 max=2047 step=1 default=250 value=120 flags=inactive
"""
        controls = aircam.CameraService.parse_control_listing(raw)

        self.assertEqual(controls["auto_exposure"]["menu"]["1"], "Manual Mode")
        self.assertEqual(controls["exposure_time_absolute"]["max"], 2047)
        self.assertIn("inactive", controls["exposure_time_absolute"]["flags"])

    def test_set_controls_rolls_back_earlier_values_when_later_value_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, state = fake_v4l2_runner(
                {
                    "auto_exposure": {
                        "type": "menu",
                        "min": 0,
                        "max": 3,
                        "value": 3,
                        "menu": {"1": "Manual Mode", "3": "Aperture Priority Mode"},
                    },
                    "exposure_time_absolute": {"min": 3, "max": 2047, "value": 100},
                },
                fail_on="exposure_time_absolute",
            )
            service = aircam.CameraService(config(tmp), run=runner)

            with self.assertRaisesRegex(aircam.AirCamError, "已回滚"):
                service.set_controls(
                    {"auto_exposure": 1, "exposure_time_absolute": 120}
                )

            self.assertEqual(state["auto_exposure"], 3)
            self.assertEqual(service.active_controls, {})

    def test_switching_to_auto_drops_stale_manual_exposure_before_reapply(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, state = fake_v4l2_runner(
                {
                    "auto_exposure": {
                        "type": "menu",
                        "min": 0,
                        "max": 3,
                        "value": 3,
                        "menu": {"1": "Manual Mode", "3": "Aperture Priority Mode"},
                    },
                    "exposure_time_absolute": {
                        "min": 3,
                        "max": 2047,
                        "value": 100,
                        "inactive_when_auto": True,
                    },
                }
            )
            service = aircam.CameraService(config(tmp), run=runner)

            service.set_controls(
                {"auto_exposure": 1, "exposure_time_absolute": 80}
            )
            service.set_controls({"auto_exposure": 3})

            self.assertEqual(state["auto_exposure"], 3)
            self.assertNotIn("exposure_time_absolute", service.active_controls)
            service.set_controls(dict(service.active_controls))

    def test_number_bounds(self):
        self.assertEqual(aircam.check_number(0.05, "x", 0.05, 10), 0.05)
        with self.assertRaises(aircam.AirCamError):
            aircam.check_number(0, "x", 0.05, 10)
        with self.assertRaises(aircam.AirCamError):
            aircam.check_number(True, "x", 0.05, 10)

    def test_duration_normalization(self):
        self.assertIsNone(aircam.normalize_duration_seconds(None))
        self.assertIsNone(aircam.normalize_duration_seconds(0))
        self.assertEqual(aircam.normalize_duration_seconds(1.5), 1.5)
        for invalid in (
            True,
            -1,
            aircam.MAX_CAPTURE_DURATION_SECONDS + 1,
        ):
            with self.assertRaises(aircam.AirCamError):
                aircam.normalize_duration_seconds(invalid)

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

    def test_validate_config_rejects_bad_mavlink_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = config(tmp)
            value["mavlink"] = {
                "enabled": True,
                "device": "/dev/serial0",
                "baud": 115200,
                "rc_channel": 9,
                "start_pwm": 1300,
                "stop_pwm": 1700,
            }
            with self.assertRaises(aircam.AirCamError):
                aircam.validate_config(value)

    def test_validate_config_rejects_bad_pwm_and_multiple_triggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = config(tmp)
            value["pwm"] = {
                "enabled": True,
                "bcm_pin": 17,
                "start_pwm": 1200,
                "stop_pwm": 1800,
            }
            with self.assertRaises(aircam.AirCamError):
                aircam.validate_config(value)

            value["pwm"] = {"enabled": True, "bcm_pin": 17}
            value["gpio"] = {"enabled": True}
            with self.assertRaises(aircam.AirCamError):
                aircam.validate_config(value)

    def test_pwm_trigger_measures_edges_and_reports_status(self):
        service = Mock()
        clock_ns = Mock(side_effect=[1_000_000_000, 1_001_900_000])
        clock = Mock(return_value=10.0)
        trigger = aircam.PwmTrigger(
            service,
            {"enabled": True, "bcm_pin": 17},
            device_factory=Mock(),
            clock=clock,
            clock_ns=clock_ns,
        )
        trigger.device = Mock()

        trigger._on_rising_edge()
        trigger._on_falling_edge()
        status = trigger.get_status(10.1)

        self.assertEqual(status["signal_state"], "ready")
        self.assertEqual(status["pwm"], 1900)
        self.assertEqual(status["pulse_count"], 1)
        self.assertEqual(status["switch_position"], "start")
        self.assertEqual(status["gpiochip"], 4)

    def test_pwm_trigger_debounces_repeated_pulses_and_times_out_safe(self):
        service = Mock()
        service.get_status.return_value = {"state": "idle"}
        trigger = aircam.PwmTrigger(
            service,
            {
                "enabled": True,
                "start_pwm": 1700,
                "stop_pwm": 1300,
                "debounce_seconds": 0.15,
                "min_stable_pulses": 2,
                "timeout_seconds": 0.5,
                "stop_on_timeout": True,
            },
            device_factory=Mock(),
        )
        trigger.device = Mock()

        trigger._record_pulse(1900, 10.0)
        trigger._evaluate(10.0)
        trigger._record_pulse(1900, 10.1)
        trigger._evaluate(10.2)
        service.start.assert_called_once_with()

        trigger._record_pulse(1100, 10.3)
        trigger._evaluate(10.3)
        trigger._record_pulse(1100, 10.4)
        trigger._evaluate(10.5)
        service.stop.assert_called_once_with()

        trigger._record_pulse(1900, 10.6)
        trigger._evaluate(10.6)
        trigger._record_pulse(1900, 10.7)
        trigger._evaluate(10.8)
        trigger._evaluate(11.3)
        self.assertEqual(service.start.call_count, 2)
        self.assertEqual(service.stop.call_count, 2)
        self.assertEqual(trigger.get_status(11.3)["signal_state"], "signal_timeout")

    def test_pwm_trigger_ignores_invalid_pulse(self):
        trigger = aircam.PwmTrigger(
            Mock(),
            {
                "enabled": True,
                "min_valid_pwm": 750,
                "max_valid_pwm": 2250,
            },
            device_factory=Mock(),
        )
        trigger._record_pulse(4000, 10.0)

        self.assertIsNone(trigger.last_pwm)
        self.assertEqual(trigger.invalid_pulse_count, 1)

    def test_pwm_gpiod_event_uses_supplied_kernel_tick(self):
        trigger = aircam.PwmTrigger(Mock(), {"enabled": True}, device_factory=Mock())
        trigger.device = Mock()

        trigger._on_kernel_edge(1, 8_000_000_000)
        trigger._on_kernel_edge(0, 8_001_750_000)

        self.assertEqual(trigger.last_pwm, 1750)

    @staticmethod
    def _rc_channels_payload(channel: int, pwm: int) -> bytes:
        payload = bytearray(42)
        struct.pack_into("<I", payload, 0, 1234)
        for index in range(18):
            struct.pack_into("<H", payload, 4 + index * 2, 1500)
        struct.pack_into("<H", payload, 4 + (channel - 1) * 2, pwm)
        payload[40] = 18
        payload[41] = 200
        return bytes(payload)

    @staticmethod
    def _mavlink2_rc_frame(payload: bytes, signed: bool = False) -> bytes:
        incompatibility_flags = 1 if signed else 0
        header = bytes(
            [
                len(payload),
                incompatibility_flags,
                0,
                7,
                1,
                1,
                aircam.MAVLINK_RC_CHANNELS_ID,
                0,
                0,
            ]
        )
        checksum = aircam.mavlink_x25_crc(
            header + payload,
            aircam.MAVLINK_RC_CHANNELS_CRC_EXTRA,
        )
        signature = bytes(13) if signed else b""
        return (
            bytes([aircam.MAVLINK_V2_MAGIC])
            + header
            + payload
            + struct.pack("<H", checksum)
            + signature
        )

    def test_mavlink_parser_accepts_fragmented_signed_rc_channels(self):
        payload = self._rc_channels_payload(9, 1900)
        frame = self._mavlink2_rc_frame(payload, signed=True)
        parser = aircam.MavlinkFrameParser()

        self.assertEqual(parser.feed(b"noise" + frame[:11]), [])
        messages = parser.feed(frame[11:])

        self.assertEqual(
            messages,
            [(1, 1, aircam.MAVLINK_RC_CHANNELS_ID, payload)],
        )

    def test_mavlink_parser_rejects_bad_checksum_and_recovers(self):
        payload = self._rc_channels_payload(9, 1900)
        damaged = bytearray(self._mavlink2_rc_frame(payload))
        damaged[15] ^= 0x01
        valid = self._mavlink2_rc_frame(payload)
        parser = aircam.MavlinkFrameParser()

        messages = parser.feed(bytes(damaged) + valid)

        self.assertEqual(
            messages,
            [(1, 1, aircam.MAVLINK_RC_CHANNELS_ID, payload)],
        )

    def test_mavlink2_truncated_zero_rssi_rc_channels_is_accepted(self):
        service = Mock()
        trigger = aircam.MavlinkTrigger(
            service,
            {"enabled": True, "rc_channel": 9, "debounce_seconds": 1},
            serial_factory=Mock(),
        )
        payload = self._rc_channels_payload(9, 1900)[:-1]

        trigger._handle_rc_channels(payload, 10.0, 1, 1)

        self.assertEqual(trigger.last_pwm, 1900)
        self.assertEqual(trigger.rc_message_count, 1)

    def test_mavlink_ignores_non_autopilot_heartbeat_and_foreign_rc(self):
        trigger = aircam.MavlinkTrigger(
            Mock(),
            {"enabled": True, "rc_channel": 9, "request_rate_hz": 10},
            serial_factory=Mock(),
        )
        trigger.serial_port = Mock()

        trigger._handle_heartbeat(42, 190, heartbeat_payload(autopilot=0), 10.0)
        trigger._handle_heartbeat(43, 42, heartbeat_payload(), 10.1)
        self.assertIsNone(trigger.target_system)
        trigger._handle_heartbeat(1, 1, heartbeat_payload(), 10.2)
        trigger._handle_rc_channels(self._rc_channels_payload(9, 1900), 10.3, 2, 1)

        self.assertEqual((trigger.target_system, trigger.target_component), (1, 1))
        self.assertIsNone(trigger.last_pwm)
        trigger.serial_port.write.assert_called_once()

    def test_mavlink_trigger_requests_rc_channels_after_heartbeat(self):
        service = Mock()
        trigger = aircam.MavlinkTrigger(
            service,
            {"enabled": True, "request_rate_hz": 10},
            serial_factory=Mock(),
        )
        trigger.serial_port = Mock()

        trigger._handle_heartbeat(1, 1, heartbeat_payload(), 20.0)

        frame = trigger.serial_port.write.call_args.args[0]
        self.assertEqual(frame[0], aircam.MAVLINK_V2_MAGIC)
        self.assertEqual(int.from_bytes(frame[7:10], "little"), 76)
        fields = struct.unpack_from("<7fHBBB", frame, 10)
        self.assertEqual(fields[0], float(aircam.MAVLINK_RC_CHANNELS_ID))
        self.assertEqual(fields[1], 100000.0)
        self.assertEqual(fields[7], aircam.MAV_CMD_SET_MESSAGE_INTERVAL)
        self.assertEqual(fields[8:10], (1, 1))

    def test_mavlink_trigger_debounces_start_stop_and_times_out_safe(self):
        service = Mock()
        service.get_status.return_value = {"state": "idle"}
        trigger = aircam.MavlinkTrigger(
            service,
            {
                "enabled": True,
                "rc_channel": 9,
                "start_pwm": 1700,
                "stop_pwm": 1300,
                "debounce_seconds": 0.15,
                "timeout_seconds": 2,
                "stop_on_timeout": True,
            },
            serial_factory=Mock(),
        )
        high = self._rc_channels_payload(9, 1900)
        low = self._rc_channels_payload(9, 1100)

        trigger._handle_rc_channels(high, 10.0)
        trigger._handle_rc_channels(high, 10.2)
        service.start.assert_called_once_with()
        trigger._handle_rc_channels(low, 10.3)
        trigger._handle_rc_channels(low, 10.5)
        service.stop.assert_called_once_with()

        trigger._handle_rc_channels(high, 10.6)
        trigger._handle_rc_channels(high, 10.8)
        trigger._check_timeout(13.0)
        self.assertEqual(service.start.call_count, 2)
        self.assertEqual(service.stop.call_count, 2)
        self.assertIsNone(trigger.commanded_active)

    def test_mavlink_status_distinguishes_wiring_and_rc_stream_states(self):
        service = Mock()
        trigger = aircam.MavlinkTrigger(
            service,
            {
                "enabled": True,
                "rc_channel": 9,
                "start_pwm": 1700,
                "stop_pwm": 1300,
                "timeout_seconds": 2,
                "request_rate_hz": 10,
            },
            serial_factory=Mock(),
        )
        trigger.serial_port = Mock()
        self.assertEqual(trigger.get_status(10.0)["link_state"], "waiting_data")

        trigger.bytes_received = 48
        trigger.last_byte_received = 10.0
        self.assertEqual(trigger.get_status(10.1)["link_state"], "invalid_data")

        trigger._handle_heartbeat(1, 1, heartbeat_payload(), 10.2)
        self.assertEqual(trigger.get_status(10.3)["link_state"], "waiting_rc")

        high = self._rc_channels_payload(9, 1900)
        trigger._handle_rc_channels(high, 10.4, 1, 1)
        status = trigger.get_status(10.5)
        self.assertEqual(status["link_state"], "ready")
        self.assertEqual(status["pwm"], 1900)
        self.assertEqual(status["switch_position"], "start")
        self.assertEqual(status["heartbeat_count"], 1)
        self.assertEqual(status["rc_message_count"], 1)
        self.assertEqual(trigger.get_status(13.0)["link_state"], "rc_timeout")

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

    def test_incremental_recorder_processes_sequence_without_status_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = aircam.CameraService(config(tmp))
            session = Path(tmp) / "photos" / "session"
            session.mkdir()
            (session / "manifest.csv").write_text(
                ",".join(service._manifest_header()) + "\n", encoding="utf-8"
            )
            service.legacy_scan_complete = True
            for number in range(1, 6):
                name = f"pending_{number:08d}.jpg"
                (session / name).write_bytes(b"jpeg")
                service.frame_timestamps_us[name] = (
                    1_700_000_000_000_000 + number
                )

            service._record_new_photos(session, 0.1, include_recent=True)

            self.assertEqual(service.photo_count, 5)
            self.assertEqual(service.next_pending_number, 6)
            service.status.session = "session"
            with patch.object(
                Path, "glob", side_effect=AssertionError("unexpected scan")
            ):
                self.assertEqual(service.get_status()["photo_count"], 5)

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

    def test_session_listing_delete_and_clear_preserve_storage_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = aircam.CameraService(config(tmp))
            first = Path(tmp) / "photos" / "20260802_120000"
            first.mkdir()
            (first / "photo_00000001.jpg").write_bytes(b"jpeg-one")
            (first / "manifest.csv").write_text("name\n", encoding="utf-8")
            (first / "session.json").write_text(
                json.dumps(
                    {
                        "started_at": "2026-08-02T04:00:00+00:00",
                        "interval_seconds": 1,
                    }
                ),
                encoding="utf-8",
            )

            sessions = service.list_sessions()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["session"], first.name)
            self.assertEqual(sessions[0]["photo_count"], 1)
            self.assertGreaterEqual(sessions[0]["size_bytes"], len(b"jpeg-one"))

            service.status.session = first.name
            deleted = service.delete_session(first.name)
            self.assertEqual(deleted["deleted_session"], first.name)
            self.assertFalse(first.exists())
            self.assertTrue(service.data_dir.is_dir())
            self.assertTrue(service.state_path.is_file())

            second = Path(tmp) / "photos" / "20260802_130000"
            second.mkdir()
            (second / "photo_00000001.jpg").write_bytes(b"jpeg-two")
            cleared = service.clear_sessions()
            self.assertEqual(cleared["deleted_sessions"], 1)
            self.assertTrue(service.data_dir.is_dir())
            self.assertEqual(service.list_sessions(), [])

    def test_session_storage_actions_reject_busy_and_unsafe_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = aircam.CameraService(config(tmp))
            session = Path(tmp) / "photos" / "20260802_120000"
            session.mkdir()
            with self.assertRaises(aircam.AirCamError):
                service.delete_session("../AirCam")
            service.status.state = "capturing"
            with self.assertRaises(aircam.AirCamError):
                service.delete_session(session.name)
            with self.assertRaises(aircam.AirCamError):
                service.clear_sessions()

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
            runner, _state = fake_v4l2_runner(
                {"exposure_time_absolute": {"min": 3, "max": 2047, "value": 100}}
            )
            service = aircam.CameraService(config(tmp), run=runner)
            session = Path(tmp) / "photos" / "session"
            session.mkdir()
            service.session_dir = session
            service.status.state = "capturing"
            service.set_controls({"exposure_time_absolute": 123})
            events = (session / "control-events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(events), 1)
            event = json.loads(events[0])
            self.assertEqual(event["controls"]["exposure_time_absolute"], 123)
            self.assertEqual(
                service.get_status()["active_controls"]["exposure_time_absolute"],
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

    def test_duration_auto_stops_capture_without_remote_command(self):
        class FakeProcess:
            pid = 201

            def __init__(self):
                self.return_code = None

            def poll(self):
                return self.return_code

        with tempfile.TemporaryDirectory() as tmp:
            value = config(tmp)
            Path(value["camera"]["device"]).touch()
            service = aircam.CameraService(value)
            process = FakeProcess()
            service._spawn_ffmpeg = Mock(return_value=process)
            service._signal_process = lambda target: setattr(
                target, "return_code", 0
            )

            started = service.start(0.05, 0.15)
            self.assertEqual(started["duration_seconds"], 0.15)
            self.assertIsNotNone(started["remaining_seconds"])
            self.assertIsNotNone(started["auto_stop_at"])

            deadline = time.time() + 2
            while time.time() < deadline:
                if service.get_status()["state"] == "idle":
                    break
                time.sleep(0.02)
            stopped = service.get_status()
            self.assertEqual(stopped["state"], "idle")
            self.assertEqual(stopped["stop_reason"], "duration_elapsed")
            self.assertIsNone(stopped["remaining_seconds"])
            self.assertIsNotNone(stopped["stopped_at"])

            session = json.loads(
                (Path(tmp) / "photos" / stopped["session"] / "session.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(session["duration_seconds"], 0.15)

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
                self.assertEqual(
                    body["status"]["mavlink"]["link_state"],
                    "disabled",
                )
                self.assertEqual(
                    body["status"]["pwm"]["signal_state"],
                    "disabled",
                )

                start_request = urllib.request.Request(
                    base + "/api/start",
                    method="POST",
                    data=json.dumps(
                        {"interval_seconds": 0.5, "duration_seconds": 120}
                    ).encode(),
                    headers={
                        "X-AirCam-Token": "test",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(start_request, timeout=2) as response:
                    self.assertTrue(json.loads(response.read())["ok"])
                camera.start.assert_called_once_with(0.5, 120)

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

    def test_http_controls_returns_hardware_metadata_and_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = config(tmp)
            runner, state = fake_v4l2_runner(
                {
                    "auto_exposure": {
                        "type": "menu",
                        "min": 0,
                        "max": 3,
                        "value": 3,
                        "menu": {"1": "Manual Mode", "3": "Aperture Priority Mode"},
                    },
                    "exposure_time_absolute": {"min": 3, "max": 2047, "value": 100},
                }
            )
            camera = aircam.CameraService(value, run=runner)
            index = Path(tmp) / "index.html"
            index.write_text("<p>AirCam</p>", encoding="utf-8")
            server = aircam.AirCamHttpServer(("127.0.0.1", 0), camera, value, index)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            headers = {"X-AirCam-Token": "test", "Content-Type": "application/json"}
            try:
                request = urllib.request.Request(base + "/api/controls", headers=headers)
                with urllib.request.urlopen(request, timeout=2) as response:
                    current = json.loads(response.read())
                self.assertEqual(
                    current["control_state"]["controls"]["exposure_time_absolute"]["max"],
                    2047,
                )

                request = urllib.request.Request(
                    base + "/api/controls",
                    method="POST",
                    data=json.dumps(
                        {"controls": {"auto_exposure": 1, "exposure_time_absolute": 80}}
                    ).encode(),
                    headers=headers,
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    changed = json.loads(response.read())
                self.assertEqual(state["exposure_time_absolute"], 80)
                self.assertEqual(
                    changed["control_state"]["effective_controls"]["exposure_time_absolute"],
                    80,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_session_download_ticket_and_confirmed_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = config(tmp)
            camera = aircam.CameraService(value)
            session = Path(tmp) / "photos" / "20260802_140000"
            session.mkdir()
            (session / "photo_00000001.jpg").write_bytes(b"jpeg-data")
            (session / "pending_00000002.jpg.tmp").write_bytes(b"incomplete")
            (session / "manifest.csv").write_text(
                "filename\nphoto_00000001.jpg\n", encoding="utf-8"
            )
            (session / "session.json").write_text(
                json.dumps({"started_at": "2026-08-02T06:00:00+00:00"}),
                encoding="utf-8",
            )
            index = Path(tmp) / "index.html"
            index.write_text("<p>AirCam</p>", encoding="utf-8")
            server = aircam.AirCamHttpServer(
                ("127.0.0.1", 0), camera, value, index
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            auth_headers = {
                "X-AirCam-Token": "test",
                "Content-Type": "application/json",
            }
            try:
                list_request = urllib.request.Request(
                    base + "/api/sessions", headers=auth_headers
                )
                with urllib.request.urlopen(list_request, timeout=2) as response:
                    sessions = json.loads(response.read())["sessions"]
                self.assertEqual(sessions[0]["session"], session.name)

                ticket_request = urllib.request.Request(
                    base + f"/api/sessions/{session.name}/download-ticket",
                    method="POST",
                    data=b"{}",
                    headers=auth_headers,
                )
                with urllib.request.urlopen(ticket_request, timeout=2) as response:
                    ticket_body = json.loads(response.read())
                download_url = base + ticket_body["download_url"]
                with urllib.request.urlopen(download_url, timeout=2) as response:
                    archive_bytes = response.read()
                    self.assertIn(
                        "attachment", response.headers["Content-Disposition"]
                    )
                    self.assertEqual(
                        response.headers["Transfer-Encoding"], "chunked"
                    )
                    self.assertIsNone(response.headers["Content-Length"])
                with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                    self.assertEqual(
                        archive.read(f"{session.name}/photo_00000001.jpg"),
                        b"jpeg-data",
                    )
                    self.assertFalse(
                        any(
                            Path(name).name.startswith("pending_")
                            for name in archive.namelist()
                        )
                    )
                self.assertFalse(camera.export_active)

                with self.assertRaises(urllib.error.HTTPError) as reused:
                    urllib.request.urlopen(download_url, timeout=2)
                self.assertEqual(reused.exception.code, 400)

                wrong_clear = urllib.request.Request(
                    base + "/api/sessions/clear",
                    method="POST",
                    data=json.dumps({"confirm_text": "清空"}).encode("utf-8"),
                    headers=auth_headers,
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(wrong_clear, timeout=2)
                self.assertEqual(rejected.exception.code, 400)
                self.assertTrue(session.is_dir())

                clear_request = urllib.request.Request(
                    base + "/api/sessions/clear",
                    method="POST",
                    data=json.dumps(
                        {"confirm_text": aircam.CLEAR_ALL_CONFIRMATION}
                    ).encode("utf-8"),
                    headers=auth_headers,
                )
                with urllib.request.urlopen(clear_request, timeout=2) as response:
                    cleared = json.loads(response.read())
                self.assertEqual(cleared["deleted_sessions"], 1)
                self.assertTrue(camera.data_dir.is_dir())
                self.assertFalse(session.exists())
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
        self.assertIn("RuntimeDirectory=aircam", unit)
        self.assertIn("WorkingDirectory=/run/aircam", unit)

    def test_web_ui_exposes_interval_floor_and_action_feedback(self):
        page = (
            Path(__file__).resolve().parents[1] / "web" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn('min="0.0334"', page)
        self.assertIn('data-interval="0.0334"', page)
        self.assertIn("最小 0.0334 秒", page)
        self.assertIn("function adjustInterval", page)
        self.assertIn("function runButton", page)
        self.assertIn('id="durationMinutes"', page)
        self.assertIn('id="durationRemaining"', page)
        self.assertIn("duration_seconds:duration", page)
        self.assertIn("function renderDurationStatus", page)
        self.assertIn('id="pwmBadge"', page)
        self.assertIn('id="pwmValue"', page)
        self.assertIn('id="photoDirectory"', page)
        self.assertIn("function applyPwmStatus", page)
        self.assertIn("function applyControlState", page)
        self.assertIn("state.controls.exposure_time_absolute", page)
        self.assertIn("state.controls.gain", page)
        self.assertIn('id="gainRange"', page)
        self.assertIn('id="brightnessRange"', page)
        self.assertIn("function applyImageControls", page)
        self.assertIn("当前摄像头未提供 gain 控制项", page)
        self.assertIn("摄像头硬件读回参数", page)
        self.assertIn("不会用网页默认值覆盖摄像头", page)
        self.assertIn('id="sessionList"', page)
        self.assertIn('id="clearDialog"', page)
        self.assertIn("function downloadSession", page)
        self.assertIn("BROWSER_IMPORT_LIMIT_BYTES", page)
        self.assertIn("response.body.getReader()", page)
        self.assertIn("URL.createObjectURL(blob)", page)
        self.assertIn("接收中 ${percent}%", page)
        self.assertIn("function clearAllSessions", page)
        self.assertIn("/download-ticket", page)
        self.assertIn('button.disabled = false;', page)
        self.assertIn('aria-live="polite"', page)
        self.assertNotIn('class="brand-mark"', page)
        self.assertNotIn("onclick=", page)
        for button_id in (
            "refreshBtn",
            "startBtn",
            "stopBtn",
            "applyExposureBtn",
            "applyImageControlsBtn",
            "loadControlsBtn",
            "applyCustomBtn",
            "latestBtn",
            "refreshSessionsBtn",
            "clearAllBtn",
            "confirmClearBtn",
            "saveTokenBtn",
        ):
            self.assertIn(f'$("{button_id}").addEventListener', page)

    def test_readme_documents_every_config_parameter(self):
        root = Path(__file__).resolve().parents[1]
        example = json.loads(
            (root / "config.example.json").read_text(encoding="utf-8")
        )
        readme = (root / "README.zh-CN.md").read_text(encoding="utf-8")
        for section, values in example.items():
            self.assertIn(f"`{section}`", readme)
            for parameter in values:
                self.assertIn(
                    f"`{parameter}`",
                    readme,
                    f"README 未说明 {section}.{parameter}",
                )


if __name__ == "__main__":
    unittest.main()
