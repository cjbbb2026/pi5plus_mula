from __future__ import annotations

import json
import math
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from control_client import PassthroughController


@dataclass
class SimpleAimState:
    last_target_center: tuple[float, float] | None = None
    last_target_class: int | None = None
    last_frame_id: int = 0
    last_target_ts: float | None = None
    stable_frames: int = 0
    lock_settle_remaining: int = 0
    candidate_target_center: tuple[float, float] | None = None
    candidate_target_class: int | None = None
    candidate_target_ts: float | None = None
    candidate_stable_frames: int = 0
    last_auto_fire_ts: float | None = None

    def reset(self) -> None:
        self.last_target_center = None
        self.last_target_class = None
        self.last_frame_id = 0
        self.last_target_ts = None
        self.stable_frames = 0
        self.lock_settle_remaining = 0
        self.last_auto_fire_ts = None
        self.reset_candidate()

    def reset_candidate(self) -> None:
        self.candidate_target_center = None
        self.candidate_target_class = None
        self.candidate_target_ts = None
        self.candidate_stable_frames = 0


class ChrisDerivativePredictor:
    ALPHA_VEL: float = 0.25
    ALPHA_ACC: float = 0.15
    MAX_VEL: float = 3000.0
    MAX_ACC: float = 5000.0

    def __init__(self) -> None:
        self.smoothed_vel = np.zeros(2, dtype=np.float32)
        self.smoothed_acc = np.zeros(2, dtype=np.float32)
        self.prev_smoothed_vel = np.zeros(2, dtype=np.float32)

    def configure(self, config: dict[str, object]) -> None:
        self.ALPHA_VEL = float(np.clip(float(config.get('chris_predict_alpha_vel', self.ALPHA_VEL) or self.ALPHA_VEL), 0.0, 1.0))
        self.ALPHA_ACC = float(np.clip(float(config.get('chris_predict_alpha_acc', self.ALPHA_ACC) or self.ALPHA_ACC), 0.0, 1.0))
        self.MAX_VEL = max(1.0, float(config.get('chris_predict_max_vel', self.MAX_VEL) or self.MAX_VEL))
        self.MAX_ACC = max(1.0, float(config.get('chris_predict_max_acc', self.MAX_ACC) or self.MAX_ACC))

    def predict(self, curr_e: np.ndarray, prev_e: np.ndarray, prev_m: np.ndarray, dt: float) -> np.ndarray:
        if dt <= 1e-6:
            return np.zeros(2, dtype=np.float32)

        vel_raw = ((curr_e - prev_e) + prev_m) / dt
        vel_raw = np.clip(vel_raw, -self.MAX_VEL, self.MAX_VEL)
        for axis in range(2):
            if abs(float(curr_e[axis])) > 5.0 and np.sign(vel_raw[axis]) != np.sign(curr_e[axis]):
                vel_raw[axis] *= 0.1

        adj_alpha_vel = 1.0 - pow(1.0 - self.ALPHA_VEL, dt / 0.01)
        adj_alpha_vel = float(np.clip(adj_alpha_vel, 0.05, 0.8))
        self.prev_smoothed_vel = self.smoothed_vel.copy()
        self.smoothed_vel = adj_alpha_vel * vel_raw + (1.0 - adj_alpha_vel) * self.smoothed_vel

        acc_raw = (self.smoothed_vel - self.prev_smoothed_vel) / dt
        acc_raw = np.clip(acc_raw, -self.MAX_ACC, self.MAX_ACC)
        for axis in range(2):
            if abs(float(curr_e[axis])) > 5.0 and np.sign(acc_raw[axis]) != np.sign(curr_e[axis]):
                acc_raw[axis] *= 0.1

        adj_alpha_acc = 1.0 - pow(1.0 - self.ALPHA_ACC, dt / 0.01)
        adj_alpha_acc = float(np.clip(adj_alpha_acc, 0.05, 0.8))
        self.smoothed_acc = adj_alpha_acc * acc_raw + (1.0 - adj_alpha_acc) * self.smoothed_acc
        return self.smoothed_vel * dt + 0.5 * self.smoothed_acc * (dt ** 2)

    def reset(self) -> None:
        self.smoothed_vel.fill(0.0)
        self.smoothed_acc.fill(0.0)
        self.prev_smoothed_vel.fill(0.0)


class ChrisAimController:
    def __init__(
        self,
        kp: float = 0.45,
        ki: float = 0.02,
        kd: float = 0.04,
        pred_weight_x: float = 0.5,
        pred_weight_y: float = 0.1,
        init_scale: float = 0.6,
        ramp_time: float = 0.5,
        output_max: float = 512.0,
    ) -> None:
        self.kp = float(kp)
        self.ki = float(ki)
        self.kd = float(kd)
        self.pred_weight_x = float(pred_weight_x)
        self.pred_weight_y = float(pred_weight_y)
        self.init_scale = float(init_scale)
        self.ramp_time = float(ramp_time)
        self.output_max = float(output_max)
        self.i_max = 100.0
        self.lock_start_time: float | None = None
        self.last_time: float | None = None
        self._i_term = np.zeros(2, dtype=np.float32)
        self._last_error = np.zeros(2, dtype=np.float32)
        self._last_raw_error = np.zeros(2, dtype=np.float32)
        self._last_output = np.zeros(2, dtype=np.float32)
        self.predictor = ChrisDerivativePredictor()

    def configure(self, config: dict[str, object]) -> None:
        self.kp = max(0.0, float(config.get('chris_kp', self.kp) or self.kp))
        self.ki = max(0.0, float(config.get('chris_ki', self.ki) or self.ki))
        self.kd = max(0.0, float(config.get('chris_kd', self.kd) or self.kd))
        self.pred_weight_x = max(0.0, float(config.get('chris_predict_weight_x', self.pred_weight_x) or self.pred_weight_x))
        self.pred_weight_y = max(0.0, float(config.get('chris_predict_weight_y', self.pred_weight_y) or self.pred_weight_y))
        self.init_scale = max(0.0, float(config.get('chris_init_scale', self.init_scale) or self.init_scale))
        self.ramp_time = max(0.0, float(config.get('chris_ramp_time', self.ramp_time) or self.ramp_time))
        self.output_max = max(1.0, float(config.get('chris_output_max', self.output_max) or self.output_max))
        self.i_max = max(0.0, float(config.get('chris_i_max', self.i_max) or self.i_max))
        self.predictor.configure(config)

    def update(self, raw_dx: float, raw_dy: float, current_time: float) -> tuple[float, float]:
        dt = 0.01 if self.last_time is None else float(current_time - self.last_time)
        dt = float(np.clip(dt, 0.001, 0.2))
        self.last_time = float(current_time)

        curr_raw_error = np.array([float(raw_dx), float(raw_dy)], dtype=np.float32)
        small_flip_threshold = 16.0
        sign_flip_mask = np.sign(curr_raw_error) != np.sign(self._last_raw_error)
        small_error_mask = np.maximum(np.abs(curr_raw_error), np.abs(self._last_raw_error)) <= small_flip_threshold
        suppress_flip_mask = sign_flip_mask & small_error_mask
        pred_displacement = self.predictor.predict(
            curr_e=curr_raw_error,
            prev_e=self._last_raw_error,
            prev_m=self._last_output,
            dt=dt,
        )
        if np.any(suppress_flip_mask):
            pred_displacement = np.where(suppress_flip_mask, 0.0, pred_displacement)

        max_pred_allowed = np.minimum(np.maximum(np.abs(curr_raw_error) * 1.5, 30.0), 60.0)
        if np.any(np.abs(pred_displacement) > max_pred_allowed):
            pred_displacement = np.clip(pred_displacement, -max_pred_allowed, max_pred_allowed)
            if np.any(np.abs(pred_displacement) > 100.0):
                self.predictor.reset()
                pred_displacement.fill(0.0)

        weights = np.array([self.pred_weight_x, self.pred_weight_y], dtype=np.float32)
        fusion_error = curr_raw_error + pred_displacement * weights

        if self.lock_start_time is None:
            self.lock_start_time = float(current_time)
        elapsed = float(current_time - self.lock_start_time)
        if self.ramp_time <= 0.0 or elapsed >= self.ramp_time:
            scale = 1.0
        else:
            progress = elapsed / self.ramp_time
            scale = self.init_scale + (1.0 - self.init_scale) * progress
        real_kp = self.kp * scale

        p_term = fusion_error * real_kp
        self._i_term += fusion_error * dt * self.ki
        self._i_term = np.clip(self._i_term, -self.i_max, self.i_max)
        if np.any(suppress_flip_mask):
            self._i_term = np.where(suppress_flip_mask, 0.0, self._i_term)
        raw_d_term = (fusion_error - self._last_error) / dt * self.kd
        if np.any(suppress_flip_mask):
            raw_d_term = np.where(suppress_flip_mask, 0.0, raw_d_term)
        d_term = np.clip(raw_d_term, -50.0, 50.0)
        output = np.clip(p_term + self._i_term + d_term, -self.output_max, self.output_max)

        self._last_error = fusion_error
        self._last_raw_error = curr_raw_error
        self._last_output = output
        return float(output[0]), float(output[1])

    def reset(self) -> None:
        self._i_term.fill(0.0)
        self._last_error.fill(0.0)
        self._last_raw_error.fill(0.0)
        self._last_output.fill(0.0)
        self.last_time = None
        self.lock_start_time = None
        self.predictor.reset()


class BezierMovementGenerator:
    def __init__(self) -> None:
        self.random_engine = random.Random()
        self.segment_dist_1 = 3.0
        self.segment_dist_2 = 8.0
        self.segment_dist_3 = 20.0
        self.curve_min_distance = 15.0
        self.curve_scale = 0.03
        self.jitter_min_distance = 10.0
        self.jitter_scale = 0.01
        self.jitter_damping = 0.3

    def configure(self, config: dict[str, object]) -> None:
        segment_dist_1 = max(0.0, float(config.get('bezier_segment_dist_1', 3.0) or 0.0))
        segment_dist_2 = max(segment_dist_1, float(config.get('bezier_segment_dist_2', 8.0) or segment_dist_1))
        segment_dist_3 = max(segment_dist_2, float(config.get('bezier_segment_dist_3', 20.0) or segment_dist_2))
        self.segment_dist_1 = segment_dist_1
        self.segment_dist_2 = segment_dist_2
        self.segment_dist_3 = segment_dist_3
        self.curve_min_distance = max(0.0, float(config.get('bezier_curve_min_distance', 15.0) or 0.0))
        self.curve_scale = max(0.0, float(config.get('bezier_curve_scale', 0.03) or 0.0))
        self.jitter_min_distance = max(0.0, float(config.get('bezier_jitter_min_distance', 10.0) or 0.0))
        self.jitter_scale = max(0.0, float(config.get('bezier_jitter_scale', 0.01) or 0.0))
        self.jitter_damping = max(0.0, min(1.0, float(config.get('bezier_jitter_damping', 0.3) or 0.0)))

    def random_offset(self, scale: float) -> float:
        return self.random_engine.uniform(-float(scale), float(scale))

    def random_jitter(self, scale: float) -> float:
        return self.random_engine.uniform(-float(scale), float(scale))

    def predict(self, dx: int, dy: int) -> list[tuple[int, int]]:
        total_dx = int(dx)
        total_dy = int(dy)
        if total_dx == 0 and total_dy == 0:
            return []

        dist = math.hypot(float(total_dx), float(total_dy))
        angle = math.atan2(float(total_dy), float(total_dx))
        if dist < self.segment_dist_1:
            num_samples = 1
        elif dist < self.segment_dist_2:
            num_samples = 2
        elif dist < self.segment_dist_3:
            num_samples = 3
        else:
            num_samples = 4

        relative_moves: list[tuple[int, int]] = []
        sent_x = 0
        sent_y = 0
        for index in range(1, num_samples + 1):
            progress = float(index) / float(num_samples)
            base_x = float(total_dx) * progress
            base_y = float(total_dy) * progress
            final_x = base_x
            final_y = base_y

            if dist > self.curve_min_distance and self.curve_scale > 0.0:
                curve_factor = math.sin(progress * math.pi)
                curve_offset = curve_factor * dist * self.curve_scale
                perp_x = -math.sin(angle)
                perp_y = math.cos(angle)
                curve_direction = 1.0 if self.random_offset(1.0) > 0.0 else -1.0
                final_x += perp_x * curve_offset * curve_direction
                final_y += perp_y * curve_offset * curve_direction

            if dist > self.jitter_min_distance and self.jitter_scale > 0.0 and self.jitter_damping > 0.0:
                jitter_scale = dist * self.jitter_scale
                jitter_progress = 1.0 - abs(progress - 0.5) * 2.0
                final_x += self.random_jitter(jitter_scale * jitter_progress * self.jitter_damping)
                final_y += self.random_jitter(jitter_scale * jitter_progress * self.jitter_damping)

            target_x = int(round(final_x))
            target_y = int(round(final_y))
            delta_x = int(target_x - sent_x)
            delta_y = int(target_y - sent_y)
            relative_moves.append((delta_x, delta_y))
            sent_x = int(target_x)
            sent_y = int(target_y)

        correction_x = int(total_dx - sent_x)
        correction_y = int(total_dy - sent_y)
        if correction_x != 0 or correction_y != 0:
            relative_moves.append((correction_x, correction_y))
        return [(step_x, step_y) for step_x, step_y in relative_moves if step_x != 0 or step_y != 0]


@dataclass
class MouseMoveRecord:
    ts: float
    dx: int
    dy: int


class MouseMoveHistory:
    def __init__(self, retention_seconds: float = 5.0) -> None:
        self.retention_seconds = max(0.5, float(retention_seconds))
        self._records: deque[MouseMoveRecord] = deque()
        self._lock = threading.Lock()

    def record_move(self, *, ts: float, dx: int, dy: int) -> None:
        if int(dx) == 0 and int(dy) == 0:
            return
        with self._lock:
            self._prune_locked(now=float(ts))
            self._records.append(MouseMoveRecord(ts=float(ts), dx=int(dx), dy=int(dy)))

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def sum_since(
        self,
        since_ts: float | None,
        *,
        now: float | None = None,
        reflected_before_ts: float | None = None,
    ) -> tuple[int, int]:
        if since_ts is None:
            return 0, 0
        total_dx = 0
        total_dy = 0
        reflected_cutoff = float('inf') if reflected_before_ts is None else float(reflected_before_ts)
        with self._lock:
            self._prune_locked(now=float(time.perf_counter() if now is None else now))
            for record in self._records:
                if float(record.ts) + 1e-9 < float(since_ts):
                    continue
                if float(record.ts) > reflected_cutoff:
                    continue
                total_dx += int(record.dx)
                total_dy += int(record.dy)
        return total_dx, total_dy

    def _prune_locked(self, now: float) -> None:
        cutoff = float(now) - self.retention_seconds
        while self._records and float(self._records[0].ts) < cutoff:
            self._records.popleft()


class AsyncMouseMover:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._queue: deque[tuple[PassthroughController, int, int, float, MouseMoveHistory | None]] = deque()
        self._thread: threading.Thread | None = None

    def _ensure_started_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name='async-mouse-mover', daemon=True)
        self._thread.start()

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()

    def enqueue(
        self,
        *,
        controller: PassthroughController,
        steps: list[tuple[int, int]],
        step_sleep_ms: float,
        move_history: MouseMoveHistory | None,
        replace_pending: bool = True,
    ) -> bool:
        normalized_steps = [(int(dx), int(dy)) for dx, dy in steps if int(dx) != 0 or int(dy) != 0]
        if not normalized_steps:
            return False
        sleep_seconds = max(0.0, float(step_sleep_ms)) / 1000.0
        with self._lock:
            if replace_pending:
                self._queue.clear()
            for dx, dy in normalized_steps:
                self._queue.append((controller, int(dx), int(dy), sleep_seconds, move_history))
            self._ensure_started_locked()
            self._event.set()
        return True

    def _run(self) -> None:
        while True:
            self._event.wait()
            while True:
                with self._lock:
                    if not self._queue:
                        self._event.clear()
                        break
                    controller, dx, dy, sleep_seconds, move_history = self._queue.popleft()
                issued_at = time.perf_counter()
                try:
                    controller.mouse_move(dx=int(dx), dy=int(dy))
                    if move_history is not None:
                        move_history.record_move(ts=issued_at, dx=int(dx), dy=int(dy))
                except Exception:
                    pass
                if sleep_seconds > 0.0:
                    time.sleep(sleep_seconds)


def clamp_mouse_step(value: float, max_step: int) -> int:
    if not np.isfinite(float(value)):
        return 0
    return int(np.clip(np.rint(float(value)), -abs(int(max_step)), abs(int(max_step))))


def clamp_fine_aim_axis(raw_move: float, target_offset: float, fine_radius_px: float) -> float:
    radius = max(0.0, float(fine_radius_px))
    if radius <= 0.0:
        return float(raw_move)
    offset = float(target_offset)
    if abs(offset) > radius:
        return float(raw_move)

    move = float(raw_move)
    if abs(offset) <= 1e-6:
        return 0.0
    if move * offset < 0.0:
        return 0.0

    max_mag = abs(offset)
    return float(np.clip(move, -max_mag, max_mag))


def apply_near_target_speed_decay(
    raw_move_x: float,
    raw_move_y: float,
    *,
    target_distance: float,
    deadzone_px: float,
    slow_radius_px: float,
    min_scale: float,
) -> tuple[float, float]:
    distance = max(0.0, float(target_distance))
    deadzone = max(0.0, float(deadzone_px))
    slow_radius = max(0.0, float(slow_radius_px))
    floor_scale = float(np.clip(float(min_scale), 0.0, 1.0))
    if slow_radius <= deadzone or floor_scale >= 0.999:
        return float(raw_move_x), float(raw_move_y)
    if distance >= slow_radius:
        return float(raw_move_x), float(raw_move_y)

    span = max(1e-6, slow_radius - deadzone)
    t = float(np.clip((distance - deadzone) / span, 0.0, 1.0))
    smooth_t = t * t * (3.0 - 2.0 * t)
    scale = floor_scale + (1.0 - floor_scale) * smooth_t
    return float(raw_move_x) * scale, float(raw_move_y) * scale


DEFAULT_AIM_KEYS = ['KEY_LEFTSHIFT', 'KEY_RIGHTSHIFT']


def parse_aim_keys(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw_items = [str(item).strip().upper() for item in value if str(item).strip()]
    else:
        text = str(value or '').strip()
        normalized = (
            text.replace('\uff0c', ',')
            .replace('\uff1b', ',')
            .replace(';', ',')
            .replace('\n', ',')
            .replace('\r', ',')
        )
        raw_items = [part.strip().upper() for part in re.split(r'[\s,]+', normalized) if part.strip()]
    parsed: list[str] = []
    for item in raw_items:
        key_name = item if item.startswith('KEY_') else f'KEY_{item}'
        if key_name not in parsed:
            parsed.append(key_name)
    return parsed or list(DEFAULT_AIM_KEYS)


def parse_target_hotkeys(value: object) -> dict[int, list[str]]:
    text = str(value or '').strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    normalized: dict[int, list[str]] = {}
    for raw_class_id, raw_keys in parsed.items():
        try:
            class_id = int(raw_class_id)
        except Exception:
            continue
        keys = parse_aim_keys(raw_keys)
        if keys:
            normalized[int(class_id)] = list(keys)
    return normalized


def parse_target_class_priority(value: object, fallback_class: int = 0) -> list[int]:
    raw_items: list[object] = []
    if isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        text = str(value or '').strip()
        if text:
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                raw_items = list(parsed)
            else:
                normalized = (
                    text.replace('\uff0c', ',')
                    .replace('\uff1b', ',')
                    .replace(';', ',')
                    .replace('\n', ',')
                    .replace('\r', ',')
                )
                raw_items = [part.strip() for part in re.split(r'[\s,]+', normalized) if part.strip()]
    ordered: list[int] = []
    for item in raw_items:
        try:
            class_id = max(0, int(float(item)))
        except Exception:
            continue
        if class_id not in ordered:
            ordered.append(class_id)
    if not ordered:
        ordered.append(max(0, int(fallback_class)))
    return ordered


def move_target_class_to_front(target_classes: list[int], class_id: int) -> list[int]:
    normalized = [int(value) for value in target_classes if int(value) != int(class_id)]
    return [int(class_id)] + normalized


def extract_pressed_keys(control_state: object) -> set[str]:
    if not isinstance(control_state, dict):
        return set()
    raw_keys = control_state.get('keys')
    if not isinstance(raw_keys, list):
        raw_keys = control_state.get('pressed_keys')
    if not isinstance(raw_keys, list):
        return set()
    return {str(item).strip().upper() for item in raw_keys if str(item).strip()}


def extract_pressed_mouse_buttons(control_state: object) -> set[str]:
    if not isinstance(control_state, dict):
        return set()
    raw_buttons = control_state.get('mouse_buttons')
    if not isinstance(raw_buttons, list):
        return set()
    return {str(item).strip().upper() for item in raw_buttons if str(item).strip()}


def find_target_hotkey_switch(
    pressed_keys: set[str],
    previous_pressed_keys: set[str],
    target_hotkeys: dict[int, list[str]],
) -> tuple[int | None, str]:
    for class_id in sorted(target_hotkeys):
        for key_name in target_hotkeys[class_id]:
            if key_name in pressed_keys and key_name not in previous_pressed_keys:
                return class_id, key_name
    return None, ''


def make_empty_aim_stats() -> dict[str, object]:
    return {
        'key_pressed': False,
        'pressed_key': '',
        'key_query_ms': 0.0,
        'mouse_cmd_ms': 0.0,
        'active_target_class': 0,
        'active_target_classes': [],
        'selected_target_class': -1,
        'switch_key': '',
        'target_class_changed': False,
        'target_found': False,
        'target_score': 0.0,
        'target_offset_x': 0.0,
        'target_offset_y': 0.0,
        'target_distance': 0.0,
        'stable_frames': 0,
        'stable_ready': False,
        'same_visual_frame': False,
        'first_frame': False,
        'required_lock_frames': 0,
        'filtered_offset_x': 0.0,
        'filtered_offset_y': 0.0,
        'control_offset_x': 0.0,
        'control_offset_y': 0.0,
        'move_x': 0,
        'move_y': 0,
        'moved': False,
        'auto_fired': False,
        'auto_fire_cmd_ms': 0.0,
        'error': '',
    }


def box_center(box: np.ndarray) -> tuple[float, float]:
    return (float(box[0] + box[2]) / 2.0, float(box[1] + box[3]) / 2.0)


def predict_screen_center_from_history(
    center: tuple[float, float] | None,
    since_ts: float | None,
    move_history: MouseMoveHistory | None,
    now_ts: float,
    compensation_delay_ms: float = 0.0,
) -> tuple[float, float] | None:
    if center is None:
        return None
    if move_history is None or since_ts is None:
        return (float(center[0]), float(center[1]))
    reflected_before_ts = float(now_ts) - max(0.0, float(compensation_delay_ms)) / 1000.0
    moved_dx, moved_dy = move_history.sum_since(since_ts, now=now_ts, reflected_before_ts=reflected_before_ts)
    return (
        float(center[0]) - float(moved_dx),
        float(center[1]) - float(moved_dy),
    )


def split_mouse_move_step(dx: int, dy: int, max_step_px: float) -> list[tuple[int, int]]:
    total_dx = int(dx)
    total_dy = int(dy)
    if total_dx == 0 and total_dy == 0:
        return []
    limit = max(0.0, float(max_step_px))
    if limit <= 0.0:
        return [(total_dx, total_dy)]
    distance = math.hypot(float(total_dx), float(total_dy))
    if distance <= limit + 1e-6:
        return [(total_dx, total_dy)]

    num_parts = max(1, int(math.ceil(distance / limit)))
    sent_x = 0
    sent_y = 0
    steps: list[tuple[int, int]] = []
    for index in range(1, num_parts + 1):
        progress = float(index) / float(num_parts)
        target_x = int(round(float(total_dx) * progress))
        target_y = int(round(float(total_dy) * progress))
        step_dx = int(target_x - sent_x)
        step_dy = int(target_y - sent_y)
        if step_dx != 0 or step_dy != 0:
            steps.append((step_dx, step_dy))
        sent_x = int(target_x)
        sent_y = int(target_y)
    return steps


def pick_target_candidates(
    boxes: np.ndarray,
    classes: np.ndarray,
    scores: np.ndarray,
    target_classes: list[int],
    frame_shape: tuple[int, int, int],
    reference_center: tuple[float, float] | None = None,
    reference_radius: float = 0.0,
):
    if len(boxes) == 0:
        return None

    frame_h, frame_w = frame_shape[:2]
    center_x = frame_w / 2.0
    center_y = frame_h / 2.0
    ref_radius_sq = max(0.0, float(reference_radius)) ** 2

    def pick_in_single_class(target_class: int):
        mask = classes == int(target_class)
        if not np.any(mask):
            return None, None

        selected_boxes = boxes[mask]
        selected_scores = scores[mask]
        best_center_index = 0
        best_center_distance_sq = None
        best_center_score = -1.0
        best_ref_index: int | None = None
        best_ref_distance_sq = None
        best_ref_score = -1.0

        for index, box in enumerate(selected_boxes):
            target_cx, target_cy = box_center(box)
            dx = target_cx - center_x
            dy = target_cy - center_y
            distance_sq = dx * dx + dy * dy
            score = float(selected_scores[index])
            if (
                best_center_distance_sq is None
                or distance_sq < best_center_distance_sq - 1e-6
                or (abs(distance_sq - best_center_distance_sq) <= 1e-6 and score > best_center_score)
            ):
                best_center_index = index
                best_center_distance_sq = distance_sq
                best_center_score = score

            if reference_center is not None and ref_radius_sq > 0.0:
                ref_dx = target_cx - float(reference_center[0])
                ref_dy = target_cy - float(reference_center[1])
                ref_distance_sq = ref_dx * ref_dx + ref_dy * ref_dy
                if ref_distance_sq <= ref_radius_sq:
                    if (
                        best_ref_distance_sq is None
                        or ref_distance_sq < best_ref_distance_sq - 1e-6
                        or (abs(ref_distance_sq - best_ref_distance_sq) <= 1e-6 and score > best_ref_score)
                    ):
                        best_ref_index = index
                        best_ref_distance_sq = ref_distance_sq
                        best_ref_score = score

        tracked_target = None if best_ref_index is None else (selected_boxes[best_ref_index], float(selected_scores[best_ref_index]))
        fallback_target = (selected_boxes[best_center_index], float(selected_scores[best_center_index]))
        return tracked_target, fallback_target

    ordered_target_classes = [int(value) for value in target_classes]
    fallback_target = None
    for target_class in ordered_target_classes:
        tracked_target, current_fallback = pick_in_single_class(int(target_class))
        if tracked_target is not None:
            target_box, target_score = tracked_target
            return target_box, target_score, int(target_class)
        if current_fallback is not None and fallback_target is None:
            target_box, target_score = current_fallback
            fallback_target = (target_box, target_score, int(target_class))
    return fallback_target


def send_mouse_move_with_bezier(
    controller: PassthroughController,
    movement_generator: BezierMovementGenerator,
    dx: int,
    dy: int,
    move_substep_max_px: float = 0.0,
    move_step_sleep_ms: float = 0.0,
    move_history: MouseMoveHistory | None = None,
    async_mover: AsyncMouseMover | None = None,
) -> tuple[float, bool]:
    started_at = time.perf_counter()
    steps: list[tuple[int, int]] = []
    step_sleep_seconds = max(0.0, float(move_step_sleep_ms)) / 1000.0
    for step_dx, step_dy in movement_generator.predict(int(dx), int(dy)):
        steps.extend(split_mouse_move_step(int(step_dx), int(step_dy), float(move_substep_max_px)))

    if async_mover is not None:
        issued = async_mover.enqueue(
            controller=controller,
            steps=steps,
            step_sleep_ms=float(move_step_sleep_ms),
            move_history=move_history,
            replace_pending=True,
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        return elapsed_ms, issued

    issued = False
    for sub_dx, sub_dy in steps:
        issued_at = time.perf_counter()
        controller.mouse_move(dx=int(sub_dx), dy=int(sub_dy))
        issued = True
        if move_history is not None:
            move_history.record_move(ts=issued_at, dx=int(sub_dx), dy=int(sub_dy))
        if step_sleep_seconds > 0.0:
            time.sleep(step_sleep_seconds)
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    return elapsed_ms, issued


def simple_aim_mouse_if_needed(
    controller: PassthroughController,
    movement_generator: BezierMovementGenerator,
    aim_controller: SimpleAimState,
    motion_controller: ChrisAimController,
    frame_id: int,
    frame_shape: tuple[int, int, int],
    boxes: np.ndarray,
    classes: np.ndarray,
    scores: np.ndarray,
    aim_keys: list[str],
    pressed_keys: set[str],
    key_query_ms: float,
    target_classes: list[int],
    target_switch_stable_frames: int,
    target_track_radius_px: float,
    target_motion_compensation_delay_ms: float,
    lock_settle_frames: int,
    pressed_mouse_buttons: set[str],
    auto_fire_enabled: bool,
    auto_fire_stable_frames: int,
    auto_fire_radius_px: float,
    auto_fire_cooldown_ms: float,
    axis_x_scale: float,
    axis_y_scale: float,
    aim_deadzone_px: float,
    near_target_slow_radius_px: float,
    near_target_min_scale: float,
    fine_aim_radius_px: float,
    micro_move_hold_radius_px: float,
    micro_move_min_step_px: float,
    move_async_enabled: bool,
    move_substep_max_px: float,
    move_step_sleep_ms: float,
    max_step: int,
    luma_enabled: bool = True,
    move_history: MouseMoveHistory | None = None,
    async_mover: AsyncMouseMover | None = None,
) -> dict[str, object]:
    stats = make_empty_aim_stats()
    stats['key_query_ms'] = float(key_query_ms)
    now_ts = time.perf_counter()
    normalized_target_classes = [int(value) for value in target_classes]
    primary_target_class = int(normalized_target_classes[0]) if normalized_target_classes else 0
    stats['active_target_class'] = int(primary_target_class)
    stats['active_target_classes'] = list(normalized_target_classes)

    axis_x = max(0.1, min(4.0, float(axis_x_scale)))
    axis_y = max(0.1, min(4.0, float(axis_y_scale)))
    deadzone_px = max(0.0, float(aim_deadzone_px))
    near_target_slow_radius = max(0.0, float(near_target_slow_radius_px))
    near_target_min_scale = float(np.clip(float(near_target_min_scale), 0.0, 1.0))
    fine_aim_radius = max(0.0, float(fine_aim_radius_px))
    micro_move_hold_radius = max(0.0, float(micro_move_hold_radius_px))
    micro_move_min_step = max(0.0, float(micro_move_min_step_px))
    auto_fire_required_frames = max(1, int(auto_fire_stable_frames))
    auto_fire_radius = max(0.0, float(auto_fire_radius_px))
    auto_fire_cooldown_seconds = max(0.0, float(auto_fire_cooldown_ms)) / 1000.0
    step_limit = max(1, min(127, int(max_step)))
    stable_required = max(1, int(target_switch_stable_frames))
    continuity_radius = max(8.0, float(target_track_radius_px))
    compensation_delay_ms = max(0.0, float(target_motion_compensation_delay_ms))
    settle_required = max(0, int(lock_settle_frames))
    for key_name in aim_keys:
        if key_name in pressed_keys:
            stats['key_pressed'] = True
            stats['pressed_key'] = key_name
            break
    if not bool(stats['key_pressed']):
        aim_controller.reset()
        motion_controller.reset()
        return stats

    reference_center = predict_screen_center_from_history(
        aim_controller.last_target_center,
        aim_controller.last_target_ts,
        move_history,
        now_ts,
        compensation_delay_ms,
    )
    reference_radius = continuity_radius if reference_center is not None else 0.0
    target = pick_target_candidates(
        boxes,
        classes,
        scores,
        normalized_target_classes,
        frame_shape,
        reference_center=reference_center,
        reference_radius=reference_radius,
    )
    if target is None:
        aim_controller.reset()
        motion_controller.reset()
        return stats

    target_box, target_score, selected_target_class = target
    target_cx, target_cy = box_center(target_box)
    frame_h, frame_w = frame_shape[:2]
    center_x = float(frame_w) / 2.0
    center_y = float(frame_h) / 2.0
    target_offset_x = float(target_cx) - center_x
    target_offset_y = float(target_cy) - center_y
    target_distance = math.hypot(target_offset_x, target_offset_y)
    same_visual_frame = bool(int(frame_id) == int(aim_controller.last_frame_id))

    continuity_distance = 0.0
    if reference_center is not None:
        continuity_distance = math.hypot(
            float(target_cx) - float(reference_center[0]),
            float(target_cy) - float(reference_center[1]),
        )
    same_target = (
        reference_center is not None
        and aim_controller.last_target_class == int(selected_target_class)
        and continuity_distance <= continuity_radius
    )

    candidate_distance = 0.0
    same_candidate = False
    predicted_candidate_center = predict_screen_center_from_history(
        aim_controller.candidate_target_center,
        aim_controller.candidate_target_ts,
        move_history,
        now_ts,
        compensation_delay_ms,
    )
    if predicted_candidate_center is not None:
        candidate_distance = math.hypot(
            float(target_cx) - float(predicted_candidate_center[0]),
            float(target_cy) - float(predicted_candidate_center[1]),
        )
        same_candidate = (
            aim_controller.candidate_target_class == int(selected_target_class)
            and candidate_distance <= continuity_radius
        )

    accepted_target = False
    first_frame = aim_controller.last_target_center is None or not same_target
    if not same_visual_frame:
        if same_target:
            aim_controller.reset_candidate()
            aim_controller.stable_frames = int(aim_controller.stable_frames + 1)
            aim_controller.last_target_center = (float(target_cx), float(target_cy))
            aim_controller.last_target_class = int(selected_target_class)
            aim_controller.last_target_ts = float(now_ts)
            accepted_target = True
        else:
            if same_candidate:
                aim_controller.candidate_stable_frames = int(aim_controller.candidate_stable_frames + 1)
                aim_controller.candidate_target_center = (float(target_cx), float(target_cy))
                aim_controller.candidate_target_ts = float(now_ts)
            else:
                aim_controller.candidate_target_center = (float(target_cx), float(target_cy))
                aim_controller.candidate_target_class = int(selected_target_class)
                aim_controller.candidate_target_ts = float(now_ts)
                aim_controller.candidate_stable_frames = 1

            if int(aim_controller.candidate_stable_frames) >= int(stable_required):
                motion_controller.reset()
                aim_controller.last_target_center = (float(target_cx), float(target_cy))
                aim_controller.last_target_class = int(selected_target_class)
                aim_controller.last_target_ts = float(now_ts)
                aim_controller.stable_frames = int(aim_controller.candidate_stable_frames)
                aim_controller.lock_settle_remaining = int(settle_required)
                aim_controller.reset_candidate()
                accepted_target = True
        aim_controller.last_frame_id = int(frame_id)
    elif same_target:
        accepted_target = True

    current_stable_frames = int(aim_controller.stable_frames)
    if not accepted_target:
        current_stable_frames = int(aim_controller.candidate_stable_frames)

    def try_auto_fire() -> bool:
        if not (
            bool(auto_fire_enabled)
            and not bool(same_visual_frame)
            and 'BTN_LEFT' not in pressed_mouse_buttons
            and int(current_stable_frames) >= int(auto_fire_required_frames)
            and (auto_fire_radius <= 0.0 or float(target_distance) <= auto_fire_radius)
        ):
            return False
        last_auto_fire_ts = aim_controller.last_auto_fire_ts
        if last_auto_fire_ts is not None and (float(now_ts) - float(last_auto_fire_ts)) < auto_fire_cooldown_seconds:
            return False
        click_started = time.perf_counter()
        controller.mouse_click('left')
        stats['auto_fire_cmd_ms'] = (time.perf_counter() - click_started) * 1000.0
        stats['auto_fired'] = True
        aim_controller.last_auto_fire_ts = float(now_ts)
        return True

    if not accepted_target:
        stats.update(
            {
                'target_found': True,
                'target_score': float(target_score),
                'selected_target_class': int(selected_target_class),
                'target_offset_x': float(target_offset_x),
                'target_offset_y': float(target_offset_y),
                'target_distance': float(target_distance),
                'stable_frames': int(current_stable_frames),
                'stable_ready': False,
                'required_lock_frames': int(stable_required),
                'same_visual_frame': bool(same_visual_frame),
                'first_frame': bool(first_frame),
                'luma_enabled': bool(luma_enabled),
                'filtered_offset_x': float(target_offset_x),
                'filtered_offset_y': float(target_offset_y),
                'control_offset_x': 0.0,
                'control_offset_y': 0.0,
                'move_x': 0,
                'move_y': 0,
            }
        )
        return stats

    if int(aim_controller.lock_settle_remaining) > 0:
        aim_controller.lock_settle_remaining = max(0, int(aim_controller.lock_settle_remaining) - 1)
        motion_controller.reset()
        stats.update(
            {
                'target_found': True,
                'target_score': float(target_score),
                'selected_target_class': int(selected_target_class),
                'target_offset_x': float(target_offset_x),
                'target_offset_y': float(target_offset_y),
                'target_distance': float(target_distance),
                'stable_frames': int(current_stable_frames),
                'stable_ready': int(current_stable_frames) >= int(stable_required),
                'required_lock_frames': int(stable_required),
                'same_visual_frame': bool(same_visual_frame),
                'first_frame': bool(first_frame),
                'luma_enabled': bool(luma_enabled),
                'filtered_offset_x': float(target_offset_x),
                'filtered_offset_y': float(target_offset_y),
                'control_offset_x': 0.0,
                'control_offset_y': 0.0,
                'move_x': 0,
                'move_y': 0,
            }
        )
        return stats

    if deadzone_px > 0.0 and float(target_distance) <= deadzone_px:
        try_auto_fire()
        if async_mover is not None:
            async_mover.clear()
        motion_controller.reset()
        stats.update(
            {
                'target_found': True,
                'target_score': float(target_score),
                'selected_target_class': int(selected_target_class),
                'target_offset_x': float(target_offset_x),
                'target_offset_y': float(target_offset_y),
                'target_distance': float(target_distance),
                'stable_frames': int(current_stable_frames),
                'stable_ready': int(current_stable_frames) >= int(stable_required),
                'required_lock_frames': int(stable_required),
                'same_visual_frame': bool(same_visual_frame),
                'first_frame': bool(first_frame),
                'luma_enabled': bool(luma_enabled),
                'filtered_offset_x': float(target_offset_x),
                'filtered_offset_y': float(target_offset_y),
                'control_offset_x': 0.0,
                'control_offset_y': 0.0,
                'move_x': 0,
                'move_y': 0,
            }
        )
        return stats

    if bool(luma_enabled):
        controller_x, controller_y = motion_controller.update(
            raw_dx=float(target_offset_x),
            raw_dy=float(target_offset_y),
            current_time=time.perf_counter(),
        )
    else:
        motion_controller.reset()
        controller_x, controller_y = float(target_offset_x), float(target_offset_y)
    raw_move_x = float(controller_x) * float(axis_x)
    raw_move_y = float(controller_y) * float(axis_y)
    raw_move_x, raw_move_y = apply_near_target_speed_decay(
        raw_move_x,
        raw_move_y,
        target_distance=float(target_distance),
        deadzone_px=deadzone_px,
        slow_radius_px=near_target_slow_radius,
        min_scale=near_target_min_scale,
    )
    raw_move_x = clamp_fine_aim_axis(raw_move_x, target_offset_x, fine_aim_radius)
    raw_move_y = clamp_fine_aim_axis(raw_move_y, target_offset_y, fine_aim_radius)
    move_x = clamp_mouse_step(float(raw_move_x), int(step_limit))
    move_y = clamp_mouse_step(float(raw_move_y), int(step_limit))
    if (
        micro_move_hold_radius > 0.0
        and micro_move_min_step > 0.0
        and float(target_distance) <= micro_move_hold_radius
        and math.hypot(float(move_x), float(move_y)) <= micro_move_min_step
    ):
        motion_controller.reset()
        raw_move_x = 0.0
        raw_move_y = 0.0
        move_x = 0
        move_y = 0

    auto_fire_triggered = try_auto_fire()
    if bool(auto_fire_triggered):
        if async_mover is not None:
            async_mover.clear()
        raw_move_x = 0.0
        raw_move_y = 0.0
        move_x = 0
        move_y = 0

    stats.update(
        {
            'target_found': True,
            'target_score': float(target_score),
            'selected_target_class': int(selected_target_class),
            'target_offset_x': float(target_offset_x),
            'target_offset_y': float(target_offset_y),
            'target_distance': float(target_distance),
            'stable_frames': int(current_stable_frames),
            'stable_ready': int(current_stable_frames) >= int(stable_required),
            'required_lock_frames': int(stable_required),
            'same_visual_frame': bool(same_visual_frame),
            'first_frame': bool(first_frame),
            'luma_enabled': bool(luma_enabled),
            'filtered_offset_x': float(target_offset_x),
            'filtered_offset_y': float(target_offset_y),
            'control_offset_x': float(raw_move_x),
            'control_offset_y': float(raw_move_y),
            'move_x': int(move_x),
            'move_y': int(move_y),
        }
    )
    if bool(same_visual_frame):
        if async_mover is not None:
            async_mover.clear()
        stats['move_x'] = 0
        stats['move_y'] = 0
        return stats
    if bool(auto_fire_triggered):
        return stats
    if int(move_x) == 0 and int(move_y) == 0:
        if async_mover is not None:
            async_mover.clear()
        return stats

    stats['mouse_cmd_ms'], stats['moved'] = send_mouse_move_with_bezier(
        controller=controller,
        movement_generator=movement_generator,
        dx=int(move_x),
        dy=int(move_y),
        move_substep_max_px=float(move_substep_max_px),
        move_step_sleep_ms=float(move_step_sleep_ms),
        move_history=move_history,
        async_mover=async_mover if bool(move_async_enabled) else None,
    )
    return stats


@dataclass
class SingleLoopAimContext:
    aim_state: SimpleAimState = field(default_factory=SimpleAimState)
    movement_generator: BezierMovementGenerator = field(default_factory=lambda: BezierMovementGenerator())
    chris_controller: ChrisAimController = field(default_factory=ChrisAimController)
    async_mover: AsyncMouseMover = field(default_factory=AsyncMouseMover)
    previous_pressed_keys: set[str] = field(default_factory=set)
    previous_aim_active: bool = False


def run_single_loop_aim(
    controller: PassthroughController,
    move_history: MouseMoveHistory,
    loop_context: SingleLoopAimContext,
    runtime_config: dict[str, object],
    frame_id: int,
    frame_shape: tuple[int, int, int],
    boxes: np.ndarray,
    classes: np.ndarray,
    scores: np.ndarray,
) -> tuple[dict[str, object], int, list[int], float]:
    loop_started = time.perf_counter()
    aim_keys = parse_aim_keys(runtime_config.get('aim_keys_text', ''))
    runtime_aim_class = int(runtime_config.get('aim_class', 0) or 0)
    active_target_classes = parse_target_class_priority(runtime_config.get('aim_classes_text', ''), fallback_class=runtime_aim_class)
    active_target_class = int(active_target_classes[0]) if active_target_classes else runtime_aim_class
    target_hotkeys = parse_target_hotkeys(runtime_config.get('aim_target_keys_text', ''))
    loop_context.movement_generator.configure(runtime_config)
    loop_context.chris_controller.configure(runtime_config)

    key_state_started = time.perf_counter()
    pressed_keys: set[str] = set()
    pressed_mouse_buttons: set[str] = set()
    key_state_error = ''
    try:
        control_state = controller.get_state(timeout=0.01)
        pressed_keys = extract_pressed_keys(control_state)
        pressed_mouse_buttons = extract_pressed_mouse_buttons(control_state)
    except Exception as exc:
        key_state_error = str(exc)
    key_query_ms = (time.perf_counter() - key_state_started) * 1000.0

    current_aim_active = any(key_name in pressed_keys for key_name in aim_keys)
    if current_aim_active != loop_context.previous_aim_active:
        loop_context.async_mover.clear()
        move_history.clear()
        loop_context.aim_state.reset()
        loop_context.chris_controller.reset()
        loop_context.previous_aim_active = current_aim_active

    switch_key = ''
    switched_target_class, switch_key = find_target_hotkey_switch(
        pressed_keys,
        loop_context.previous_pressed_keys,
        target_hotkeys,
    )
    class_changed = switched_target_class is not None and int(switched_target_class) != active_target_class
    if class_changed:
        active_target_classes = move_target_class_to_front(active_target_classes, int(switched_target_class))
        active_target_class = int(active_target_classes[0]) if active_target_classes else int(switched_target_class)

    aim_stats = make_empty_aim_stats()
    aim_stats['active_target_class'] = active_target_class
    aim_stats['active_target_classes'] = list(active_target_classes)
    aim_stats['switch_key'] = switch_key
    aim_stats['target_class_changed'] = bool(class_changed and switch_key)
    if key_state_error:
        aim_stats['error'] = key_state_error

    try:
        current_aim_stats = simple_aim_mouse_if_needed(
            controller=controller,
            movement_generator=loop_context.movement_generator,
            aim_controller=loop_context.aim_state,
            motion_controller=loop_context.chris_controller,
            frame_id=int(frame_id),
            frame_shape=frame_shape,
            boxes=boxes,
            classes=classes,
            scores=scores,
            pressed_keys=pressed_keys,
            aim_keys=aim_keys,
            key_query_ms=key_query_ms,
            target_classes=active_target_classes,
            target_switch_stable_frames=int(runtime_config.get('target_switch_stable_frames', 3) or 1),
            target_track_radius_px=float(runtime_config.get('target_track_radius_px', 40.0) or 40.0),
            target_motion_compensation_delay_ms=float(runtime_config.get('target_motion_compensation_delay_ms', 35.0) or 0.0),
            lock_settle_frames=int(runtime_config.get('lock_settle_frames', 1) or 0),
            pressed_mouse_buttons=pressed_mouse_buttons,
            auto_fire_enabled=bool(runtime_config.get('auto_fire_enabled', False)),
            auto_fire_stable_frames=int(runtime_config.get('auto_fire_stable_frames', 6) or 1),
            auto_fire_radius_px=float(runtime_config.get('auto_fire_radius_px', 4.0) or 0.0),
            auto_fire_cooldown_ms=float(runtime_config.get('auto_fire_cooldown_ms', 250.0) or 0.0),
            axis_x_scale=float(runtime_config.get('aim_axis_x_scale', 1.0) or 1.0),
            axis_y_scale=float(runtime_config.get('aim_axis_y_scale', 1.0) or 1.0),
            aim_deadzone_px=float(runtime_config.get('aim_deadzone_px', 12.0) or 0.0),
            near_target_slow_radius_px=float(runtime_config.get('near_target_slow_radius_px', 18.0) or 0.0),
            near_target_min_scale=float(runtime_config.get('near_target_min_scale', 0.35) or 0.0),
            fine_aim_radius_px=float(runtime_config.get('fine_aim_radius_px', 24.0) or 0.0),
            micro_move_hold_radius_px=float(runtime_config.get('micro_move_hold_radius_px', 7.0) or 0.0),
            micro_move_min_step_px=float(runtime_config.get('micro_move_min_step_px', 2.0) or 0.0),
            move_async_enabled=bool(runtime_config.get('move_async_enabled', True)),
            move_substep_max_px=float(runtime_config.get('move_substep_max_px', 0.0) or 0.0),
            move_step_sleep_ms=float(runtime_config.get('move_step_sleep_ms', 0.0) or 0.0),
            max_step=int(runtime_config.get('control_max_step', 127) or 127),
            luma_enabled=bool(runtime_config.get('luma_enabled', True)),
            move_history=move_history,
            async_mover=loop_context.async_mover,
        )
        if key_state_error and not current_aim_stats.get('error'):
            current_aim_stats['error'] = key_state_error
        current_aim_stats['active_target_class'] = active_target_class
        current_aim_stats['active_target_classes'] = list(active_target_classes)
        current_aim_stats['switch_key'] = switch_key
        current_aim_stats['target_class_changed'] = bool(class_changed and switch_key)
        aim_stats = current_aim_stats
    except Exception as exc:
        aim_stats = make_empty_aim_stats()
        aim_stats['active_target_class'] = active_target_class
        aim_stats['active_target_classes'] = list(active_target_classes)
        aim_stats['switch_key'] = switch_key
        aim_stats['target_class_changed'] = bool(class_changed and switch_key)
        aim_stats['key_query_ms'] = float(key_query_ms)
        aim_stats['error'] = str(exc) if not key_state_error else f'{key_state_error} | {exc}'

    loop_context.previous_pressed_keys = set(pressed_keys)
    return aim_stats, active_target_class, active_target_classes, (time.perf_counter() - loop_started) * 1000.0
