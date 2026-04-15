#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import errno
import mmap
import os
import re
import select
import subprocess
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np


libc = ctypes.CDLL(None, use_errno=True)

V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE = 9
V4L2_MEMORY_MMAP = 1
V4L2_FIELD_NONE = 1

_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_DIRBITS = 2

_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, ioctl_type: int, number: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (ioctl_type << _IOC_TYPESHIFT)
        | (number << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def _iorw(number: int, struct_type) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, ord("V"), number, ctypes.sizeof(struct_type))


def _iow(number: int, struct_type) -> int:
    return _ioc(_IOC_WRITE, ord("V"), number, ctypes.sizeof(struct_type))


class TimeVal(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_long),
        ("tv_usec", ctypes.c_long),
    ]


class V4L2TimeCode(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("frames", ctypes.c_uint8),
        ("seconds", ctypes.c_uint8),
        ("minutes", ctypes.c_uint8),
        ("hours", ctypes.c_uint8),
        ("userbits", ctypes.c_uint8 * 4),
    ]


class V4L2PlaneMem(ctypes.Union):
    _fields_ = [
        ("mem_offset", ctypes.c_uint32),
        ("userptr", ctypes.c_ulong),
        ("fd", ctypes.c_int32),
    ]


class V4L2Plane(ctypes.Structure):
    _fields_ = [
        ("bytesused", ctypes.c_uint32),
        ("length", ctypes.c_uint32),
        ("m", V4L2PlaneMem),
        ("data_offset", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 11),
    ]


class V4L2BufferMem(ctypes.Union):
    _fields_ = [
        ("offset", ctypes.c_uint32),
        ("userptr", ctypes.c_ulong),
        ("planes", ctypes.c_void_p),
        ("fd", ctypes.c_int32),
    ]


class V4L2Buffer(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("bytesused", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("timestamp", TimeVal),
        ("timecode", V4L2TimeCode),
        ("sequence", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("m", V4L2BufferMem),
        ("length", ctypes.c_uint32),
        ("reserved2", ctypes.c_uint32),
        ("request_fd", ctypes.c_int32),
    ]


class V4L2RequestBuffers(ctypes.Structure):
    _fields_ = [
        ("count", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("flags", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8 * 3),
    ]


VIDIOC_REQBUFS = _iorw(8, V4L2RequestBuffers)
VIDIOC_QUERYBUF = _iorw(9, V4L2Buffer)
VIDIOC_QBUF = _iorw(15, V4L2Buffer)
VIDIOC_DQBUF = _iorw(17, V4L2Buffer)
VIDIOC_STREAMON = _iow(18, ctypes.c_int)
VIDIOC_STREAMOFF = _iow(19, ctypes.c_int)


def ioctl_call(fd: int, request: int, arg) -> int:
    ret = libc.ioctl(fd, request, arg)
    if ret < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return ret


@dataclass
class FramePacket:
    frame: object | None = None
    frame_id: int = 0
    capture_time: float = 0.0
    source_shape: tuple[int, ...] | None = None
    crop_rect: tuple[int, int, int, int] | None = None
    capture_read_ms: float = 0.0
    capture_crop_ms: float = 0.0
    capture_resize_ms: float = 0.0


@dataclass
class CropResult:
    frame: object
    x1: int
    y1: int
    x2: int
    y2: int


@dataclass
class VideoDeviceInfo:
    driver_name: str = ""
    card_type: str = ""
    bus_info: str = ""
    pixel_format_fourcc: str = ""
    pixel_format: str = ""
    current_width: int = 0
    current_height: int = 0
    active_width: int = 0
    active_height: int = 0
    active_fps: int = 0
    size_image: int = 0


def run_text_command(args: list[str]) -> str:
    completed = subprocess.run(args, check=False, capture_output=True, text=True)
    return completed.stdout


def _extract_first_int(pattern: str, text: str) -> int:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except Exception:
        return 0


def _extract_first_float_as_int(pattern: str, text: str) -> int:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return 0
    try:
        return int(round(float(match.group(1))))
    except Exception:
        return 0


def _extract_first_text(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return ""
    return str(match.group(1)).strip()


def query_device_info(device: str) -> VideoDeviceInfo:
    all_output = run_text_command(["v4l2-ctl", "-d", device, "--all"])
    timings_output = run_text_command(["v4l2-ctl", "-d", device, "--get-dv-timings"])
    return VideoDeviceInfo(
        driver_name=_extract_first_text(r"Driver name\s*:\s*(.+)", all_output),
        card_type=_extract_first_text(r"Card type\s*:\s*(.+)", all_output),
        bus_info=_extract_first_text(r"Bus info\s*:\s*(.+)", all_output),
        pixel_format_fourcc=_extract_first_text(r"Pixel Format\s*:\s*'([^']+)'", all_output),
        pixel_format=_extract_first_text(r"Pixel Format\s*:\s*'[^']*'\s*\(([^)]+)\)", all_output),
        current_width=_extract_first_int(r"Width/Height\s*:\s*(\d+)\s*/\s*\d+", all_output),
        current_height=_extract_first_int(r"Width/Height\s*:\s*\d+\s*/\s*(\d+)", all_output),
        active_width=_extract_first_int(r"Active width:\s*(\d+)", timings_output),
        active_height=_extract_first_int(r"Active height:\s*(\d+)", timings_output),
        active_fps=_extract_first_float_as_int(r"\((\d+(?:\.\d+)?)\s+frames per second\)", timings_output),
        size_image=_extract_first_int(r"Size Image\s*:\s*(\d+)", all_output),
    )


def list_video_devices() -> list[str]:
    output = run_text_command(["v4l2-ctl", "--list-devices"])
    devices: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("/dev/video"):
            devices.append(line)
    return devices


def score_device(device: str) -> int:
    score = 0
    all_output = run_text_command(["v4l2-ctl", "-d", device, "--all"])
    lower = all_output.lower()
    if "hdmi" in lower:
        score += 5
    if "rx" in lower or "hdmirx" in lower:
        score += 5
    if "dv timings" in lower:
        score += 3

    formats = run_text_command(["v4l2-ctl", "-d", device, "--list-formats-ext"])
    if formats:
        score += 1

    return score


def auto_detect_device() -> str:
    devices = list_video_devices()
    if not devices:
        raise RuntimeError("No /dev/video* devices found")

    scored = sorted(((score_device(device), device) for device in devices), reverse=True)
    best_score, best_device = scored[0]
    if best_score <= 0:
        return devices[0]
    return best_device


def crop_center(frame, crop_width: int, crop_height: int) -> CropResult:
    if crop_width <= 0 or crop_height <= 0:
        height, width = frame.shape[:2]
        return CropResult(frame=frame, x1=0, y1=0, x2=width, y2=height)

    height, width = frame.shape[:2]
    crop_width = min(crop_width, width)
    crop_height = min(crop_height, height)
    x1 = max((width - crop_width) // 2, 0)
    y1 = max((height - crop_height) // 2, 0)
    x2 = min(x1 + crop_width, width)
    y2 = min(y1 + crop_height, height)
    return CropResult(frame=frame[y1:y2, x1:x2], x1=x1, y1=y1, x2=x2, y2=y2)


def resize_for_processing(frame, width: int, height: int):
    if width <= 0 or height <= 0:
        return frame
    current_height, current_width = frame.shape[:2]
    if current_width == width and current_height == height:
        return frame
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)


class LatestFrameCapture:
    def __init__(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        backend: str,
        crop_width: int,
        crop_height: int,
        process_width: int,
        process_height: int,
        use_capture_thread: bool = True,
    ) -> None:
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.backend = backend
        self.crop_width = crop_width
        self.crop_height = crop_height
        self.process_width = process_width
        self.process_height = process_height
        self.use_capture_thread = bool(use_capture_thread)
        self.device_info = query_device_info(device)
        self.pipeline = ""
        self.cap: cv2.VideoCapture | None = None
        self.v4l2_fd: int | None = None
        self.v4l2_maps: list[mmap.mmap] = []
        self.v4l2_plane_count = 1
        self.stream_frame_width = 0
        self.stream_frame_height = 0
        self.stream_frame_size = 0
        self.prefetched_packet: FramePacket | None = None
        self.lock = threading.Lock()
        self.packet = FramePacket()
        self.running = False
        self.thread: threading.Thread | None = None
        self.read_failures = 0
        self.reopen_attempts = 0
        self.last_open_error = ""
        self.active_backend = ""
        self.last_consumed_frame_id = 0
        self.raw_buffer_count = 2

    def resolve_source_caps(self) -> tuple[int, int, int]:
        width = int(self.width)
        height = int(self.height)
        fps = int(self.fps)

        if "rk_hdmirx" in self.device_info.driver_name.lower():
            source_width = int(self.device_info.active_width or self.device_info.current_width or width)
            source_height = int(self.device_info.active_height or self.device_info.current_height or height)
            source_fps = int(self.device_info.active_fps or fps)
            return max(1, source_width), max(1, source_height), max(1, source_fps)

        return max(1, width), max(1, height), max(1, fps)

    def can_use_raw_v4l2_stream(self) -> bool:
        if "rk_hdmirx" not in self.device_info.driver_name.lower():
            return False
        if str(self.device_info.pixel_format_fourcc).strip().upper() != "BGR3":
            return False
        width, height, _fps = self.resolve_source_caps()
        if width <= 0 or height <= 0:
            return False
        return True

    def build_gstreamer_pipelines(self) -> list[tuple[str, str]]:
        source_width, source_height, source_fps = self.resolve_source_caps()
        source_caps = f"video/x-raw,width={source_width},height={source_height},framerate={source_fps}/1"
        appsink = "appsink drop=true max-buffers=1 sync=false wait-on-eos=false"
        queue = "queue leaky=downstream max-size-buffers=1 max-size-bytes=0 max-size-time=0"
        base = f"v4l2src device={self.device} io-mode=mmap do-timestamp=true ! {source_caps}"
        pipelines: list[tuple[str, str]] = []

        if "rk_hdmirx" in self.device_info.driver_name.lower():
            pipelines.append(
                (
                    "gstreamer-rk-hdmirx-bgr",
                    f"{base},format=BGR ! {queue} ! {appsink}",
                )
            )
            pipelines.append(
                (
                    "gstreamer-rk-hdmirx-convert",
                    f"{base} ! videoconvert n-threads=1 ! video/x-raw,format=BGR ! {queue} ! {appsink}",
                )
            )

        pipelines.append(
            (
                "gstreamer-generic-convert",
                f"{base} ! videoconvert n-threads=1 ! video/x-raw,format=BGR ! {queue} ! {appsink}",
            )
        )
        return pipelines

    def open_v4l2_raw(self) -> bool:
        if not self.can_use_raw_v4l2_stream():
            self.last_open_error = (
                f"Raw V4L2 stream is unsupported for device {self.device}: "
                f"driver={self.device_info.driver_name or 'unknown'} "
                f"fourcc={self.device_info.pixel_format_fourcc or 'unknown'}"
            )
            return False

        width, height, _fps = self.resolve_source_caps()
        frame_size = int(self.device_info.size_image or (width * height * 3))
        if frame_size <= 0:
            self.last_open_error = f"Invalid raw frame size for device {self.device}"
            return False

        try:
            fd = os.open(self.device, os.O_RDWR | os.O_NONBLOCK)
        except Exception as exc:
            self.last_open_error = f"Failed to open raw V4L2 device: {exc}"
            return False

        self.v4l2_fd = fd
        self.stream_frame_width = int(width)
        self.stream_frame_height = int(height)
        self.stream_frame_size = int(frame_size)
        self.prefetched_packet = None
        try:
            req = V4L2RequestBuffers()
            req.count = int(self.raw_buffer_count)
            req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE
            req.memory = V4L2_MEMORY_MMAP
            ioctl_call(fd, VIDIOC_REQBUFS, ctypes.byref(req))
            if int(req.count) <= 0:
                raise RuntimeError("VIDIOC_REQBUFS returned zero buffers")

            self.v4l2_maps = []
            for index in range(int(req.count)):
                planes = (V4L2Plane * self.v4l2_plane_count)()
                buf = V4L2Buffer()
                buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE
                buf.memory = V4L2_MEMORY_MMAP
                buf.index = int(index)
                buf.length = self.v4l2_plane_count
                buf.m.planes = ctypes.cast(planes, ctypes.c_void_p).value
                ioctl_call(fd, VIDIOC_QUERYBUF, ctypes.byref(buf))

                plane = planes[0]
                length = int(plane.length or self.stream_frame_size)
                offset = int(plane.m.mem_offset)
                mapped = mmap.mmap(
                    fd,
                    length,
                    flags=mmap.MAP_SHARED,
                    prot=mmap.PROT_READ | mmap.PROT_WRITE,
                    offset=offset,
                )
                self.v4l2_maps.append(mapped)

                q_planes = (V4L2Plane * self.v4l2_plane_count)()
                q_planes[0].length = plane.length
                qbuf = V4L2Buffer()
                qbuf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE
                qbuf.memory = V4L2_MEMORY_MMAP
                qbuf.index = int(index)
                qbuf.length = self.v4l2_plane_count
                qbuf.m.planes = ctypes.cast(q_planes, ctypes.c_void_p).value
                ioctl_call(fd, VIDIOC_QBUF, ctypes.byref(qbuf))

            buf_type = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)
            ioctl_call(fd, VIDIOC_STREAMON, ctypes.byref(buf_type))

            self.prefetched_packet = self._read_frame_packet_from_v4l2_mmap(timeout_seconds=1.5)
            if self.prefetched_packet is None:
                raise RuntimeError(
                    f"Raw V4L2 mmap stream opened but produced no frame within timeout: "
                    f"{self.device} ({self.device_info.pixel_format_fourcc} {width}x{height})"
                )

            self.active_backend = "v4l2-raw"
            self.last_open_error = ""
            return True
        except Exception as exc:
            self.last_open_error = str(exc)
            self._close_v4l2_mmap()
            return False

    def open_gstreamer(self) -> bool:
        errors: list[str] = []
        for backend_name, pipeline in self.build_gstreamer_pipelines():
            cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            if not cap.isOpened():
                errors.append(f"{backend_name}: {pipeline}")
                continue
            self.pipeline = pipeline
            self.cap = cap
            self.active_backend = backend_name
            self.last_open_error = ""
            return True
        self.last_open_error = "Failed to open capture pipeline(s): " + "; ".join(errors)
        return False

    def open_v4l2(self) -> bool:
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            self.last_open_error = f"Failed to open V4L2 device: {self.device}"
            return False
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        self.cap = cap
        self.active_backend = "v4l2"
        self.last_open_error = ""
        return True

    def open(self) -> bool:
        if self.backend == "v4l2-raw":
            return self.open_v4l2_raw()
        if self.backend == "gstreamer":
            return self.open_gstreamer()
        if self.backend == "v4l2":
            return self.open_v4l2()

        if self.open_v4l2_raw():
            return True
        raw_error = self.last_open_error
        if self.open_gstreamer():
            if raw_error:
                print(f"Falling back to GStreamer backend after raw V4L2 open failed: {raw_error}")
            return True
        gst_error = self.last_open_error
        if self.open_v4l2():
            print(f"Falling back to OpenCV V4L2 backend after GStreamer open failed: {gst_error}")
            return True
        self.last_open_error = f"{raw_error}; {gst_error}; {self.last_open_error}"
        return False

    def start(self) -> "LatestFrameCapture":
        if not self.open():
            raise RuntimeError(self.last_open_error)
        self.running = True
        if self.use_capture_thread:
            self.thread = threading.Thread(target=self._capture_loop, daemon=True)
            self.thread.start()
        return self

    def _reopen(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self._close_v4l2_mmap()
        self.reopen_attempts += 1
        time.sleep(0.3)
        self.open()

    def _capture_loop(self) -> None:
        while self.running:
            packet = self._read_frame_packet()
            if packet is None:
                continue
            with self.lock:
                self.packet = packet

    def _read_frame_packet(self) -> FramePacket | None:
        if self.prefetched_packet is not None:
            packet = self.prefetched_packet
            self.prefetched_packet = None
            return packet
        if self.v4l2_fd is not None:
            return self._read_frame_packet_from_v4l2_mmap()
        if self.cap is None:
            self._reopen()
            if self.cap is None:
                return None

        read_started = time.perf_counter()
        ok, frame = self.cap.read()
        capture_read_ms = (time.perf_counter() - read_started) * 1000.0
        if not ok:
            self.read_failures += 1
            self._reopen()
            return None

        crop_started = time.perf_counter()
        cropped = crop_center(frame, self.crop_width, self.crop_height)
        capture_crop_ms = (time.perf_counter() - crop_started) * 1000.0

        resize_started = time.perf_counter()
        working_frame = resize_for_processing(cropped.frame, self.process_width, self.process_height)
        capture_resize_ms = (time.perf_counter() - resize_started) * 1000.0

        next_frame_id = int(self.packet.frame_id) + 1
        return FramePacket(
            frame=working_frame,
            frame_id=next_frame_id,
            capture_time=time.perf_counter(),
            source_shape=frame.shape,
            crop_rect=(cropped.x1, cropped.y1, cropped.x2, cropped.y2),
            capture_read_ms=capture_read_ms,
            capture_crop_ms=capture_crop_ms,
            capture_resize_ms=capture_resize_ms,
        )

    def _queue_v4l2_buffer(self, index: int, plane_length: int) -> None:
        if self.v4l2_fd is None:
            return
        planes = (V4L2Plane * self.v4l2_plane_count)()
        planes[0].length = int(plane_length)
        buf = V4L2Buffer()
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE
        buf.memory = V4L2_MEMORY_MMAP
        buf.index = int(index)
        buf.length = self.v4l2_plane_count
        buf.m.planes = ctypes.cast(planes, ctypes.c_void_p).value
        ioctl_call(self.v4l2_fd, VIDIOC_QBUF, ctypes.byref(buf))

    def _read_frame_packet_from_v4l2_mmap(self, timeout_seconds: float | None = None) -> FramePacket | None:
        fd = self.v4l2_fd
        if fd is None:
            return None

        wait_seconds = None if timeout_seconds is None else float(timeout_seconds)
        ready, _write_ready, _err_ready = select.select([fd], [], [], wait_seconds)
        if not ready:
            return None

        planes = (V4L2Plane * self.v4l2_plane_count)()
        buf = V4L2Buffer()
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE
        buf.memory = V4L2_MEMORY_MMAP
        buf.length = self.v4l2_plane_count
        buf.m.planes = ctypes.cast(planes, ctypes.c_void_p).value

        try:
            read_started = time.perf_counter()
            ioctl_call(fd, VIDIOC_DQBUF, ctypes.byref(buf))
            capture_read_ms = (time.perf_counter() - read_started) * 1000.0
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EINTR):
                return None
            self.read_failures += 1
            self._reopen()
            return None

        index = int(buf.index)
        if index < 0 or index >= len(self.v4l2_maps):
            self.read_failures += 1
            self._reopen()
            return None

        plane = planes[0]
        bytesused = int(plane.bytesused or self.stream_frame_size)
        data_offset = int(plane.data_offset or 0)
        plane_length = int(plane.length or self.stream_frame_size)
        if bytesused <= 0:
            bytesused = self.stream_frame_size

        try:
            mapped = self.v4l2_maps[index]
            expected_bytes = int(self.stream_frame_height * self.stream_frame_width * 3)
            if bytesused < expected_bytes:
                raise ValueError(
                    f"raw frame too small: bytesused={bytesused} expected={expected_bytes}"
                )
            frame = np.ndarray(
                shape=(self.stream_frame_height, self.stream_frame_width, 3),
                dtype=np.uint8,
                buffer=mapped,
                offset=data_offset,
            )
        except Exception:
            try:
                self._queue_v4l2_buffer(index, plane_length)
            except Exception:
                pass
            self.read_failures += 1
            self._reopen()
            return None

        crop_started = time.perf_counter()
        cropped = crop_center(frame, self.crop_width, self.crop_height)
        capture_crop_ms = (time.perf_counter() - crop_started) * 1000.0

        resize_started = time.perf_counter()
        if self.process_width > 0 and self.process_height > 0:
            working_frame = resize_for_processing(cropped.frame, self.process_width, self.process_height)
        else:
            working_frame = cropped.frame
        # Raw V4L2 mmap buffers are returned to the driver immediately after dequeue.
        # Copy the frame into owned memory first, otherwise later hardware writes can
        # mutate the image while inference/render still use it and produce box/frame mismatch.
        working_frame = np.ascontiguousarray(working_frame).copy()
        capture_resize_ms = (time.perf_counter() - resize_started) * 1000.0

        try:
            self._queue_v4l2_buffer(index, plane_length)
        except Exception:
            self.read_failures += 1
            self._reopen()
            return None

        next_frame_id = int(self.packet.frame_id) + 1
        return FramePacket(
            frame=working_frame,
            frame_id=next_frame_id,
            capture_time=time.perf_counter(),
            source_shape=frame.shape,
            crop_rect=(cropped.x1, cropped.y1, cropped.x2, cropped.y2),
            capture_read_ms=capture_read_ms,
            capture_crop_ms=capture_crop_ms,
            capture_resize_ms=capture_resize_ms,
        )

    def _close_v4l2_mmap(self) -> None:
        if self.v4l2_fd is not None:
            try:
                buf_type = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)
                ioctl_call(self.v4l2_fd, VIDIOC_STREAMOFF, ctypes.byref(buf_type))
            except Exception:
                pass
        for mapped in self.v4l2_maps:
            try:
                mapped.close()
            except Exception:
                pass
        self.v4l2_maps = []
        if self.v4l2_fd is not None:
            try:
                os.close(self.v4l2_fd)
            except Exception:
                pass
            self.v4l2_fd = None
        self.prefetched_packet = None

    def read_latest(self) -> tuple[bool, object | None, int, float, tuple[int, ...] | None, tuple[int, int, int, int] | None, float, float, float]:
        if not self.use_capture_thread:
            packet = self._read_frame_packet()
            if packet is None:
                return False, None, self.packet.frame_id, self.packet.capture_time, self.packet.source_shape, self.packet.crop_rect, 0.0, 0.0, 0.0
            self.packet = packet
            return (
                True,
                packet.frame,
                packet.frame_id,
                packet.capture_time,
                packet.source_shape,
                packet.crop_rect,
                packet.capture_read_ms,
                packet.capture_crop_ms,
                packet.capture_resize_ms,
            )
        with self.lock:
            if self.packet.frame is None:
                return False, None, 0, 0.0, None, None, 0.0, 0.0, 0.0
            return (
                True,
                self.packet.frame,
                self.packet.frame_id,
                self.packet.capture_time,
                self.packet.source_shape,
                self.packet.crop_rect,
                self.packet.capture_read_ms,
                self.packet.capture_crop_ms,
                self.packet.capture_resize_ms,
            )

    def consume_latest(self) -> tuple[bool, object | None, int, float, tuple[int, ...] | None, tuple[int, int, int, int] | None, float, float, float]:
        if not self.use_capture_thread:
            return self.read_latest()
        with self.lock:
            if self.packet.frame is None:
                return False, None, 0, 0.0, None, None, 0.0, 0.0, 0.0
            if self.packet.frame_id == self.last_consumed_frame_id:
                return False, None, self.packet.frame_id, self.packet.capture_time, self.packet.source_shape, self.packet.crop_rect, self.packet.capture_read_ms, self.packet.capture_crop_ms, self.packet.capture_resize_ms
            self.last_consumed_frame_id = self.packet.frame_id
            return (
                True,
                self.packet.frame,
                self.packet.frame_id,
                self.packet.capture_time,
                self.packet.source_shape,
                self.packet.crop_rect,
                self.packet.capture_read_ms,
                self.packet.capture_crop_ms,
                self.packet.capture_resize_ms,
            )

    def stop(self) -> None:
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=1.0)
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self._close_v4l2_mmap()


def create_capture_from_args(args: argparse.Namespace) -> LatestFrameCapture:
    device = auto_detect_device() if args.device == "auto" else args.device
    return LatestFrameCapture(
        device,
        args.width,
        args.height,
        args.fps,
        args.backend,
        args.crop_width,
        args.crop_height,
        args.process_width,
        args.process_height,
    )


def process_frame(frame, mode: str):
    if mode == "original":
        return frame
    if mode == "gray":
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if mode == "edges":
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 80, 160)
        return cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR)
    raise ValueError(f"unsupported mode: {mode}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Low-latency HDMI-in capture for OrangePi 5 Plus.")
    parser.add_argument("--device", default="auto", help="V4L2 device path, e.g. /dev/video0, or auto")
    parser.add_argument("--width", type=int, default=1920, help="Requested capture width")
    parser.add_argument("--height", type=int, default=1080, help="Requested capture height")
    parser.add_argument("--fps", type=int, default=60, help="Requested capture FPS")
    parser.add_argument("--backend", choices=("auto", "gstreamer", "v4l2", "v4l2-raw"), default="v4l2-raw", help="Capture backend preference")
    parser.add_argument("--show", action="store_true", help="Show preview window")
    parser.add_argument("--mode", choices=("original", "gray", "edges"), default="original", help="Preview/processing mode")
    parser.add_argument("--crop-width", type=int, default=500, help="Center crop width before processing, 0 disables crop")
    parser.add_argument("--crop-height", type=int, default=500, help="Center crop height before processing, 0 disables crop")
    parser.add_argument("--process-width", type=int, default=0, help="Resize cropped frame to this width before processing, 0 keeps crop size")
    parser.add_argument("--process-height", type=int, default=0, help="Resize cropped frame to this height before processing, 0 keeps crop size")
    parser.add_argument("--print-every", type=float, default=1.0, help="Stats print interval in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    device = auto_detect_device() if args.device == "auto" else args.device
    print(f"Using device: {device}")

    show_enabled = bool(args.show)
    if show_enabled and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("Preview disabled: no DISPLAY/WAYLAND_DISPLAY in current session.")
        show_enabled = False

    capture = create_capture_from_args(args).start()
    print(f"Backend: {capture.active_backend}")
    if capture.active_backend.startswith("gstreamer"):
        print(f"Pipeline: {capture.pipeline}")
    last_frame_id = -1
    processed_frames = 0
    last_stat_at = time.perf_counter()

    try:
        while True:
            ok, frame, frame_id, capture_time, source_shape, crop_rect, capture_read_ms, capture_crop_ms, capture_resize_ms = capture.read_latest()
            if not ok:
                time.sleep(0.001)
                continue

            if frame_id == last_frame_id:
                time.sleep(0.001)
                continue

            last_frame_id = frame_id
            processed = process_frame(frame, args.mode)
            processed_frames += 1
            now = time.perf_counter()
            latency_ms = (now - capture_time) * 1000.0

            if now - last_stat_at >= args.print_every:
                crop_text = "crop_rect=unknown"
                if crop_rect is not None:
                    crop_text = f"crop_rect=({crop_rect[0]},{crop_rect[1]})-({crop_rect[2]},{crop_rect[3]})"
                print(
                    f"processed_fps={processed_frames / (now - last_stat_at):.2f} "
                    f"latency_ms={latency_ms:.1f} "
                    f"read_failures={capture.read_failures} "
                    f"reopen_attempts={capture.reopen_attempts} "
                    f"capture_read_ms={capture_read_ms:.1f} "
                    f"capture_crop_ms={capture_crop_ms:.1f} "
                    f"capture_resize_ms={capture_resize_ms:.1f} "
                    f"capture_shape={source_shape} "
                    f"{crop_text} "
                    f"process_shape={frame.shape}"
                )
                processed_frames = 0
                last_stat_at = now

            if show_enabled:
                try:
                    cv2.imshow("hdmi_in_processed", processed)
                    if cv2.waitKey(1) & 0xFF == 27:
                        break
                except cv2.error as exc:
                    print(f"Preview disabled after OpenCV highgui init failed: {exc}")
                    show_enabled = False

    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()
        if show_enabled:
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
