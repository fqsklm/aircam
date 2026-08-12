#!/usr/bin/env python3
"""AirCam: a small, dependency-free control service for a V4L2 USB camera.

The service delegates camera capture and controls to ffmpeg and v4l2-ctl.  It
provides an authenticated HTTP API, a small web UI, and an optional GPIO
start/stop input.
"""

from __future__ import annotations

import argparse
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
        self.active_controls: dict[str, Any] = dict(
            config["camera"].get("controls", {})
        )
        self.control_history: list[tuple[float, dict[str, Any]]] = []
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

    def list_controls(self) -> str:
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
        return result.stdout

    def set_controls(self, controls: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(controls, dict) or not controls:
            raise AirCamError("controls 必须是非空对象")

        with self.lock:
            results: list[dict[str, Any]] = []
            successful: dict[str, int] = {}
            for name, raw_value in controls.items():
                if not CONTROL_NAME.fullmatch(name):
                    raise AirCamError(f"非法控制项名称：{name}")
                if isinstance(raw_value, bool):
                    value = int(raw_value)
                elif isinstance(raw_value, int):
                    value = raw_value
                else:
                    raise AirCamError(f"{name} 的值必须是整数或布尔值")

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
                    item["error"] = (
                        result.stderr or result.stdout or "设置失败"
                    ).strip()
                else:
                    successful[name] = value
                results.append(item)

            if successful:
                self.active_controls.update(successful)
                self._record_control_event(time.time())

            failures = [item for item in results if not item["ok"]]
            if failures:
                messages = "; ".join(
                    f"{item['name']}: {item.get('error', '设置失败')}"
                    for item in failures
                )
                raise AirCamError(messages)
            return results

    def _record_control_event(self, timestamp: float) -> None:
        snapshot = dict(self.active_controls)
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
            return dict(self.active_controls)

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
        candidates = sorted(
            [*session_dir.glob("pending_*.jpg"), *session_dir.glob("photo_*.jpg")]
        )
        for photo in candidates:
            if photo.name in self.manifested_photos:
                continue
            try:
                stat = photo.stat()
            except FileNotFoundError:
                continue
            # ffmpeg can leave a zero-byte placeholder when capture is
            # interrupted after opening the next JPEG but before writing it.
            # This is not a completed photograph.
            if stat.st_size == 0:
                continue
            # Avoid recording a file while ffmpeg may still be writing it.
            if not include_recent and now - stat.st_mtime < 0.25:
                continue

            pending_match = PENDING_PHOTO.fullmatch(photo.name)
            if pending_match:
                with self.lock:
                    capture_unix_us = self.frame_timestamps_us.pop(
                        photo.name, None
                    )
                if capture_unix_us is None and not include_recent:
                    continue
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

        if rows:
            with (session_dir / "manifest.csv").open(
                "a", encoding="utf-8", newline=""
            ) as handle:
                writer = csv.writer(handle)
                writer.writerows(rows)
                handle.flush()
                os.fsync(handle.fileno())

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
            session_dir = (
                self.data_dir / self.status.session
                if self.status.session
                else None
            )
            status["photo_count"] = (
                sum(1 for _ in session_dir.glob("photo_*.jpg"))
                if session_dir and session_dir.is_dir()
                else 0
            )
            disk = shutil.disk_usage(self.data_dir)
            status["disk"] = {
                "free_bytes": disk.free,
                "total_bytes": disk.total,
            }
            status["device"] = self.device
            status["active_controls"] = dict(self.active_controls)
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
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "status": self.app.camera.get_status()},
                )
            elif path == "/api/controls":
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "raw": self.app.camera.list_controls()},
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
                self._json(HTTPStatus.OK, {"ok": True, "controls": results})
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
    ) -> None:
        self.camera = camera
        self.config = config
        self.index_path = index_path
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
        "--config", type=Path, default=Path("/etc/aircam/config.json")
    )
    parser.add_argument(
        "--web", type=Path, default=Path("/opt/aircam/web/index.html")
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
        gpio = GpioTrigger(camera, config.get("gpio", {}))
        gpio.start()
        host = str(config["server"].get("host", "0.0.0.0"))
        port = int(config["server"].get("port", 8080))
        server = AirCamHttpServer((host, port), camera, config, args.web)

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
            gpio.close()
            camera.shutdown()
        return 0
    except AirCamError as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
