#!/usr/bin/env python3
"""AirCam: a small control service for a V4L2 USB camera.

The service delegates camera capture and controls to ffmpeg and v4l2-ctl.  It
provides an authenticated HTTP API, a small web UI, and optional GPIO or
MAVLink RC-channel start/stop inputs.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


LOG = logging.getLogger("aircam")
CONTROL_NAME = re.compile(r"^[A-Za-z0-9_]+$")
V4L2_CONTROL_LINE = re.compile(
    r"^\s*([A-Za-z0-9_]+)\s+0x[0-9A-Fa-f]+\s+\(([^)]+)\)\s*:\s*(.*)$"
)
V4L2_MENU_LINE = re.compile(r"^\s+(-?\d+):\s*(.+?)\s*$")
V4L2_NUMERIC_FIELD = re.compile(
    r"(?:^|\s)(min|max|step|default|value)=(-?\d+)(?=\s|$)"
)
MIN_CAPTURE_INTERVAL_SECONDS = 0.0334
MAX_CAPTURE_DURATION_SECONDS = 7 * 24 * 60 * 60
SHOWINFO_TIMEBASE = re.compile(r"config in time_base:\s*(\d+)/(\d+)")
SHOWINFO_FRAME = re.compile(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)")
PENDING_PHOTO = re.compile(r"^pending_(\d{8})\.jpg$")
TIMED_PHOTO = re.compile(
    r"^photo_\d{8}T\d{6}\.\d{6}Z_(\d{8})\.jpg$"
)
LEGACY_PHOTO = re.compile(r"^photo_(\d{8})\.jpg$")
SESSION_NAME = re.compile(r"^\d{8}_\d{6}(?:_\d+)?$")
DOWNLOAD_TICKET_TTL_SECONDS = 60
CLEAR_ALL_CONFIRMATION = "清空全部照片"
MAVLINK_V1_MAGIC = 0xFE
MAVLINK_V2_MAGIC = 0xFD
MAVLINK_V2_SIGNED_FLAG = 0x01
MAVLINK_HEARTBEAT_ID = 0
MAVLINK_HEARTBEAT_CRC_EXTRA = 50
MAVLINK_RC_CHANNELS_ID = 65
MAVLINK_RC_CHANNELS_CRC_EXTRA = 118
MAVLINK_COMMAND_LONG_ID = 76
MAVLINK_COMMAND_LONG_CRC_EXTRA = 152
MAV_CMD_SET_MESSAGE_INTERVAL = 511


class AirCamError(RuntimeError):
    """An error safe to return to an API client."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AirCamError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise AirCamError(f"配置文件不是有效 JSON：{exc}") from exc

    required = ("camera", "capture", "server", "storage")
    missing = [key for key in required if key not in config]
    if missing:
        raise AirCamError(f"配置文件缺少字段：{', '.join(missing)}")
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    camera = config["camera"]
    capture = config["capture"]
    server = config["server"]
    storage = config["storage"]

    if not isinstance(camera.get("device"), str) or not camera["device"]:
        raise AirCamError("camera.device 必须是非空路径")
    if not isinstance(camera.get("input_format"), str) or not camera["input_format"]:
        raise AirCamError("camera.input_format 必须是非空字符串")
    controls = camera.get("controls", {})
    if not isinstance(controls, dict):
        raise AirCamError("camera.controls 必须是对象")
    for name, value in controls.items():
        if not CONTROL_NAME.fullmatch(name):
            raise AirCamError(f"非法控制项名称：{name}")
        if isinstance(value, bool):
            continue
        if not isinstance(value, int):
            raise AirCamError(f"camera.controls.{name} 必须是整数或布尔值")

    width = capture.get("width", 1920)
    height = capture.get("height", 1080)
    source_fps = capture.get("source_fps", 30)
    quality = capture.get("jpeg_quality", 2)
    interval = capture.get("interval_seconds", 1)
    max_restarts = capture.get("max_restarts", 3)
    restart_delay = capture.get("restart_delay_seconds", 2)
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        raise AirCamError("capture.width 必须是正整数")
    if isinstance(height, bool) or not isinstance(height, int) or height < 1:
        raise AirCamError("capture.height 必须是正整数")
    if (
        isinstance(source_fps, bool)
        or not isinstance(source_fps, int)
        or not 1 <= source_fps <= 240
    ):
        raise AirCamError("capture.source_fps 必须是 1 到 240 的整数")
    if (
        isinstance(quality, bool)
        or not isinstance(quality, int)
        or not 2 <= quality <= 31
    ):
        raise AirCamError("capture.jpeg_quality 必须是 2 到 31 的整数")
    check_number(
        interval,
        "capture.interval_seconds",
        MIN_CAPTURE_INTERVAL_SECONDS,
        3600,
    )
    if not isinstance(capture.get("auto_restart", True), bool):
        raise AirCamError("capture.auto_restart 必须是布尔值")
    if (
        isinstance(max_restarts, bool)
        or not isinstance(max_restarts, int)
        or not 0 <= max_restarts <= 100
    ):
        raise AirCamError("capture.max_restarts 必须是 0 到 100 的整数")
    check_number(restart_delay, "capture.restart_delay_seconds", 0.1, 300)

    if not isinstance(storage.get("data_dir"), str) or not storage["data_dir"]:
        raise AirCamError("storage.data_dir 必须是非空路径")
    check_number(storage.get("min_free_mb", 512), "storage.min_free_mb", 16, 1048576)
    port = server.get("port", 8080)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise AirCamError("server.port 必须是 1 到 65535 的整数")
    if not isinstance(server.get("token", ""), str):
        raise AirCamError("server.token 必须是字符串")

    pwm = config.get("pwm", {})
    if not isinstance(pwm, dict):
        raise AirCamError("pwm 必须是对象")
    if not isinstance(pwm.get("enabled", False), bool):
        raise AirCamError("pwm.enabled 必须是布尔值")
    bcm_pin = pwm.get("bcm_pin", 17)
    if (
        isinstance(bcm_pin, bool)
        or not isinstance(bcm_pin, int)
        or not 0 <= bcm_pin <= 27
    ):
        raise AirCamError("pwm.bcm_pin 必须是 0 到 27 的整数")
    gpiochip = pwm.get("gpiochip", 4)
    if (
        isinstance(gpiochip, bool)
        or not isinstance(gpiochip, int)
        or not 0 <= gpiochip <= 15
    ):
        raise AirCamError("pwm.gpiochip 必须是 0 到 15 的整数")
    pwm_start = pwm.get("start_pwm", 1700)
    pwm_stop = pwm.get("stop_pwm", 1300)
    pwm_min = pwm.get("min_valid_pwm", 750)
    pwm_max = pwm.get("max_valid_pwm", 2250)
    for value, name in (
        (pwm_start, "start_pwm"),
        (pwm_stop, "stop_pwm"),
        (pwm_min, "min_valid_pwm"),
        (pwm_max, "max_valid_pwm"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 500 <= value <= 2500
        ):
            raise AirCamError(f"pwm.{name} 必须是 500 到 2500 的整数")
    if not pwm_min <= pwm_stop < pwm_start <= pwm_max:
        raise AirCamError(
            "PWM 阈值必须满足 min_valid_pwm <= stop_pwm < start_pwm <= max_valid_pwm"
        )
    check_number(
        pwm.get("debounce_seconds", 0.15),
        "pwm.debounce_seconds",
        0,
        5,
    )
    check_number(
        pwm.get("timeout_seconds", 0.5),
        "pwm.timeout_seconds",
        0.1,
        10,
    )
    if not isinstance(pwm.get("stop_on_timeout", True), bool):
        raise AirCamError("pwm.stop_on_timeout 必须是布尔值")
    min_stable_pulses = pwm.get("min_stable_pulses", 5)
    if (
        isinstance(min_stable_pulses, bool)
        or not isinstance(min_stable_pulses, int)
        or not 2 <= min_stable_pulses <= 50
    ):
        raise AirCamError("pwm.min_stable_pulses 必须是 2 到 50 的整数")

    mavlink = config.get("mavlink", {})
    if not isinstance(mavlink, dict):
        raise AirCamError("mavlink 必须是对象")
    if not isinstance(mavlink.get("enabled", False), bool):
        raise AirCamError("mavlink.enabled 必须是布尔值")
    device = mavlink.get("device", "/dev/serial0")
    if not isinstance(device, str) or not device:
        raise AirCamError("mavlink.device 必须是非空路径")
    baud = mavlink.get("baud", 115200)
    if (
        isinstance(baud, bool)
        or not isinstance(baud, int)
        or not 1200 <= baud <= 3_000_000
    ):
        raise AirCamError("mavlink.baud 必须是 1200 到 3000000 的整数")
    rc_channel = mavlink.get("rc_channel", 9)
    if (
        isinstance(rc_channel, bool)
        or not isinstance(rc_channel, int)
        or not 1 <= rc_channel <= 18
    ):
        raise AirCamError("mavlink.rc_channel 必须是 1 到 18 的整数")
    start_pwm = mavlink.get("start_pwm", 1700)
    stop_pwm = mavlink.get("stop_pwm", 1300)
    for value, name in ((start_pwm, "start_pwm"), (stop_pwm, "stop_pwm")):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 800 <= value <= 2200
        ):
            raise AirCamError(f"mavlink.{name} 必须是 800 到 2200 的整数")
    if stop_pwm >= start_pwm:
        raise AirCamError("mavlink.stop_pwm 必须小于 mavlink.start_pwm")
    check_number(
        mavlink.get("debounce_seconds", 0.15),
        "mavlink.debounce_seconds",
        0,
        5,
    )
    check_number(
        mavlink.get("timeout_seconds", 2),
        "mavlink.timeout_seconds",
        0.5,
        60,
    )
    check_number(
        mavlink.get("reconnect_seconds", 2),
        "mavlink.reconnect_seconds",
        0.1,
        60,
    )
    request_rate_hz = mavlink.get("request_rate_hz", 10)
    if (
        isinstance(request_rate_hz, bool)
        or not isinstance(request_rate_hz, int)
        or not 1 <= request_rate_hz <= 50
    ):
        raise AirCamError("mavlink.request_rate_hz 必须是 1 到 50 的整数")
    if not isinstance(mavlink.get("stop_on_timeout", True), bool):
        raise AirCamError("mavlink.stop_on_timeout 必须是布尔值")

    enabled_triggers = sum(
        bool(config.get(section, {}).get("enabled", False))
        for section in ("gpio", "pwm", "mavlink")
    )
    if enabled_triggers > 1:
        raise AirCamError("gpio、pwm、mavlink 触发方式最多只能启用一种")


def check_number(
    value: Any, name: str, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AirCamError(f"{name} 必须是数字")
    value = float(value)
    if not minimum <= value <= maximum:
        raise AirCamError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def normalize_duration_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AirCamError("duration_seconds 必须是数字")
    value = float(value)
    if value == 0:
        return None
    return check_number(
        value,
        "duration_seconds",
        0.1,
        MAX_CAPTURE_DURATION_SECONDS,
    )


@dataclass
class CaptureStatus:
    state: str = "idle"
    session: str | None = None
    started_at: str | None = None
    stopped_at: str | None = None
    last_error: str | None = None
    pid: int | None = None
    restart_count: int = 0
    duration_seconds: float | None = None
    auto_stop_at: str | None = None
    stop_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "session": self.session,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "last_error": self.last_error,
            "pid": self.pid,
            "restart_count": self.restart_count,
            "duration_seconds": self.duration_seconds,
            "auto_stop_at": self.auto_stop_at,
            "stop_reason": self.stop_reason,
        }


class CameraService:
    def __init__(
        self,
        config: dict[str, Any],
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        validate_config(config)
        self.config = config
        self.popen = popen
        self.run = run
        self.lock = threading.RLock()
        self.process: subprocess.Popen[bytes] | None = None
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.stop_deadline_monotonic: float | None = None
        self.export_active = False
        self.export_session_name: str | None = None
        self.session_dir: Path | None = None
        self.manifested_photos: set[str] = set()
        self.frame_timestamps_us: dict[str, int] = {}
        self.ffmpeg_reader_threads: dict[int, threading.Thread] = {}
        self.configured_controls: dict[str, int | bool] = dict(
            config["camera"].get("controls", {})
        )
        # Requested values are kept separately from hardware-confirmed values.
        # The latter are populated only by a V4L2 readback.
        self.active_controls: dict[str, int | bool] = dict(
            self.configured_controls
        )
        self.effective_controls: dict[str, int] = {}
        self.control_metadata: dict[str, dict[str, Any]] = {}
        self.controls_read_at: str | None = None
        self.control_history: list[tuple[float, dict[str, Any]]] = []
        self.photo_count = 0
        self.next_pending_number = 1
        self.legacy_scan_complete = False
        self.status = CaptureStatus()
        self.data_dir = Path(config["storage"]["data_dir"])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self._save_state()

    @property
    def device(self) -> str:
        return str(self.config["camera"].get("device", "/dev/video0"))

    def _save_state(self) -> None:
        atomic_write_json(self.state_path, self.status.as_dict())

    def _is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def build_ffmpeg_command(
        self,
        output_pattern: Path,
        interval_seconds: float,
        start_number: int = 1,
    ) -> list[str]:
        camera = self.config["camera"]
        capture = self.config["capture"]
        width = int(capture.get("width", 1920))
        height = int(capture.get("height", 1080))
        source_fps = int(capture.get("source_fps", 30))
        quality = int(capture.get("jpeg_quality", 2))
        input_format = str(camera.get("input_format", "mjpeg"))
        interval_text = f"{interval_seconds:g}"
        select_filter = (
            "select=isnan(prev_selected_t)+gt("
            f"floor((t-start_t)/{interval_text})\\,"
            f"floor((prev_selected_t-start_t)/{interval_text})),showinfo"
        )

        if width < 1 or height < 1:
            raise AirCamError("图像宽高必须大于 0")
        if not 1 <= source_fps <= 240:
            raise AirCamError("source_fps 必须在 1 到 240 之间")
        if not 2 <= quality <= 31:
            raise AirCamError("jpeg_quality 必须在 2 到 31 之间")

        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostdin",
            "-copyts",
            "-f",
            "v4l2",
            "-timestamps",
            "abs",
            "-input_format",
            input_format,
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            str(source_fps),
            "-i",
            self.device,
            "-an",
            "-vf",
            select_filter,
            "-fps_mode",
            "vfr",
            "-q:v",
            str(quality),
            "-start_number",
            str(start_number),
            "-atomic_writing",
            "1",
            str(output_pattern),
        ]

    @staticmethod
    def parse_control_listing(raw: str) -> dict[str, dict[str, Any]]:
        controls: dict[str, dict[str, Any]] = {}
        current_menu: dict[str, Any] | None = None
        for line in raw.splitlines():
            match = V4L2_CONTROL_LINE.match(line)
            if match:
                name, control_type, details = match.groups()
                item: dict[str, Any] = {
                    "name": name,
                    "type": control_type,
                    "menu": {},
                    "flags": [],
                }
                for field, value in V4L2_NUMERIC_FIELD.findall(details):
                    item[field] = int(value)
                flags_match = re.search(r"(?:^|\s)flags=(.+)$", details)
                if flags_match:
                    item["flags"] = [
                        value.strip()
                        for value in flags_match.group(1).split(",")
                        if value.strip()
                    ]
                controls[name] = item
                current_menu = item if "menu" in control_type else None
                continue

            menu_match = V4L2_MENU_LINE.match(line)
            if menu_match and current_menu is not None:
                value, label = menu_match.groups()
                current_menu["menu"][value] = label
        return controls

    def read_control_state(self) -> dict[str, Any]:
        result = self.run(
            ["v4l2-ctl", "-d", self.device, "--list-ctrls-menus"],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
        if result.returncode != 0:
            raise AirCamError(
                (result.stderr or result.stdout or "无法读取摄像头控制项").strip()
            )
        metadata = self.parse_control_listing(result.stdout)
        effective = {
            name: int(item["value"])
            for name, item in metadata.items()
            if "value" in item and "inactive" not in item.get("flags", [])
        }
        read_at = utc_now()
        with self.lock:
            self.control_metadata = metadata
            self.effective_controls = effective
            self.controls_read_at = read_at
            requested = dict(self.active_controls)
            configured = dict(self.configured_controls)
        return {
            "raw": result.stdout,
            "controls": metadata,
            "effective_controls": effective,
            "requested_controls": requested,
            "configured_controls": configured,
            "read_at": read_at,
        }

    def list_controls(self) -> str:
        return str(self.read_control_state()["raw"])

    @staticmethod
    def _normalize_control_values(
        controls: dict[str, Any],
    ) -> dict[str, int]:
        if not isinstance(controls, dict) or not controls:
            raise AirCamError("controls 必须是非空对象")
        normalized: dict[str, int] = {}
        for name, raw_value in controls.items():
            if not CONTROL_NAME.fullmatch(name):
                raise AirCamError(f"非法控制项名称：{name}")
            if isinstance(raw_value, bool):
                value = int(raw_value)
            elif isinstance(raw_value, int):
                value = raw_value
            else:
                raise AirCamError(f"{name} 的值必须是整数或布尔值")
            normalized[name] = value
        return normalized

    @staticmethod
    def _validate_control_values(
        controls: dict[str, int], metadata: dict[str, dict[str, Any]]
    ) -> None:
        for name, value in controls.items():
            item = metadata.get(name)
            if item is None:
                raise AirCamError(f"摄像头不支持控制项：{name}")
            minimum = item.get("min")
            maximum = item.get("max")
            if minimum is not None and value < minimum:
                raise AirCamError(f"{name} 不能小于 {minimum}")
            if maximum is not None and value > maximum:
                raise AirCamError(f"{name} 不能大于 {maximum}")
            step = item.get("step")
            if (
                step is not None
                and step > 0
                and minimum is not None
                and (value - minimum) % step != 0
            ):
                raise AirCamError(
                    f"{name} 必须从 {minimum} 起按步长 {step} 取值"
                )
            menu = item.get("menu", {})
            if menu and str(value) not in menu:
                raise AirCamError(f"{name} 不支持值 {value}")

    def initialize_controls(self) -> dict[str, Any]:
        if self.active_controls:
            self.set_controls(dict(self.active_controls))
            return self.control_state()
        return self.read_control_state()

    def control_state(self) -> dict[str, Any]:
        """Return the last hardware readback without invoking v4l2-ctl."""

        with self.lock:
            return {
                "controls": copy.deepcopy(self.control_metadata),
                "effective_controls": dict(self.effective_controls),
                "requested_controls": dict(self.active_controls),
                "configured_controls": dict(self.configured_controls),
                "read_at": self.controls_read_at,
            }

    def _rollback_controls(
        self,
        successful_names: list[str],
        previous_values: dict[str, Any],
    ) -> list[str]:
        rollback_errors: list[str] = []
        for name in reversed(successful_names):
            previous = previous_values.get(name)
            if previous is None:
                continue
            rollback = self.run(
                [
                    "v4l2-ctl",
                    "-d",
                    self.device,
                    "-c",
                    f"{name}={previous}",
                ],
                text=True,
                capture_output=True,
                timeout=8,
                check=False,
            )
            if rollback.returncode != 0:
                rollback_errors.append(name)
        return rollback_errors

    def set_controls(self, controls: dict[str, Any]) -> list[dict[str, Any]]:
        normalized = self._normalize_control_values(controls)
        with self.lock:
            before = self.read_control_state()
            metadata = before["controls"]
            self._validate_control_values(normalized, metadata)
            previous_values = {
                name: metadata[name].get("value") for name in normalized
            }
            results: list[dict[str, Any]] = []
            successful_names: list[str] = []
            failure_message: str | None = None
            ordered = list(normalized.items())
            ordered.sort(key=lambda item: 0 if item[0] == "auto_exposure" else 1)
            for name, value in ordered:
                result = self.run(
                    ["v4l2-ctl", "-d", self.device, "-c", f"{name}={value}"],
                    text=True,
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
                item = {
                    "name": name,
                    "value": value,
                    "ok": result.returncode == 0,
                }
                if result.returncode != 0:
                    failure_message = (
                        result.stderr or result.stdout or "设置失败"
                    ).strip()
                    item["error"] = failure_message
                else:
                    successful_names.append(name)
                results.append(item)
                if failure_message is not None:
                    break

            if failure_message is not None:
                rollback_errors = self._rollback_controls(
                    successful_names, previous_values
                )
                final_state = self.read_control_state()
                failed = next(item for item in results if not item["ok"])
                message = f"{failed['name']}: {failure_message}"
                if rollback_errors:
                    message += (
                        "；回滚失败：" + ", ".join(rollback_errors)
                        + "；最终硬件值："
                        + json.dumps(
                            final_state["effective_controls"],
                            ensure_ascii=False,
                        )
                    )
                else:
                    message += "；已回滚先前成功的参数"
                raise AirCamError(message)

            final_state = self.read_control_state()
            mismatches: list[str] = []
            for name, requested in normalized.items():
                item = final_state["controls"].get(name, {})
                if item.get("value") != requested:
                    mismatches.append(
                        f"{name} 请求 {requested}、读回 {item.get('value')}"
                    )
            if mismatches:
                rollback_errors = self._rollback_controls(
                    successful_names, previous_values
                )
                rolled_back_state = self.read_control_state()
                message = "；".join(mismatches) + "；设置未生效"
                if rollback_errors:
                    message += "；回滚失败：" + ", ".join(rollback_errors)
                else:
                    message += "；已恢复设置前的硬件值"
                message += "；最终硬件值：" + json.dumps(
                    rolled_back_state["effective_controls"], ensure_ascii=False
                )
                raise AirCamError(message)

            self.active_controls.update(normalized)
            # Automatic modes commonly make their paired manual controls
            # inactive. Do not retain those stale manual values for the next
            # capture start/recovery, or reapplying them will fail even though
            # switching to automatic mode itself succeeded.
            for name in list(self.active_controls):
                item = final_state["controls"].get(name)
                if item is not None and "inactive" in item.get("flags", []):
                    self.active_controls.pop(name, None)
            self._record_control_event(time.time())
            return results

    def _record_control_event(self, timestamp: float) -> None:
        snapshot = dict(self.effective_controls)
        self.control_history.append((timestamp, snapshot))
        if (
            self.session_dir is None
            or self.status.state
            not in {"capturing", "recovering", "starting", "stopping"}
        ):
            return
        event = {
            "applied_at_utc": datetime.fromtimestamp(
                timestamp, timezone.utc
            ).isoformat(timespec="milliseconds"),
            "applied_at_unix": timestamp,
            "controls": snapshot,
        }
        with (self.session_dir / "control-events.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _controls_at(self, timestamp: float) -> dict[str, Any]:
        with self.lock:
            for event_time, controls in reversed(self.control_history):
                if event_time <= timestamp:
                    return dict(controls)
            return dict(self.effective_controls)

    def _minimum_free_bytes(self) -> int:
        return int(float(self.config["storage"].get("min_free_mb", 512)) * 1024 * 1024)

    def _ensure_disk_space(self) -> None:
        free = shutil.disk_usage(self.data_dir).free
        minimum = self._minimum_free_bytes()
        if free < minimum:
            raise AirCamError(
                f"磁盘剩余空间不足：{free // (1024 * 1024)} MB，"
                f"至少需要 {minimum // (1024 * 1024)} MB"
            )

    def _spawn_ffmpeg(
        self, session_dir: Path, interval: float, start_number: int
    ) -> subprocess.Popen[bytes]:
        output_pattern = session_dir / "pending_%08d.jpg"
        command = self.build_ffmpeg_command(
            output_pattern, interval, start_number=start_number
        )
        try:
            process = self.popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise AirCamError(f"无法启动 ffmpeg：{exc}") from exc

        reader = threading.Thread(
            target=self._read_ffmpeg_stderr,
            args=(process, session_dir, start_number),
            name=f"ffmpeg-log-{process.pid}",
            daemon=True,
        )
        self.ffmpeg_reader_threads[id(process)] = reader
        reader.start()

        time.sleep(0.25)
        exit_code = process.poll()
        if exit_code is not None:
            self._join_ffmpeg_reader(process)
            log_text = (session_dir / "ffmpeg.log").read_text(
                encoding="utf-8", errors="replace"
            )
            raise AirCamError(
                f"ffmpeg 启动失败（退出码 {exit_code}）：{log_text[-1200:]}"
            )
        return process

    @staticmethod
    def _pts_to_unix_us(pts: int, numerator: int, denominator: int) -> int:
        if denominator <= 0:
            raise ValueError("time base denominator must be positive")
        scaled = pts * numerator * 1_000_000
        if scaled >= 0:
            return (scaled + denominator // 2) // denominator
        return -((-scaled + denominator // 2) // denominator)

    def _read_ffmpeg_stderr(
        self,
        process: subprocess.Popen[bytes],
        session_dir: Path,
        start_number: int,
    ) -> None:
        stream = getattr(process, "stderr", None)
        if stream is None:
            return
        time_base: tuple[int, int] | None = None
        with (session_dir / "ffmpeg.log").open("ab", buffering=0) as log_handle:
            for raw_line in stream:
                if isinstance(raw_line, str):
                    line = raw_line
                    encoded = raw_line.encode("utf-8", errors="replace")
                else:
                    encoded = raw_line
                    line = raw_line.decode("utf-8", errors="replace")
                log_handle.write(encoded)

                match = SHOWINFO_TIMEBASE.search(line)
                if match:
                    time_base = (int(match.group(1)), int(match.group(2)))
                    continue
                match = SHOWINFO_FRAME.search(line)
                if match and time_base is not None:
                    frame_index = int(match.group(1))
                    pts = int(match.group(2))
                    capture_us = self._pts_to_unix_us(
                        pts, time_base[0], time_base[1]
                    )
                    pending_name = (
                        f"pending_{start_number + frame_index:08d}.jpg"
                    )
                    with self.lock:
                        self.frame_timestamps_us[pending_name] = capture_us

    def _join_ffmpeg_reader(
        self, process: subprocess.Popen[bytes], timeout: float = 2.0
    ) -> None:
        reader = self.ffmpeg_reader_threads.pop(id(process), None)
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=timeout)

    @staticmethod
    def _manifest_header() -> list[str]:
        return [
            "filename",
            "capture_time_utc",
            "capture_unix_us",
            "capture_time_source",
            "file_mtime_utc",
            "write_delay_ms",
            "size_bytes",
            "interval_seconds",
            "camera_controls_json",
        ]

    @staticmethod
    def _timed_photo_name(capture_unix_us: int, sequence: int) -> str:
        seconds, microseconds = divmod(capture_unix_us, 1_000_000)
        captured = datetime.fromtimestamp(seconds, timezone.utc).replace(
            microsecond=microseconds
        )
        return (
            f"photo_{captured:%Y%m%dT%H%M%S}."
            f"{microseconds:06d}Z_{sequence:08d}.jpg"
        )

    def _record_new_photos(
        self,
        session_dir: Path,
        interval: float,
        include_recent: bool = False,
    ) -> None:
        now = time.time()
        rows: list[list[Any]] = []
        candidates: list[Path] = []
        # Scan already-finalized photographs only once. During normal capture,
        # ffmpeg writes predictable consecutive pending names, so checking the
        # next expected path avoids repeatedly walking an ever-growing folder.
        if not self.legacy_scan_complete:
            candidates.extend(sorted(session_dir.glob("photo_*.jpg")))
            self.legacy_scan_complete = True
        scan_number = self.next_pending_number
        while True:
            pending = session_dir / f"pending_{scan_number:08d}.jpg"
            if not pending.exists():
                break
            candidates.append(pending)
            scan_number += 1
        for photo in candidates:
            if photo.name in self.manifested_photos:
                continue
            pending_match = PENDING_PHOTO.fullmatch(photo.name)
            try:
                stat = photo.stat()
            except FileNotFoundError:
                if pending_match:
                    break
                continue
            # ffmpeg can leave a zero-byte placeholder when capture is
            # interrupted after opening the next JPEG but before writing it.
            # This is not a completed photograph.
            if stat.st_size == 0:
                if pending_match:
                    break
                continue
            # Avoid recording a file while ffmpeg may still be writing it.
            if not include_recent and now - stat.st_mtime < 0.25:
                if pending_match:
                    break
                continue

            if pending_match:
                with self.lock:
                    capture_unix_us = self.frame_timestamps_us.pop(
                        photo.name, None
                    )
                if capture_unix_us is None and not include_recent:
                    break
                capture_source = (
                    "v4l2_pts_abs"
                    if capture_unix_us is not None
                    else "file_mtime_fallback"
                )
                if capture_unix_us is None:
                    capture_unix_us = round(stat.st_mtime * 1_000_000)
                final_name = self._timed_photo_name(
                    capture_unix_us, int(pending_match.group(1))
                )
                final_path = session_dir / final_name
                if final_path.exists():
                    LOG.error(
                        "refusing to overwrite existing photograph: %s",
                        final_path,
                    )
                    continue
                os.replace(photo, final_path)
                photo = final_path
                stat = photo.stat()
            else:
                capture_unix_us = round(stat.st_mtime * 1_000_000)
                capture_source = "file_mtime_fallback"

            capture_seconds = capture_unix_us / 1_000_000
            capture_time = datetime.fromtimestamp(
                capture_seconds, timezone.utc
            ).isoformat(timespec="microseconds")
            rows.append(
                [
                    photo.name,
                    capture_time,
                    capture_unix_us,
                    capture_source,
                    datetime.fromtimestamp(
                        stat.st_mtime, timezone.utc
                    ).isoformat(timespec="microseconds"),
                    round((stat.st_mtime - capture_seconds) * 1000, 3),
                    stat.st_size,
                    interval,
                    json.dumps(
                        self._controls_at(capture_seconds),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ]
            )
            self.manifested_photos.add(photo.name)
            if pending_match:
                self.next_pending_number = int(pending_match.group(1)) + 1

        if rows:
            with (session_dir / "manifest.csv").open(
                "a", encoding="utf-8", newline=""
            ) as handle:
                writer = csv.writer(handle)
                writer.writerows(rows)
                handle.flush()
                os.fsync(handle.fileno())
            with self.lock:
                self.photo_count += len(rows)

    @staticmethod
    def _next_photo_number(session_dir: Path) -> int:
        highest = 0
        for photo in [
            *session_dir.glob("photo_*.jpg"),
            *session_dir.glob("pending_*.jpg"),
        ]:
            match = (
                TIMED_PHOTO.fullmatch(photo.name)
                or LEGACY_PHOTO.fullmatch(photo.name)
                or PENDING_PHOTO.fullmatch(photo.name)
            )
            if match:
                highest = max(highest, int(match.group(1)))
        return highest + 1

    def start(
        self,
        interval_seconds: Any = None,
        duration_seconds: Any = None,
    ) -> dict[str, Any]:
        with self.lock:
            if self.export_active:
                raise AirCamError("照片正在导入电脑，请等待下载完成后再开始拍照")
            if self._is_running() or self.status.state in {
                "starting",
                "recovering",
                "stopping",
            }:
                raise AirCamError("拍照已经在进行中")
            if not Path(self.device).exists():
                raise AirCamError(f"找不到摄像头设备：{self.device}")
            self._ensure_disk_space()

            configured_interval = self.config["capture"].get(
                "interval_seconds", 1.0
            )
            interval = check_number(
                configured_interval if interval_seconds is None else interval_seconds,
                "interval_seconds",
                MIN_CAPTURE_INTERVAL_SECONDS,
                3600,
            )
            duration = normalize_duration_seconds(duration_seconds)
            started_at = utc_now()
            auto_stop_at = (
                (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=duration)
                ).isoformat(timespec="seconds")
                if duration is not None
                else None
            )

            controls = dict(self.active_controls)
            if controls:
                self.set_controls(controls)

            local_now = datetime.now().astimezone()
            base_session = local_now.strftime("%Y%m%d_%H%M%S")
            session_name = base_session
            suffix = 1
            while (self.data_dir / session_name).exists():
                suffix += 1
                session_name = f"{base_session}_{suffix}"
            session_dir = self.data_dir / session_name
            session_dir.mkdir(parents=True)
            self.session_dir = session_dir
            self.manifested_photos = set()
            self.frame_timestamps_us = {}
            self.ffmpeg_reader_threads = {}
            self.control_history = []
            self.photo_count = 0
            self.next_pending_number = 1
            self.legacy_scan_complete = True
            with (session_dir / "manifest.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                csv.writer(handle).writerow(self._manifest_header())
                handle.flush()
                os.fsync(handle.fileno())

            metadata = {
                "session": session_name,
                "started_at": started_at,
                "device": self.device,
                "interval_seconds": interval,
                "duration_seconds": duration,
                "auto_stop_at": auto_stop_at,
                "camera": self.config["camera"],
                "active_controls": dict(self.active_controls),
                "capture": self.config["capture"],
                "timestamping": {
                    "source": "V4L2 absolute frame PTS",
                    "resolution": "microseconds",
                    "filename_format": (
                        "photo_YYYYMMDDTHHMMSS.ffffffZ_NNNNNNNN.jpg"
                    ),
                },
            }
            atomic_write_json(session_dir / "session.json", metadata)
            self.status = CaptureStatus(
                state="starting",
                session=session_name,
                started_at=metadata["started_at"],
                duration_seconds=duration,
                auto_stop_at=auto_stop_at,
            )
            self._record_control_event(time.time())
            try:
                process = self._spawn_ffmpeg(session_dir, interval, start_number=1)
            except AirCamError as exc:
                self.status = CaptureStatus(
                    state="error",
                    session=session_name,
                    started_at=metadata["started_at"],
                    stopped_at=utc_now(),
                    last_error=str(exc),
                    duration_seconds=duration,
                    auto_stop_at=auto_stop_at,
                )
                self.stop_deadline_monotonic = None
                self._save_state()
                raise

            self.stop_event.clear()
            self.stop_deadline_monotonic = (
                time.monotonic() + duration
                if duration is not None
                else None
            )
            self.process = process
            self.status = CaptureStatus(
                state="capturing",
                session=session_name,
                started_at=metadata["started_at"],
                pid=process.pid,
                duration_seconds=duration,
                auto_stop_at=auto_stop_at,
            )
            self._save_state()
            self.worker = threading.Thread(
                target=self._supervise_session,
                args=(session_dir, interval),
                name="capture-supervisor",
                daemon=True,
            )
            self.worker.start()
            LOG.info("capture started: session=%s pid=%s", session_name, process.pid)
            return self.get_status()

    def _duration_has_elapsed(self) -> bool:
        with self.lock:
            deadline = self.stop_deadline_monotonic
            if deadline is None or time.monotonic() < deadline:
                return False
            self.stop_event.set()
            self.status.state = "stopping"
            self.status.stop_reason = "duration_elapsed"
            self._save_state()
            return True

    def _finish_recovery_stop(self) -> None:
        with self.lock:
            self.status.state = "idle"
            self.status.stopped_at = utc_now()
            self.status.pid = None
            self.stop_deadline_monotonic = None
            self._save_state()

    @staticmethod
    def _signal_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            return

    def _supervise_session(
        self, session_dir: Path, interval: float
    ) -> None:
        auto_restart = bool(self.config["capture"].get("auto_restart", True))
        max_restarts = int(self.config["capture"].get("max_restarts", 3))
        restart_delay = float(
            self.config["capture"].get("restart_delay_seconds", 2)
        )
        disk_guard_triggered = False
        duration_elapsed = False

        while True:
            with self.lock:
                process = self.process
            if process is None:
                return

            self._record_new_photos(session_dir, interval)

            if not self.stop_event.is_set() and self._duration_has_elapsed():
                duration_elapsed = True
                LOG.info("capture duration elapsed: session=%s", self.status.session)
                self._signal_process(process)

            if not self.stop_event.is_set():
                free = shutil.disk_usage(self.data_dir).free
                if free < self._minimum_free_bytes():
                    disk_guard_triggered = True
                    self.stop_event.set()
                    with self.lock:
                        self.status.state = "stopping"
                        self.status.stop_reason = "disk_guard"
                        self.status.last_error = (
                            "已停止拍照：磁盘剩余空间低于安全阈值"
                        )
                        self._save_state()
                    LOG.error(self.status.last_error)
                    self._signal_process(process)

            return_code = process.poll()
            if return_code is None:
                if self.stop_event.is_set():
                    self._signal_process(process)
                time.sleep(0.25)
                continue

            self._join_ffmpeg_reader(process)
            self._record_new_photos(
                session_dir, interval, include_recent=True
            )
            with self.lock:
                if self.process is process:
                    self.process = None
                    self.status.pid = None

            if self.stop_event.is_set():
                with self.lock:
                    self.status.stopped_at = utc_now()
                    self.status.state = "error" if disk_guard_triggered else "idle"
                    if not duration_elapsed and self.status.stop_reason is None:
                        self.status.stop_reason = "manual"
                    self.stop_deadline_monotonic = None
                    self._save_state()
                return

            with self.lock:
                restart_count = self.status.restart_count
                self.status.last_error = (
                    f"ffmpeg 意外退出，退出码 {return_code}"
                )
                if not auto_restart or restart_count >= max_restarts:
                    self.status.state = "error"
                    self.status.stopped_at = utc_now()
                    self._save_state()
                    LOG.error(
                        "%s；自动恢复次数已用完", self.status.last_error
                    )
                    return
                self.status.state = "recovering"
                self.status.restart_count += 1
                self._save_state()
                attempt = self.status.restart_count

            while True:
                if self._duration_has_elapsed():
                    self._finish_recovery_stop()
                    return
                LOG.warning(
                    "capture process exited; recovery attempt %s/%s",
                    attempt,
                    max_restarts,
                )
                with self.lock:
                    deadline = self.stop_deadline_monotonic
                wait_seconds = restart_delay
                if deadline is not None:
                    wait_seconds = min(
                        wait_seconds,
                        max(0.0, deadline - time.monotonic()),
                    )
                if self.stop_event.wait(wait_seconds):
                    self._finish_recovery_stop()
                    return
                if self._duration_has_elapsed():
                    self._finish_recovery_stop()
                    return

                next_number = self._next_photo_number(session_dir)
                with self.lock:
                    self.next_pending_number = next_number
                try:
                    if not Path(self.device).exists():
                        raise AirCamError(f"找不到摄像头设备：{self.device}")
                    self._ensure_disk_space()
                    controls = dict(self.active_controls)
                    if controls:
                        self.set_controls(controls)
                    replacement = self._spawn_ffmpeg(
                        session_dir, interval, start_number=next_number
                    )
                    break
                except AirCamError as exc:
                    with self.lock:
                        self.status.last_error = f"第 {attempt} 次恢复失败：{exc}"
                        if self.status.restart_count >= max_restarts:
                            self.status.state = "error"
                            self.status.stopped_at = utc_now()
                            self._save_state()
                            return
                        self.status.restart_count += 1
                        attempt = self.status.restart_count
                        self._save_state()

            with self.lock:
                self.process = replacement
                self.status.state = "capturing"
                self.status.pid = replacement.pid
                self.status.last_error = None
                self._save_state()
            LOG.info("capture recovered: pid=%s", replacement.pid)

    def stop(self) -> dict[str, Any]:
        with self.lock:
            if not self._is_running() and self.status.state != "recovering":
                self.process = None
                self.status.state = "idle"
                self.status.pid = None
                self._save_state()
                return self.get_status()
            process = self.process
            worker = self.worker
            self.stop_event.set()
            self.status.state = "stopping"
            if self.status.stop_reason is None:
                self.status.stop_reason = "manual"
            self._save_state()

        if process is not None:
            self._signal_process(process)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=12)

        with self.lock:
            current_process = self.process
        if current_process is not None and current_process.poll() is None:
            current_process.terminate()
            try:
                current_process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                current_process.kill()
                current_process.wait(timeout=2)

        with self.lock:
            self.process = None
            self.worker = None
            self.status.state = "idle"
            self.status.pid = None
            self.status.stopped_at = utc_now()
            self.stop_deadline_monotonic = None
            self._save_state()
            LOG.info("capture stopped: session=%s", self.status.session)
            return self.get_status()

    def get_status(self) -> dict[str, Any]:
        with self.lock:
            status = self.status.as_dict()
            status["photo_count"] = self.photo_count
            disk = shutil.disk_usage(self.data_dir)
            status["disk"] = {
                "free_bytes": disk.free,
                "total_bytes": disk.total,
            }
            status["device"] = self.device
            status["data_dir"] = str(self.data_dir)
            # Keep active_controls as a compatibility alias, but expose the
            # requested/configured values separately from hardware readback.
            status["active_controls"] = dict(self.effective_controls)
            status["effective_controls"] = dict(self.effective_controls)
            status["requested_controls"] = dict(self.active_controls)
            status["configured_controls"] = dict(self.configured_controls)
            status["controls_read_at"] = self.controls_read_at
            status["export_active"] = self.export_active
            deadline = self.stop_deadline_monotonic
            status["remaining_seconds"] = (
                max(0.0, deadline - time.monotonic())
                if deadline is not None
                and self.status.state
                in {"starting", "capturing", "recovering", "stopping"}
                else None
            )
            return status

    def _capture_is_busy(self) -> bool:
        return self._is_running() or self.status.state in {
            "starting",
            "capturing",
            "recovering",
            "stopping",
        }

    def _require_storage_idle(self) -> None:
        if self._capture_is_busy():
            raise AirCamError("拍摄进行中，结束拍照后才能导入或清理照片")
        if self.export_active:
            raise AirCamError("照片正在导入电脑，请等待下载完成")

    def _resolve_session_dir(self, session_name: str) -> Path:
        if not isinstance(session_name, str) or not SESSION_NAME.fullmatch(
            session_name
        ):
            raise AirCamError("任务编号格式无效")
        data_root = self.data_dir.resolve()
        candidate = self.data_dir / session_name
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise AirCamError("任务不存在或已经被清理") from exc
        if resolved.parent != data_root or not resolved.is_dir() or candidate.is_symlink():
            raise AirCamError("任务目录无效")
        return resolved

    @staticmethod
    def _session_size(session_dir: Path) -> int:
        total = 0
        for path in session_dir.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    continue
        return total

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.lock:
            active_session = self.status.session
            sessions: list[dict[str, Any]] = []
            try:
                candidates = sorted(self.data_dir.iterdir(), reverse=True)
            except FileNotFoundError:
                candidates = []
            for session_dir in candidates:
                if (
                    not session_dir.is_dir()
                    or session_dir.is_symlink()
                    or not SESSION_NAME.fullmatch(session_dir.name)
                ):
                    continue
                metadata: dict[str, Any] = {}
                metadata_path = session_dir / "session.json"
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if not isinstance(metadata, dict):
                        metadata = {}
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    metadata = {}
                photos = 0
                for photo in session_dir.glob("photo_*.jpg"):
                    try:
                        if photo.is_file() and photo.stat().st_size > 0:
                            photos += 1
                    except FileNotFoundError:
                        continue
                sessions.append(
                    {
                        "session": session_dir.name,
                        "started_at": metadata.get("started_at"),
                        "duration_seconds": metadata.get("duration_seconds"),
                        "interval_seconds": metadata.get("interval_seconds"),
                        "photo_count": photos,
                        "size_bytes": self._session_size(session_dir),
                        "active": session_dir.name == active_session
                        and self._capture_is_busy(),
                    }
                )
            return sessions

    def begin_export(self, session_name: str) -> Path:
        with self.lock:
            self._require_storage_idle()
            session_dir = self._resolve_session_dir(session_name)
            self.export_active = True
            self.export_session_name = session_name
            return session_dir

    def finish_export(self) -> None:
        with self.lock:
            self.export_active = False
            self.export_session_name = None

    def validate_export_session(self, session_name: str) -> None:
        with self.lock:
            self._require_storage_idle()
            self._resolve_session_dir(session_name)

    def delete_session(self, session_name: str) -> dict[str, Any]:
        with self.lock:
            self._require_storage_idle()
            session_dir = self._resolve_session_dir(session_name)
            shutil.rmtree(session_dir)
            if self.status.session == session_name:
                self.status = CaptureStatus()
                self.session_dir = None
                self.photo_count = 0
                self._save_state()
            return {"deleted_session": session_name}

    def clear_sessions(self) -> dict[str, Any]:
        with self.lock:
            self._require_storage_idle()
            deleted_sessions = 0
            try:
                candidates = list(self.data_dir.iterdir())
            except FileNotFoundError:
                candidates = []
            for candidate in candidates:
                if (
                    candidate.is_dir()
                    and not candidate.is_symlink()
                    and SESSION_NAME.fullmatch(candidate.name)
                ):
                    shutil.rmtree(candidate)
                    deleted_sessions += 1
            self.status = CaptureStatus()
            self.session_dir = None
            self.photo_count = 0
            self._save_state()
            return {"deleted_sessions": deleted_sessions}

    def latest_photo(self) -> Path | None:
        with self.lock:
            session_dirs: list[Path] = []
            if self.status.session:
                session_dirs.append(self.data_dir / self.status.session)
            try:
                stored_sessions = sorted(
                    (
                        path
                        for path in self.data_dir.iterdir()
                        if path.is_dir()
                    ),
                    reverse=True,
                )
            except FileNotFoundError:
                stored_sessions = []
            session_dirs.extend(
                path for path in stored_sessions if path not in session_dirs
            )

            for session_dir in session_dirs:
                for photo in sorted(
                    session_dir.glob("photo_*.jpg"), reverse=True
                ):
                    try:
                        if photo.stat().st_size > 0:
                            return photo
                    except FileNotFoundError:
                        continue
            return None

    def shutdown(self) -> None:
        try:
            self.stop()
        except Exception:
            LOG.exception("failed to stop capture during shutdown")


class GpioTrigger:
    """Optional high=start, low=stop digital trigger.

    Use an optocoupler, transistor, or 3.3 V-safe flight-controller output.
    Never connect a 5 V receiver signal directly to a Raspberry Pi GPIO.
    """

    def __init__(self, service: CameraService, config: dict[str, Any]) -> None:
        self.service = service
        self.config = config
        self.device: Any = None

    def start(self) -> None:
        if not self.config.get("enabled", False):
            return
        try:
            from gpiozero import DigitalInputDevice
        except ImportError as exc:
            raise AirCamError(
                "GPIO 已启用，但缺少 gpiozero；请安装 python3-gpiozero 和 python3-lgpio"
            ) from exc

        pin = int(self.config.get("bcm_pin", 17))
        active_high = bool(self.config.get("active_high", True))
        pull_up = self.config.get("pull_up", None)
        bounce_time = float(self.config.get("bounce_time", 0.15))
        self.device = DigitalInputDevice(
            pin=pin,
            pull_up=pull_up,
            active_state=active_high,
            bounce_time=bounce_time,
        )

        def start_capture() -> None:
            try:
                if not self.service.get_status()["state"] == "capturing":
                    self.service.start()
            except Exception:
                LOG.exception("GPIO start failed")

        def stop_capture() -> None:
            try:
                self.service.stop()
            except Exception:
                LOG.exception("GPIO stop failed")

        self.device.when_activated = start_capture
        self.device.when_deactivated = stop_capture
        LOG.info("GPIO trigger enabled on BCM %s", pin)

        # Honor the switch position present at service startup.
        if self.device.is_active:
            start_capture()

    def close(self) -> None:
        if self.device is not None:
            self.device.close()


class PwmTrigger:
    """Measure a 3.3 V servo PWM signal and control capture safely."""

    def __init__(
        self,
        service: CameraService,
        config: dict[str, Any],
        device_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.service = service
        self.config = config
        self.device_factory = device_factory
        self.clock = clock
        self.clock_ns = clock_ns
        self.device: Any = None
        self.gpio_chip: Any = None
        self.gpio_line: Any = None
        self.uses_kernel_timestamps = False
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.rise_ns: int | None = None
        self.previous_rise_ns: int | None = None
        self.last_valid_pulse: float | None = None
        self.last_pulse_seen: float | None = None
        self.last_pwm: int | None = None
        self.frequency_hz: float | None = None
        self.pulse_count = 0
        self.invalid_pulse_count = 0
        self.commanded_active: bool | None = None
        self.pending_active: bool | None = None
        self.pending_since: float | None = None
        self.pending_first_pulse_count: int | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if not self.config.get("enabled", False):
            return
        if self.device_factory is None:
            try:
                import gpiod
            except ImportError as exc:
                raise AirCamError(
                    "PWM 触发已启用，但缺少 gpiod；请安装 python3-libgpiod"
                ) from exc
            pin = int(self.config.get("bcm_pin", 17))
            gpiochip = int(self.config.get("gpiochip", 4))
            try:
                chip = gpiod.Chip(f"gpiochip{gpiochip}")
            except Exception as exc:
                raise AirCamError(
                    f"无法打开 gpiochip{gpiochip}：{exc}"
                ) from exc
            try:
                line = chip.get_line(pin)
                flags = getattr(gpiod, "LINE_REQ_FLAG_BIAS_PULL_DOWN", 0)
                line.request(
                    consumer="aircam-pwm",
                    type=gpiod.LINE_REQ_EV_BOTH_EDGES,
                    flags=flags,
                )
            except Exception as exc:
                try:
                    chip.close()
                except Exception:
                    pass
                raise AirCamError(
                    f"无法申请 gpiochip{gpiochip} 的 BCM{pin} 输入：{exc}"
                ) from exc
            self.gpio_chip = chip
            self.gpio_line = line
            self.device = line
            self.uses_kernel_timestamps = True
        else:
            pin = int(self.config.get("bcm_pin", 17))
            self.device = self.device_factory(pin=pin, pull_up=False)
            self.device.when_activated = self._on_rising_edge
            self.device.when_deactivated = self._on_falling_edge
        self.stop_event.clear()
        monitor_target = (
            self._monitor_gpiod if self.gpio_line is not None else self._monitor
        )
        self.thread = threading.Thread(
            target=monitor_target,
            name="pwm-trigger",
            daemon=True,
        )
        self.thread.start()
        LOG.info(
            "PWM trigger enabled: BCM%s start>=%sus stop<=%sus timeout=%ss",
            pin,
            self.config.get("start_pwm", 1700),
            self.config.get("stop_pwm", 1300),
            self.config.get("timeout_seconds", 0.5),
        )

    def _record_rising_edge(self, now_ns: int) -> None:
        with self.lock:
            previous = self.previous_rise_ns
            if previous is not None:
                period_ns = now_ns - previous
                if 5_000_000 <= period_ns <= 100_000_000:
                    self.frequency_hz = round(1_000_000_000 / period_ns, 1)
            self.previous_rise_ns = now_ns
            self.rise_ns = now_ns

    def _record_falling_edge(self, now_ns: int, now: float) -> None:
        with self.lock:
            rise_ns = self.rise_ns
            self.rise_ns = None
        if rise_ns is None or now_ns <= rise_ns:
            return
        self._record_pulse((now_ns - rise_ns) / 1000.0, now)

    def _on_rising_edge(self) -> None:
        self._record_rising_edge(self.clock_ns())

    def _on_falling_edge(self) -> None:
        self._record_falling_edge(self.clock_ns(), self.clock())

    def _on_kernel_edge(self, level: int, tick: int) -> None:
        # libgpiod supplies the kernel event timestamp in nanoseconds; using it
        # avoids Python thread scheduling latency distorting the pulse width.
        if level == 1:
            self._record_rising_edge(int(tick))
        elif level == 0:
            self._record_falling_edge(int(tick), self.clock())

    def _record_pulse(self, pulse_us: float, now: float) -> None:
        minimum = int(self.config.get("min_valid_pwm", 750))
        maximum = int(self.config.get("max_valid_pwm", 2250))
        with self.lock:
            if not minimum <= pulse_us <= maximum:
                self.invalid_pulse_count += 1
                return
            self.last_pwm = round(pulse_us)
            self.last_valid_pulse = now
            self.last_pulse_seen = now
            self.pulse_count += 1
            self.last_error = None

    def _monitor(self) -> None:
        while not self.stop_event.wait(0.02):
            self._evaluate(self.clock())

    def _monitor_gpiod(self) -> None:
        try:
            import gpiod

            line = self.gpio_line
            while not self.stop_event.is_set():
                if line is not None and line.event_wait(
                    sec=0, nsec=20_000_000
                ):
                    event = line.event_read()
                    level = (
                        1
                        if event.type == gpiod.LineEvent.RISING_EDGE
                        else 0
                    )
                    self._on_kernel_edge(level, event.timestamp)
                self._evaluate(self.clock())
        except Exception as exc:
            if not self.stop_event.is_set():
                with self.lock:
                    self.last_error = f"GPIO 边沿读取失败：{exc}"
                    self.device = None
                LOG.exception("PWM GPIO event loop failed")
                self._fail_safe("gpio_error")

    def _evaluate(self, now: float) -> None:
        action: bool | None = None
        timed_out = False
        with self.lock:
            last = self.last_valid_pulse
            timeout = float(self.config.get("timeout_seconds", 0.5))
            if last is not None and now - last >= timeout:
                self.last_valid_pulse = None
                self.pending_active = None
                self.pending_since = None
                self.pending_first_pulse_count = None
                timed_out = True
            elif last is not None and self.last_pwm is not None:
                start_pwm = int(self.config.get("start_pwm", 1700))
                stop_pwm = int(self.config.get("stop_pwm", 1300))
                desired = (
                    True
                    if self.last_pwm >= start_pwm
                    else False
                    if self.last_pwm <= stop_pwm
                    else None
                )
                if desired is None or desired == self.commanded_active:
                    self.pending_active = None
                    self.pending_since = None
                    self.pending_first_pulse_count = None
                elif desired != self.pending_active:
                    self.pending_active = desired
                    self.pending_since = now
                    self.pending_first_pulse_count = self.pulse_count
                else:
                    debounce = float(
                        self.config.get("debounce_seconds", 0.15)
                    )
                    minimum_pulses = int(
                        self.config.get("min_stable_pulses", 5)
                    )
                    if (
                        self.pending_since is not None
                        and now - self.pending_since >= debounce
                        and self.pending_first_pulse_count is not None
                        and self.pulse_count - self.pending_first_pulse_count + 1
                        >= minimum_pulses
                    ):
                        action = desired

        if timed_out:
            self._fail_safe("pwm_timeout")
        elif action is not None:
            self._apply_action(action)

    def _apply_action(self, active: bool) -> None:
        try:
            if active:
                state = self.service.get_status()["state"]
                if state not in {"starting", "capturing", "recovering"}:
                    self.service.start()
                LOG.info("PWM capture start: pulse=%sus", self.last_pwm)
            else:
                self.service.stop()
                LOG.info("PWM capture stop: pulse=%sus", self.last_pwm)
        except Exception as exc:
            with self.lock:
                self.last_error = f"PWM 控制拍照失败：{exc}"
                self.pending_since = self.clock()
            LOG.exception("PWM capture action failed")
            return
        with self.lock:
            self.commanded_active = active
            self.pending_active = None
            self.pending_since = None
            self.pending_first_pulse_count = None

    def _fail_safe(self, reason: str) -> None:
        with self.lock:
            should_handle = (
                self.last_pulse_seen is not None
                or self.commanded_active is not None
            )
            self.pending_active = None
            self.pending_since = None
            self.pending_first_pulse_count = None
        if not should_handle:
            return
        if self.config.get("stop_on_timeout", True):
            try:
                self.service.stop()
                LOG.warning("PWM fail-safe stopped capture: %s", reason)
            except Exception as exc:
                with self.lock:
                    self.last_error = f"PWM 超时停止失败：{exc}"
                LOG.exception("PWM fail-safe stop failed: %s", reason)
        with self.lock:
            self.commanded_active = None

    @staticmethod
    def _age(now: float, timestamp: float | None) -> float | None:
        if timestamp is None:
            return None
        return round(max(0.0, now - timestamp), 2)

    def get_status(self, now: float | None = None) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", False))
        current = self.clock() if now is None else now
        with self.lock:
            age = self._age(current, self.last_pulse_seen)
            timeout = float(self.config.get("timeout_seconds", 0.5))
            fresh = age is not None and age < timeout
            if not enabled:
                signal_state = "disabled"
            elif self.device is None:
                signal_state = "gpio_error" if self.last_error else "connecting"
            elif fresh:
                signal_state = "ready"
            elif self.last_pulse_seen is not None:
                signal_state = "signal_timeout"
            else:
                signal_state = "waiting_signal"

            start_pwm = int(self.config.get("start_pwm", 1700))
            stop_pwm = int(self.config.get("stop_pwm", 1300))
            if self.last_pwm is None:
                switch_position = "unknown"
            elif self.last_pwm >= start_pwm:
                switch_position = "start"
            elif self.last_pwm <= stop_pwm:
                switch_position = "stop"
            else:
                switch_position = "middle"

            return {
                "enabled": enabled,
                "signal_state": signal_state,
                "gpio_connected": self.device is not None,
                "bcm_pin": int(self.config.get("bcm_pin", 17)),
                "gpiochip": int(self.config.get("gpiochip", 4)),
                "timestamp_source": (
                    "gpiod_kernel_event"
                    if self.uses_kernel_timestamps
                    else "test_callback_clock"
                ),
                "pwm": self.last_pwm,
                "frequency_hz": self.frequency_hz,
                "pulse_count": self.pulse_count,
                "invalid_pulse_count": self.invalid_pulse_count,
                "last_pulse_age_seconds": age,
                "start_pwm": start_pwm,
                "stop_pwm": stop_pwm,
                "switch_position": switch_position,
                "commanded_active": self.commanded_active,
                "last_error": self.last_error,
            }

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        if self.gpio_line is not None:
            try:
                self.gpio_line.release()
            except Exception:
                LOG.exception("failed to release PWM GPIO line")
            self.gpio_line = None
        if self.gpio_chip is not None:
            try:
                self.gpio_chip.close()
            except Exception:
                LOG.exception("failed to close PWM gpiochip")
            self.gpio_chip = None
            self.device = None
        elif self.device is not None:
            try:
                self.device.close()
            except Exception:
                LOG.exception("failed to close PWM GPIO input")
            self.device = None


def mavlink_x25_crc(data: bytes, extra: int) -> int:
    """Return the MAVLink X.25 checksum for a header/payload and CRC extra."""

    crc = 0xFFFF
    for byte in (*data, extra):
        temporary = byte ^ (crc & 0xFF)
        temporary ^= (temporary << 4) & 0xFF
        crc = (
            (crc >> 8)
            ^ (temporary << 8)
            ^ (temporary << 3)
            ^ (temporary >> 4)
        ) & 0xFFFF
    return crc


class MavlinkFrameParser:
    """Incrementally extract checksum-validated MAVLink control frames."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, int, int, bytes]]:
        self.buffer.extend(data)
        messages: list[tuple[int, int, int, bytes]] = []
        while self.buffer:
            start = next(
                (
                    index
                    for index, byte in enumerate(self.buffer)
                    if byte in (MAVLINK_V1_MAGIC, MAVLINK_V2_MAGIC)
                ),
                None,
            )
            if start is None:
                self.buffer.clear()
                break
            if start:
                del self.buffer[:start]

            magic = self.buffer[0]
            minimum_header = 6 if magic == MAVLINK_V1_MAGIC else 10
            if len(self.buffer) < minimum_header:
                break
            payload_length = self.buffer[1]
            if magic == MAVLINK_V1_MAGIC:
                message_id = self.buffer[5]
                system_id = self.buffer[3]
                component_id = self.buffer[4]
                payload_start = 6
                signature_length = 0
            else:
                message_id = int.from_bytes(self.buffer[7:10], "little")
                system_id = self.buffer[5]
                component_id = self.buffer[6]
                payload_start = 10
                signature_length = (
                    13 if self.buffer[2] & MAVLINK_V2_SIGNED_FLAG else 0
                )
            checksum_start = payload_start + payload_length
            frame_length = checksum_start + 2 + signature_length
            if len(self.buffer) < frame_length:
                break

            crc_extra = {
                MAVLINK_HEARTBEAT_ID: MAVLINK_HEARTBEAT_CRC_EXTRA,
                MAVLINK_RC_CHANNELS_ID: MAVLINK_RC_CHANNELS_CRC_EXTRA,
            }.get(message_id)
            if crc_extra is None:
                del self.buffer[:frame_length]
                continue

            received_crc = int.from_bytes(
                self.buffer[checksum_start : checksum_start + 2], "little"
            )
            calculated_crc = mavlink_x25_crc(
                bytes(self.buffer[1:checksum_start]),
                crc_extra,
            )
            if received_crc != calculated_crc:
                # Drop only the bad magic byte so a valid frame nested in a
                # corrupted length field can still be recovered.
                del self.buffer[0]
                continue

            payload = bytes(self.buffer[payload_start:checksum_start])
            messages.append((system_id, component_id, message_id, payload))
            del self.buffer[:frame_length]
        return messages


def build_mavlink2_frame(
    message_id: int,
    payload: bytes,
    crc_extra: int,
    sequence: int,
    system_id: int = 255,
    component_id: int = 190,
) -> bytes:
    """Build an unsigned MAVLink 2 frame for the direct flight-controller link."""

    header = bytes(
        [
            len(payload),
            0,
            0,
            sequence & 0xFF,
            system_id,
            component_id,
            message_id & 0xFF,
            (message_id >> 8) & 0xFF,
            (message_id >> 16) & 0xFF,
        ]
    )
    checksum = mavlink_x25_crc(header + payload, crc_extra)
    return (
        bytes([MAVLINK_V2_MAGIC])
        + header
        + payload
        + struct.pack("<H", checksum)
    )


class MavlinkTrigger:
    """Use a MAVLink RC channel as a fail-safe start/stop level control."""

    def __init__(
        self,
        service: CameraService,
        config: dict[str, Any],
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.service = service
        self.config = config
        self.serial_factory = serial_factory
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.serial_port: Any = None
        self.commanded_active: bool | None = None
        self.pending_active: bool | None = None
        self.pending_since: float | None = None
        self.last_rc_message: float | None = None
        self.last_rc_seen: float | None = None
        self.last_pwm: int | None = None
        self.last_byte_received: float | None = None
        self.last_heartbeat: float | None = None
        self.bytes_received = 0
        self.heartbeat_count = 0
        self.rc_message_count = 0
        self.last_error: str | None = None
        self.target_system: int | None = None
        self.target_component: int | None = None
        self.last_stream_request: float | None = None
        self.sequence = 0

    def start(self) -> None:
        if not self.config.get("enabled", False):
            return
        if self.serial_factory is None:
            try:
                import serial
            except ImportError as exc:
                raise AirCamError(
                    "MAVLink 已启用，但缺少 pyserial；请安装 python3-serial"
                ) from exc
            self.serial_factory = serial.Serial

        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="mavlink-trigger",
            daemon=True,
        )
        self.thread.start()
        LOG.info(
            "MAVLink trigger enabled: device=%s baud=%s RC%s",
            self.config.get("device", "/dev/serial0"),
            self.config.get("baud", 115200),
            self.config.get("rc_channel", 9),
        )

    def _run(self) -> None:
        device = str(self.config.get("device", "/dev/serial0"))
        baud = int(self.config.get("baud", 115200))
        reconnect_seconds = float(self.config.get("reconnect_seconds", 2))
        while not self.stop_event.is_set():
            parser = MavlinkFrameParser()
            try:
                assert self.serial_factory is not None
                self.serial_port = self.serial_factory(
                    device,
                    baudrate=baud,
                    timeout=0.2,
                    write_timeout=0.5,
                )
                self.last_error = None
                LOG.info("MAVLink serial connected: %s at %s baud", device, baud)
                while not self.stop_event.is_set():
                    data = self.serial_port.read(512)
                    now = time.monotonic()
                    if data:
                        self.bytes_received += len(data)
                        self.last_byte_received = now
                    for system_id, component_id, message_id, payload in parser.feed(
                        data
                    ):
                        if message_id == MAVLINK_HEARTBEAT_ID:
                            self._handle_heartbeat(
                                system_id, component_id, payload, now
                            )
                        elif message_id == MAVLINK_RC_CHANNELS_ID:
                            self._handle_rc_channels(
                                payload,
                                now,
                                system_id,
                                component_id,
                            )
                    if (
                        self.last_rc_message is None
                        and self.target_system is not None
                        and (
                            self.last_stream_request is None
                            or now - self.last_stream_request >= 5
                        )
                    ):
                        self._request_rc_stream(now)
                    self._check_timeout(now)
            except Exception:
                if not self.stop_event.is_set():
                    self.last_error = "串口读取失败，正在自动重连"
                    LOG.exception("MAVLink serial connection failed")
                    self._fail_safe("serial_error")
            finally:
                port = self.serial_port
                self.serial_port = None
                if port is not None:
                    try:
                        port.close()
                    except Exception:
                        LOG.exception("failed to close MAVLink serial port")
            if self.stop_event.wait(reconnect_seconds):
                break

    def _handle_heartbeat(
        self,
        system_id: int,
        component_id: int,
        payload: bytes,
        now: float,
    ) -> None:
        # HEARTBEAT byte 5 is MAV_AUTOPILOT. Only the ArduPilot autopilot
        # component may become the direct-link request target; routed GCS or
        # companion-computer heartbeats must not retarget COMMAND_LONG.
        if len(payload) < 9 or payload[5] != 3 or component_id != 1:
            return
        self.last_heartbeat = now
        self.heartbeat_count += 1
        if system_id != self.target_system or component_id != self.target_component:
            self.target_system = system_id
            self.target_component = component_id
            self.last_stream_request = None
            LOG.info(
                "MAVLink heartbeat received: system=%s component=%s",
                system_id,
                component_id,
            )
        if self.last_stream_request is None:
            self._request_rc_stream(now)

    def _request_rc_stream(self, now: float) -> None:
        if (
            self.serial_port is None
            or self.target_system is None
            or self.target_component is None
        ):
            return
        rate_hz = int(self.config.get("request_rate_hz", 10))
        payload = struct.pack(
            "<7fHBBB",
            float(MAVLINK_RC_CHANNELS_ID),
            1_000_000.0 / rate_hz,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            MAV_CMD_SET_MESSAGE_INTERVAL,
            self.target_system,
            self.target_component,
            0,
        )
        frame = build_mavlink2_frame(
            MAVLINK_COMMAND_LONG_ID,
            payload,
            MAVLINK_COMMAND_LONG_CRC_EXTRA,
            self.sequence,
        )
        self.serial_port.write(frame)
        self.sequence = (self.sequence + 1) & 0xFF
        self.last_stream_request = now
        LOG.info("requested RC_CHANNELS at %s Hz", rate_hz)

    def _handle_rc_channels(
        self,
        payload: bytes,
        now: float,
        system_id: int = 0,
        component_id: int = 0,
    ) -> None:
        channel = int(self.config.get("rc_channel", 9))
        # MAVLink 2 may truncate trailing zero fields. RSSI is byte 41 and may
        # therefore be omitted; byte 40 (chancount) is the last required byte.
        if len(payload) < 41 or payload[40] < channel:
            return
        if self.target_system is not None and (
            system_id != self.target_system
            or component_id != self.target_component
        ):
            return
        pwm = struct.unpack_from("<H", payload, 4 + (channel - 1) * 2)[0]
        if pwm in (0, 0xFFFF) or not 500 <= pwm <= 2500:
            return

        first_message = self.last_rc_message is None
        self.last_rc_message = now
        self.last_rc_seen = now
        self.last_pwm = pwm
        self.rc_message_count += 1
        if first_message:
            LOG.info(
                "MAVLink RC stream received: system=%s component=%s RC%s=%s",
                system_id,
                component_id,
                channel,
                pwm,
            )

        start_pwm = int(self.config.get("start_pwm", 1700))
        stop_pwm = int(self.config.get("stop_pwm", 1300))
        desired = True if pwm >= start_pwm else False if pwm <= stop_pwm else None
        if desired is None:
            self.pending_active = None
            self.pending_since = None
            return
        if desired == self.commanded_active:
            self.pending_active = None
            self.pending_since = None
            return
        if desired != self.pending_active:
            self.pending_active = desired
            self.pending_since = now
            return
        debounce = float(self.config.get("debounce_seconds", 0.15))
        if self.pending_since is None or now - self.pending_since < debounce:
            return

        try:
            if desired:
                state = self.service.get_status()["state"]
                if state not in {"starting", "capturing", "recovering"}:
                    self.service.start()
                LOG.info("MAVLink RC%s=%s: capture start", channel, pwm)
            else:
                self.service.stop()
                LOG.info("MAVLink RC%s=%s: capture stop", channel, pwm)
        except Exception:
            LOG.exception("MAVLink RC%s action failed", channel)
            return
        self.commanded_active = desired
        self.pending_active = None
        self.pending_since = None

    def _check_timeout(self, now: float) -> None:
        last = self.last_rc_message
        if last is None:
            return
        timeout = float(self.config.get("timeout_seconds", 2))
        if now - last >= timeout:
            self._fail_safe("rc_timeout")

    def _fail_safe(self, reason: str) -> None:
        if self.last_rc_message is None and self.commanded_active is None:
            return
        self.last_rc_message = None
        self.pending_active = None
        self.pending_since = None
        if self.config.get("stop_on_timeout", True):
            try:
                self.service.stop()
                LOG.warning("MAVLink fail-safe stopped capture: %s", reason)
            except Exception:
                LOG.exception("MAVLink fail-safe stop failed: %s", reason)
        self.commanded_active = None

    @staticmethod
    def _age(now: float, timestamp: float | None) -> float | None:
        if timestamp is None:
            return None
        return round(max(0.0, now - timestamp), 1)

    def get_status(self, now: float | None = None) -> dict[str, Any]:
        """Return safe live diagnostics for the web console."""

        enabled = bool(self.config.get("enabled", False))
        current = time.monotonic() if now is None else now
        serial_connected = self.serial_port is not None
        rc_age = self._age(current, self.last_rc_seen)
        timeout = float(self.config.get("timeout_seconds", 2))
        rc_fresh = rc_age is not None and rc_age < timeout

        if not enabled:
            link_state = "disabled"
        elif not serial_connected:
            link_state = "serial_error" if self.last_error else "connecting"
        elif rc_fresh:
            link_state = "ready"
        elif self.last_rc_seen is not None:
            link_state = "rc_timeout"
        elif self.last_heartbeat is not None:
            link_state = "waiting_rc"
        elif self.bytes_received:
            link_state = "invalid_data"
        else:
            link_state = "waiting_data"

        start_pwm = int(self.config.get("start_pwm", 1700))
        stop_pwm = int(self.config.get("stop_pwm", 1300))
        if self.last_pwm is None:
            switch_position = "unknown"
        elif self.last_pwm >= start_pwm:
            switch_position = "start"
        elif self.last_pwm <= stop_pwm:
            switch_position = "stop"
        else:
            switch_position = "middle"

        return {
            "enabled": enabled,
            "link_state": link_state,
            "serial_connected": serial_connected,
            "bytes_received": self.bytes_received,
            "last_byte_age_seconds": self._age(current, self.last_byte_received),
            "heartbeat_received": self.last_heartbeat is not None,
            "heartbeat_count": self.heartbeat_count,
            "heartbeat_age_seconds": self._age(current, self.last_heartbeat),
            "rc_stream_received": self.last_rc_seen is not None,
            "rc_message_count": self.rc_message_count,
            "rc_age_seconds": rc_age,
            "rc_channel": int(self.config.get("rc_channel", 9)),
            "pwm": self.last_pwm,
            "start_pwm": start_pwm,
            "stop_pwm": stop_pwm,
            "switch_position": switch_position,
            "commanded_active": self.commanded_active,
            "last_error": self.last_error,
        }

    def close(self) -> None:
        self.stop_event.set()
        port = self.serial_port
        if port is not None:
            try:
                port.close()
            except Exception:
                LOG.exception("failed to interrupt MAVLink serial port")
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)


class ChunkedWriter:
    """Write an HTTP/1.1 chunked response body to a buffered socket."""

    def __init__(self, output: Any) -> None:
        self.output = output
        self.finished = False

    def write(self, data: bytes) -> int:
        if self.finished:
            raise ValueError("cannot write after the final HTTP chunk")
        if not data:
            return 0
        self.output.write(f"{len(data):X}\r\n".encode("ascii"))
        self.output.write(data)
        self.output.write(b"\r\n")
        return len(data)

    def flush(self) -> None:
        self.output.flush()

    def finish(self) -> None:
        if self.finished:
            return
        self.output.write(b"0\r\n\r\n")
        self.output.flush()
        self.finished = True


class AirCamHandler(BaseHTTPRequestHandler):
    server_version = "AirCam/0.4.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> "AirCamHttpServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _authorized(self) -> bool:
        expected = str(self.app.config["server"].get("token", ""))
        if not expected:
            return True
        supplied = self.headers.get("X-AirCam-Token", "")
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[7:]
        return hmac.compare_digest(supplied, expected)

    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise AirCamError("无效的 Content-Length") from exc
        if size > 65536:
            raise AirCamError("请求内容过大")
        if size == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(size))
        except json.JSONDecodeError as exc:
            raise AirCamError(f"JSON 格式错误：{exc}") from exc
        if not isinstance(value, dict):
            raise AirCamError("请求内容必须是 JSON 对象")
        return value

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "令牌错误"})
        return False

    def _stream_session_zip(self, ticket: str) -> None:
        session_name = self.app.consume_download_ticket(ticket)
        session_dir: Path | None = None
        response_started = False
        transfer_finished = False
        try:
            session_dir = self.app.camera.begin_export(session_name)
            filename = f"AirCam_{session_name}.zip"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            response_started = True
            chunked_output = ChunkedWriter(self.wfile)
            with zipfile.ZipFile(
                chunked_output,
                mode="w",
                compression=zipfile.ZIP_STORED,
                allowZip64=True,
            ) as archive:
                for path in sorted(session_dir.rglob("*")):
                    if (
                        path.is_file()
                        and not path.is_symlink()
                        and not path.name.startswith("pending_")
                    ):
                        relative = path.relative_to(session_dir)
                        archive.write(
                            path,
                            arcname=f"{session_name}/{relative.as_posix()}",
                        )
            chunked_output.finish()
            transfer_finished = True
            LOG.info("download completed: session=%s", session_name)
        except (BrokenPipeError, ConnectionResetError):
            LOG.warning("download interrupted: session=%s", session_name)
        except Exception:
            if not response_started:
                raise
            LOG.exception("download failed after response started: session=%s", session_name)
        finally:
            if session_dir is not None:
                self.app.camera.finish_export()
            if response_started and not transfer_finished:
                self.close_connection = True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                data = self.app.index_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            download_match = re.fullmatch(r"/api/download/([A-Za-z0-9_-]+)", path)
            if download_match:
                self._stream_session_zip(download_match.group(1))
                return
            if not self._require_auth():
                return
            if path == "/api/status":
                status = self.app.camera.get_status()
                status["pwm"] = self.app.pwm.get_status()
                status["mavlink"] = self.app.mavlink.get_status()
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "status": status},
                )
            elif path == "/api/controls":
                state = self.app.camera.read_control_state()
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "raw": state.pop("raw"),
                        "control_state": state,
                    },
                )
            elif path == "/api/latest":
                photo = self.app.camera.latest_photo()
                if photo is None:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "当前任务还没有照片"},
                    )
                    return
                data = photo.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header(
                    "Content-Type",
                    mimetypes.guess_type(photo.name)[0] or "image/jpeg",
                )
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/sessions":
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "sessions": self.app.camera.list_sessions()},
                )
            else:
                self._json(
                    HTTPStatus.NOT_FOUND, {"ok": False, "error": "接口不存在"}
                )
        except AirCamError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception:
            LOG.exception("GET request failed")
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "服务内部错误"},
            )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._require_auth():
            return
        try:
            payload = self._read_json()
            if path == "/api/start":
                status = self.app.camera.start(
                    payload.get("interval_seconds"),
                    payload.get("duration_seconds"),
                )
                self._json(HTTPStatus.OK, {"ok": True, "status": status})
            elif path == "/api/stop":
                status = self.app.camera.stop()
                self._json(HTTPStatus.OK, {"ok": True, "status": status})
            elif path == "/api/controls":
                results = self.app.camera.set_controls(
                    payload.get("controls", {})
                )
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "controls": results,
                        "control_state": self.app.camera.control_state(),
                    },
                )
            elif path == "/api/sessions/clear":
                if payload.get("confirm_text") != CLEAR_ALL_CONFIRMATION:
                    raise AirCamError(
                        f"请输入“{CLEAR_ALL_CONFIRMATION}”确认清理全部任务"
                    )
                result = self.app.camera.clear_sessions()
                self._json(HTTPStatus.OK, {"ok": True, **result})
            else:
                session_action = re.fullmatch(
                    r"/api/sessions/([^/]+)/(download-ticket|delete)", path
                )
                if not session_action:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "接口不存在"},
                    )
                    return
                session_name, action = session_action.groups()
                if action == "download-ticket":
                    ticket = self.app.issue_download_ticket(session_name)
                    self._json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "download_url": f"/api/download/{ticket}",
                            "expires_in_seconds": DOWNLOAD_TICKET_TTL_SECONDS,
                        },
                    )
                else:
                    if payload.get("confirm_session") != session_name:
                        raise AirCamError("确认的任务编号不匹配")
                    result = self.app.camera.delete_session(session_name)
                    self._json(HTTPStatus.OK, {"ok": True, **result})
        except AirCamError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception:
            LOG.exception("POST request failed")
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "服务内部错误"},
            )


class AirCamHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(
                socket.IPPROTO_IPV6,
                socket.IPV6_V6ONLY,
                0,
            )
        super().server_bind()

    def __init__(
        self,
        address: tuple[str, int],
        camera: CameraService,
        config: dict[str, Any],
        index_path: Path,
        pwm: PwmTrigger | None = None,
        mavlink: MavlinkTrigger | None = None,
    ) -> None:
        self.camera = camera
        self.config = config
        self.index_path = index_path
        self.pwm = pwm or PwmTrigger(
            camera,
            config.get("pwm", {}),
        )
        self.mavlink = mavlink or MavlinkTrigger(
            camera,
            config.get("mavlink", {}),
        )
        self.download_ticket_lock = threading.Lock()
        self.download_tickets: dict[str, tuple[float, str]] = {}
        bind_address = address
        if address[0] == "0.0.0.0" and socket.has_dualstack_ipv6():
            self.address_family = socket.AF_INET6
            bind_address = ("::", address[1])
        super().__init__(bind_address, AirCamHandler)

    def issue_download_ticket(self, session_name: str) -> str:
        self.camera.validate_export_session(session_name)
        ticket = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self.download_ticket_lock:
            self.download_tickets = {
                value: details
                for value, details in self.download_tickets.items()
                if details[0] > now
            }
            self.download_tickets[ticket] = (
                now + DOWNLOAD_TICKET_TTL_SECONDS,
                session_name,
            )
        return ticket

    def consume_download_ticket(self, ticket: str) -> str:
        with self.download_ticket_lock:
            details = self.download_tickets.pop(ticket, None)
        if details is None or details[0] <= time.monotonic():
            raise AirCamError("下载链接无效或已经过期，请重新点击导入电脑")
        return details[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AirCam USB camera service")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/home/pi/AirCam/config/config.json"),
    )
    parser.add_argument(
        "--web", type=Path, default=Path("/home/pi/AirCam/web/index.html")
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and exit",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    try:
        config = load_config(args.config)
        if args.check_config:
            CameraService(config)
            print("configuration OK")
            return 0

        camera = CameraService(config)
        camera.initialize_controls()
        gpio = GpioTrigger(camera, config.get("gpio", {}))
        gpio.start()
        pwm = PwmTrigger(camera, config.get("pwm", {}))
        pwm.start()
        mavlink = MavlinkTrigger(camera, config.get("mavlink", {}))
        mavlink.start()
        host = str(config["server"].get("host", "0.0.0.0"))
        port = int(config["server"].get("port", 8080))
        server = AirCamHttpServer(
            (host, port),
            camera,
            config,
            args.web,
            pwm=pwm,
            mavlink=mavlink,
        )

        stopped = threading.Event()

        def request_shutdown(_signum: int, _frame: Any) -> None:
            if not stopped.is_set():
                stopped.set()
                threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
        LOG.info("AirCam listening on http://%s:%s", host, port)
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
            mavlink.close()
            pwm.close()
            gpio.close()
            camera.shutdown()
        return 0
    except AirCamError as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
