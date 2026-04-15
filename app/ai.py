from __future__ import annotations

import argparse
import json
import math
import queue
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from control_client import PassthroughController
from hdmi_low_latency import LatestFrameCapture, auto_detect_device
from runtime_config import build_runtime_config_from_args, sanitize_runtime_patch
from single_loop_aim import (
    MouseMoveHistory,
    SingleLoopAimContext,
    parse_aim_keys,
    parse_target_class_priority,
    parse_target_hotkeys,
    run_single_loop_aim,
)

try:
    from rknnlite.api import RKNNLite
except ImportError as exc:
    raise SystemExit(
        'rknnlite is not installed. Install rknn-toolkit-lite2 on the OrangePi first.'
    ) from exc


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = ROOT / 'yolo261n-rk3588.rknn'
PRECISION_DEBUG_ROOT = ROOT / 'generated' / 'aim_precision_debug'

COCO_CLASSES = (
    'person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus', 'train', 'truck', 'boat', 'traffic light',
    'fire hydrant', 'stop sign', 'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse', 'sheep', 'cow',
    'elephant', 'bear', 'zebra', 'giraffe', 'backpack', 'umbrella', 'handbag', 'tie', 'suitcase', 'frisbee',
    'skis', 'snowboard', 'sports ball', 'kite', 'baseball bat', 'baseball glove', 'skateboard', 'surfboard',
    'tennis racket', 'bottle', 'wine glass', 'cup', 'fork', 'knife', 'spoon', 'bowl', 'banana', 'apple',
    'sandwich', 'orange', 'broccoli', 'carrot', 'hot dog', 'pizza', 'donut', 'cake', 'chair', 'couch',
    'potted plant', 'bed', 'dining table', 'toilet', 'tv', 'laptop', 'mouse', 'remote', 'keyboard',
    'cell phone', 'microwave', 'oven', 'toaster', 'sink', 'refrigerator', 'book', 'clock', 'vase',
    'scissors', 'teddy bear', 'hair drier', 'toothbrush'
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run RKNN Lite inference on latest HDMI frames.')
    parser.add_argument('--model', default=str(DEFAULT_MODEL), help='Path to .rknn model file')
    parser.add_argument('--device', default='auto', help='V4L2 device path or auto')
    parser.add_argument('--backend', default='v4l2-raw', choices=('auto', 'gstreamer', 'v4l2', 'v4l2-raw'), help='Capture backend')
    parser.add_argument('--width', type=int, default=1920, help='Requested capture width')
    parser.add_argument('--height', type=int, default=1080, help='Requested capture height')
    parser.add_argument('--fps', type=int, default=60, help='Requested capture FPS')
    parser.add_argument('--crop-width', type=int, default=500, help='Center crop width before inference')
    parser.add_argument('--crop-height', type=int, default=500, help='Center crop height before inference')
    parser.add_argument('--process-width', type=int, default=0, help='Resize cropped frame before inference, 0 keeps crop size')
    parser.add_argument('--process-height', type=int, default=0, help='Resize cropped frame before inference, 0 keeps crop size')
    parser.add_argument('--model-input-width', type=int, default=0, help='Override model input width, 0 auto-detects')
    parser.add_argument('--model-input-height', type=int, default=0, help='Override model input height, 0 auto-detects')
    parser.add_argument('--conf', type=float, default=0.25, help='Confidence threshold')
    parser.add_argument('--nms', type=float, default=0.45, help='NMS threshold')
    parser.add_argument('--show', action='store_true', help='Show annotated window')
    parser.add_argument('--show-interval', type=float, default=0.05, help='Minimum interval between window refreshes in seconds when --show is enabled')
    parser.add_argument('--aim-class', type=int, default=0, help='Target class id used for mouse following')
    parser.add_argument('--aim-classes-text', default='', help='Comma-separated prioritized target classes, front classes have higher priority')
    parser.add_argument('--target-switch-stable-frames', type=int, default=3, help='How many consecutive stable frames a new target must keep before switching to it')
    parser.add_argument('--aim-key', action='append', default=[], help='Key that enables mouse follow, repeatable. Default: KEY_LEFTSHIFT and KEY_RIGHTSHIFT')
    parser.add_argument('--aim-keys-text', default='', help='Comma-separated aim trigger keys, for example KEY_LEFTSHIFT,KEY_RIGHTSHIFT')
    parser.add_argument('--control-max-step', type=int, default=127, help='Clamp each mouse move command to this absolute step')
    parser.add_argument('--control-socket', default='/tmp/km_passthrough.sock', help='Passthrough control socket path')
    parser.add_argument('--loop-interval-ms', type=float, default=0.0, help='Fixed sleep at the end of each main loop iteration in milliseconds')
    parser.add_argument('--print-every', type=float, default=2.0, help='Stats print interval in seconds')
    parser.add_argument('--log-every-frame', action='store_true', help='Print one detailed log line for every processed frame')
    parser.add_argument('--core-mask', default='auto', choices=('auto', '0', '1', '2', '0_1_2'), help='RK3588 NPU core mask')
    parser.add_argument('--runtime-config-file', default=str(ROOT / 'config' / 'default.json'), help='JSON file used for live runtime config updates')
    parser.add_argument('--persist-config-file', default='', help='Deprecated compatibility option')
    parser.add_argument('--state-file', default=str(ROOT / '.ai_state.json'), help='JSON file used to publish current AI status')
    parser.add_argument('--state-interval', type=float, default=0.25, help='How often to flush AI state to disk in seconds')
    parser.add_argument('--preview-file', default=str(ROOT / '.ai_preview.jpg'), help='JPEG preview path for web console')
    parser.add_argument('--preview-interval', type=float, default=0.12, help='How often to refresh preview JPEG in seconds')
    parser.add_argument('--preview-max-width', type=int, default=960, help='Resize preview to this max width, 0 keeps original')
    parser.add_argument('--preview-jpeg-quality', type=int, default=80, help='JPEG quality for preview file')
    return parser.parse_args()


def resolve_model_path(path_str: str) -> Path:
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f'model not found: {path}')
    return path


def get_core_mask(mask_name: str):
    if mask_name == 'auto':
        return getattr(RKNNLite, 'NPU_CORE_AUTO', None)
    if mask_name == '0':
        return getattr(RKNNLite, 'NPU_CORE_0', None)
    if mask_name == '1':
        return getattr(RKNNLite, 'NPU_CORE_1', None)
    if mask_name == '2':
        return getattr(RKNNLite, 'NPU_CORE_2', None)
    if mask_name == '0_1_2':
        return getattr(RKNNLite, 'NPU_CORE_0_1_2', None)
    return None




def _attr_value(attr, key: str):
    if isinstance(attr, dict):
        return attr.get(key)
    return getattr(attr, key, None)


def _dims_to_hw(dims, fmt) -> tuple[int, int] | None:
    if dims is None:
        return None
    values = [int(v) for v in list(dims)]
    if len(values) < 2:
        return None
    if len(values) == 4:
        if values[0] == 1 and values[1] == 3:
            return values[2], values[3]
        if values[0] == 1 and values[3] == 3:
            return values[1], values[2]
    fmt_text = str(fmt).upper() if fmt is not None else ''
    if 'NHWC' in fmt_text and len(values) >= 4:
        return values[1], values[2]
    if 'NCHW' in fmt_text and len(values) >= 4:
        return values[2], values[3]
    positive = [v for v in values if v > 1]
    if len(positive) >= 2:
        return positive[-2], positive[-1]
    return None


def query_model_input_hw(rknn: RKNNLite) -> tuple[int, int] | None:
    query_code = getattr(RKNNLite, 'RKNN_QUERY_INPUT_ATTR', None)
    if query_code is None or not hasattr(rknn, 'query'):
        return None

    try:
        attr = rknn.query(query_code, 0)
    except Exception:
        return None

    dims = _attr_value(attr, 'dims')
    fmt = _attr_value(attr, 'fmt')
    return _dims_to_hw(dims, fmt)

def resolve_process_size(runtime_config: dict[str, object], model_input_w: int, model_input_h: int) -> tuple[int, int]:
    process_width = int(runtime_config.get('process_width', 0) or 0)
    process_height = int(runtime_config.get('process_height', 0) or 0)
    if process_width <= 0:
        process_width = int(model_input_w)
    if process_height <= 0:
        process_height = int(model_input_h)
    return process_width, process_height


def build_capture(args: argparse.Namespace, model_input_w: int, model_input_h: int) -> LatestFrameCapture:
    device = auto_detect_device() if args.device == 'auto' else args.device
    process_width = int(args.process_width) if int(args.process_width) > 0 else int(model_input_w)
    process_height = int(args.process_height) if int(args.process_height) > 0 else int(model_input_h)
    return LatestFrameCapture(
        device=device,
        width=args.width,
        height=args.height,
        fps=args.fps,
        backend=args.backend,
        crop_width=args.crop_width,
        crop_height=args.crop_height,
        process_width=process_width,
        process_height=process_height,
        # Keep draining the capture backend in the background so inference only sees the latest frame.
        use_capture_thread=True,
    )


def read_once(capture: LatestFrameCapture, last_frame_id: int):
    if hasattr(capture, 'consume_latest'):
        return capture.consume_latest()
    ok, frame, frame_id, capture_time, source_shape, crop_rect, capture_read_ms, capture_crop_ms, capture_resize_ms = capture.read_latest()
    if not ok or frame_id == last_frame_id:
        return False, None, frame_id, capture_time, source_shape, crop_rect, capture_read_ms, capture_crop_ms, capture_resize_ms
    return True, frame, frame_id, capture_time, source_shape, crop_rect, capture_read_ms, capture_crop_ms, capture_resize_ms


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + '.tmp')
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    temp_path.replace(path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + '.tmp')
    temp_path.write_text(text, encoding='utf-8')
    temp_path.replace(path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + '.tmp')
    temp_path.write_bytes(data)
    temp_path.replace(path)


def apply_runtime_capture_config(
    capture: LatestFrameCapture,
    runtime_config: dict[str, object],
    model_input_w: int,
    model_input_h: int,
) -> None:
    capture.crop_width = int(runtime_config['crop_width'])
    capture.crop_height = int(runtime_config['crop_height'])
    process_width, process_height = resolve_process_size(runtime_config, model_input_w, model_input_h)
    capture.process_width = process_width
    capture.process_height = process_height


def maybe_reload_runtime_config(
    config_path: Path,
    runtime_config: dict[str, object],
    last_mtime: float | None,
    capture: LatestFrameCapture,
    model_input_w: int,
    model_input_h: int,
) -> tuple[dict[str, object], float | None]:
    if not config_path.exists():
        return runtime_config, last_mtime

    try:
        stat = config_path.stat()
        current_mtime = stat.st_mtime
    except OSError:
        return runtime_config, last_mtime

    if last_mtime is not None and current_mtime <= last_mtime:
        return runtime_config, last_mtime

    try:
        raw = json.loads(config_path.read_text(encoding='utf-8'))
    except Exception:
        return runtime_config, current_mtime

    if not isinstance(raw, dict):
        return runtime_config, current_mtime

    updates = sanitize_runtime_patch(runtime_config, raw)
    if not updates:
        return runtime_config, current_mtime

    updated = dict(updates)
    apply_runtime_capture_config(capture, updated, model_input_w, model_input_h)
    return updated, current_mtime


def shape_to_json(value):
    if value is None:
        return None
    if isinstance(value, tuple):
        return list(value)
    return value


def softmax(x: np.ndarray, axis: int) -> np.ndarray:
    x = x - np.max(x, axis=axis, keepdims=True)
    exp_x = np.exp(x)
    return exp_x / np.sum(exp_x, axis=axis, keepdims=True)


def dfl(position: np.ndarray) -> np.ndarray:
    n, channels, grid_h, grid_w = position.shape
    bins = channels // 4
    reshaped = position.reshape(n, 4, bins, grid_h, grid_w)
    probs = softmax(reshaped, axis=2)
    acc = np.arange(bins, dtype=np.float32).reshape(1, 1, bins, 1, 1)
    return (probs * acc).sum(axis=2)


def box_process(position: np.ndarray, input_size: tuple[int, int]) -> np.ndarray:
    grid_h, grid_w = position.shape[2:4]
    col, row = np.meshgrid(np.arange(grid_w), np.arange(grid_h))
    grid = np.stack((col, row), axis=0).reshape(1, 2, grid_h, grid_w).astype(np.float32)
    stride = np.array([input_size[0] / grid_w, input_size[1] / grid_h], dtype=np.float32).reshape(1, 2, 1, 1)
    dist = dfl(position)
    box_xy1 = grid + 0.5 - dist[:, 0:2, :, :]
    box_xy2 = grid + 0.5 + dist[:, 2:4, :, :]
    return np.concatenate((box_xy1 * stride, box_xy2 * stride), axis=1)


def flatten_nchw(tensor: np.ndarray) -> np.ndarray:
    channels = tensor.shape[1]
    return tensor.transpose(0, 2, 3, 1).reshape(-1, channels)


def nms_boxes(boxes: np.ndarray, scores: np.ndarray, nms_thres: float) -> np.ndarray:
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter_w = np.maximum(0.0, xx2 - xx1)
        inter_h = np.maximum(0.0, yy2 - yy1)
        inter = inter_w * inter_h
        union = areas[i] + areas[order[1:]] - inter
        iou = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)
        order = order[np.where(iou <= nms_thres)[0] + 1]
    return np.array(keep, dtype=np.int32)




def sanitize_detections(boxes: np.ndarray, classes: np.ndarray, scores: np.ndarray):
    if boxes.size == 0 or classes.size == 0 or scores.size == 0:
        return (
            np.empty((0, 4), dtype=np.float32),
            np.empty((0,), dtype=np.int32),
            np.empty((0,), dtype=np.float32),
        )

    finite_mask = np.isfinite(boxes).all(axis=1) & np.isfinite(scores)
    boxes = boxes[finite_mask]
    classes = classes[finite_mask]
    scores = scores[finite_mask]
    if boxes.size == 0:
        return (
            np.empty((0, 4), dtype=np.float32),
            np.empty((0,), dtype=np.int32),
            np.empty((0,), dtype=np.float32),
        )

    wh_mask = (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
    boxes = boxes[wh_mask]
    classes = classes[wh_mask]
    scores = scores[wh_mask]
    if boxes.size == 0:
        return (
            np.empty((0, 4), dtype=np.float32),
            np.empty((0,), dtype=np.int32),
            np.empty((0,), dtype=np.float32),
        )

    return boxes.astype(np.float32), classes.astype(np.int32), scores.astype(np.float32)

def run_classwise_nms(boxes: np.ndarray, classes: np.ndarray, scores: np.ndarray, nms_thres: float):
    boxes, classes, scores = sanitize_detections(boxes, classes, scores)
    if boxes.size == 0:
        return boxes, classes, scores

    final_boxes: list[np.ndarray] = []
    final_classes: list[np.ndarray] = []
    final_scores: list[np.ndarray] = []
    for class_id in np.unique(classes):
        inds = np.where(classes == class_id)[0]
        keep = nms_boxes(boxes[inds], scores[inds], nms_thres)
        if keep.size == 0:
            continue
        final_boxes.append(boxes[inds][keep])
        final_classes.append(classes[inds][keep])
        final_scores.append(scores[inds][keep])
    if not final_boxes:
        return np.empty((0, 4), dtype=np.float32), np.empty((0,), dtype=np.int32), np.empty((0,), dtype=np.float32)
    return np.concatenate(final_boxes, axis=0), np.concatenate(final_classes, axis=0), np.concatenate(final_scores, axis=0)


def decode_multi_output(outputs: list[np.ndarray], input_size: tuple[int, int], conf_thres: float, nms_thres: float):
    branch_count = 3
    pair_per_branch = len(outputs) // branch_count
    if pair_per_branch not in (2, 3):
        raise RuntimeError(f'Unsupported RKNN output layout: {len(outputs)} tensors')

    all_boxes: list[np.ndarray] = []
    all_classes: list[np.ndarray] = []
    all_scores: list[np.ndarray] = []

    for branch_index in range(branch_count):
        offset = branch_index * pair_per_branch
        position = outputs[offset].astype(np.float32)
        class_probs = outputs[offset + 1].astype(np.float32)
        if pair_per_branch == 3:
            objectness = outputs[offset + 2].astype(np.float32)
        else:
            objectness = np.ones_like(class_probs[:, :1, :, :], dtype=np.float32)

        branch_boxes = box_process(position, input_size)
        branch_boxes = flatten_nchw(branch_boxes)
        branch_classes = flatten_nchw(class_probs)
        branch_objectness = flatten_nchw(objectness).reshape(-1)
        class_scores = np.max(branch_classes, axis=-1)
        classes = np.argmax(branch_classes, axis=-1)
        scores = class_scores * branch_objectness
        keep = scores >= conf_thres
        if not np.any(keep):
            continue
        all_boxes.append(branch_boxes[keep])
        all_classes.append(classes[keep])
        all_scores.append(scores[keep])

    if not all_boxes:
        return np.empty((0, 4), dtype=np.float32), np.empty((0,), dtype=np.int32), np.empty((0,), dtype=np.float32)

    boxes = np.concatenate(all_boxes, axis=0)
    classes = np.concatenate(all_classes, axis=0)
    scores = np.concatenate(all_scores, axis=0)
    return run_classwise_nms(boxes, classes, scores, nms_thres)


def decode_single_output(output: np.ndarray, conf_thres: float, nms_thres: float):
    data = np.array(output, dtype=np.float32)
    data = np.squeeze(data)
    if data.ndim != 2:
        raise RuntimeError(f'Unsupported single-output tensor shape: {data.shape}')

    if data.shape[0] < data.shape[1]:
        data = data.T

    if data.shape[1] < 6:
        raise RuntimeError(f'Single-output tensor has too few channels: {data.shape}')
    extra = data[:, 4:]

    if extra.shape[1] == len(COCO_CLASSES) + 1:
        objectness = extra[:, 0]
        class_probs = extra[:, 1:]
        class_scores = np.max(class_probs, axis=1)
        classes = np.argmax(class_probs, axis=1)
        scores = objectness * class_scores
    else:
        class_probs = extra
        scores = np.max(class_probs, axis=1)
        classes = np.argmax(class_probs, axis=1)

    keep = np.isfinite(scores) & (scores >= conf_thres)
    if not np.any(keep):
        return np.empty((0, 4), dtype=np.float32), np.empty((0,), dtype=np.int32), np.empty((0,), dtype=np.float32)

    data = data[keep]
    classes = classes[keep]
    scores = scores[keep]
    finite_rows = np.isfinite(data).all(axis=1)
    if not np.any(finite_rows):
        return np.empty((0, 4), dtype=np.float32), np.empty((0,), dtype=np.int32), np.empty((0,), dtype=np.float32)

    data = data[finite_rows]
    classes = classes[finite_rows]
    scores = scores[finite_rows]
    boxes_xywh = data[:, :4]

    boxes = np.empty_like(boxes_xywh, dtype=np.float32)
    boxes[:, 0] = boxes_xywh[:, 0] - boxes_xywh[:, 2] / 2.0
    boxes[:, 1] = boxes_xywh[:, 1] - boxes_xywh[:, 3] / 2.0
    boxes[:, 2] = boxes_xywh[:, 0] + boxes_xywh[:, 2] / 2.0
    boxes[:, 3] = boxes_xywh[:, 1] + boxes_xywh[:, 3] / 2.0
    return run_classwise_nms(boxes, classes, scores, nms_thres)


def decode_outputs(outputs: list[np.ndarray], input_size: tuple[int, int], conf_thres: float, nms_thres: float):
    if len(outputs) == 1:
        return decode_single_output(outputs[0], conf_thres, nms_thres)
    if len(outputs) >= 6:
        return decode_multi_output(outputs, input_size, conf_thres, nms_thres)
    raise RuntimeError(f'Unexpected RKNN output count: {len(outputs)}')


def scale_boxes_back(boxes: np.ndarray, scale_x: float, scale_y: float, src_shape: tuple[int, int]) -> np.ndarray:
    if boxes.size == 0:
        return boxes
    scaled = boxes.copy().astype(np.float32)
    scaled[:, [0, 2]] /= scale_x
    scaled[:, [1, 3]] /= scale_y
    src_h, src_w = src_shape
    scaled[:, [0, 2]] = np.clip(scaled[:, [0, 2]], 0, src_w - 1)
    scaled[:, [1, 3]] = np.clip(scaled[:, [1, 3]], 0, src_h - 1)
    finite_mask = np.isfinite(scaled).all(axis=1)
    return scaled[finite_mask]


def format_optional_float(value: object, digits: int = 1) -> str:
    if value is None:
        return 'na'
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 'na'
    if not np.isfinite(number):
        return 'na'
    return f'{number:.{digits}f}'


def draw_boxes(image: np.ndarray, boxes: np.ndarray, classes: np.ndarray, scores: np.ndarray) -> np.ndarray:
    canvas = image.copy()
    count = min(len(boxes), len(classes), len(scores))
    for idx in range(count):
        box = boxes[idx]
        class_id = int(classes[idx])
        score = float(scores[idx])
        if not np.isfinite(box).all() or not np.isfinite(score):
            continue
        x1, y1, x2, y2 = [int(v) for v in box]
        label = str(class_id)
        if 0 <= class_id < len(COCO_CLASSES):
            label = COCO_CLASSES[class_id]
        text = f'{label} {score:.2f}'
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(canvas, text, (x1, max(y1 - 8, 16)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
    return canvas


def build_annotated_frame(
    image: np.ndarray,
    boxes: np.ndarray,
    classes: np.ndarray,
    scores: np.ndarray,
    aim_stats: dict[str, object],
    infer_fps: float,
    total_latency_ms: float,
) -> np.ndarray:
    annotated = draw_boxes(image, boxes, classes, scores)
    frame_h, frame_w = annotated.shape[:2]
    center = (frame_w // 2, frame_h // 2)
    cv2.drawMarker(annotated, center, (255, 0, 0), cv2.MARKER_CROSS, 18, 2)

    if bool(aim_stats.get('target_found')):
        target_x = int(round(frame_w / 2.0 + float(aim_stats.get('target_offset_x', 0.0) or 0.0)))
        target_y = int(round(frame_h / 2.0 + float(aim_stats.get('target_offset_y', 0.0) or 0.0)))
        cv2.circle(annotated, (target_x, target_y), 8, (0, 255, 255), 2)
        cv2.line(annotated, center, (target_x, target_y), (0, 255, 255), 1)

    overlay_lines = [
        f'fps={infer_fps:.1f} latency={total_latency_ms:.1f}ms det={len(scores)}',
        (
            f'key={aim_stats.get("pressed_key") or "none"} '
            f'target={1 if bool(aim_stats.get("target_found")) else 0} '
            f'first={1 if bool(aim_stats.get("first_frame", False)) else 0} '
            f'stable={int(aim_stats.get("stable_frames", 0) or 0)}/{int(aim_stats.get("required_lock_frames", 0) or 0)} '
            f'move=({int(aim_stats.get("move_x", 0) or 0)},{int(aim_stats.get("move_y", 0) or 0)})'
        ),
        (
            f'offset=({format_optional_float(aim_stats.get("target_offset_x"), 1)},'
            f'{format_optional_float(aim_stats.get("target_offset_y"), 1)}) '
            f'control=({format_optional_float(aim_stats.get("control_offset_x"), 1)},'
            f'{format_optional_float(aim_stats.get("control_offset_y"), 1)})'
        ),
    ]
    y = 24
    for line in overlay_lines:
        cv2.putText(annotated, line, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 3)
        cv2.putText(annotated, line, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
        y += 22
    return annotated


def write_preview_image(path: Path, image: np.ndarray, max_width: int, jpeg_quality: int) -> bool:
    preview = image
    if max_width > 0 and image.shape[1] > max_width:
        scale = max_width / float(image.shape[1])
        preview_height = max(1, int(round(image.shape[0] * scale)))
        preview = cv2.resize(image, (max_width, preview_height), interpolation=cv2.INTER_AREA)

    quality = max(30, min(100, int(jpeg_quality)))
    ok, encoded = cv2.imencode('.jpg', preview, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + '.tmp')
    temp_path.write_bytes(encoded.tobytes())
    temp_path.replace(path)
    return True


class RenderWorker:
    def __init__(
        self,
        preview_file: Path,
        preview_max_width: int,
        preview_jpeg_quality: int,
        window_enabled: bool,
    ) -> None:
        self.preview_file = preview_file
        self.preview_max_width = preview_max_width
        self.preview_jpeg_quality = preview_jpeg_quality
        self.window_enabled = window_enabled
        self.window_name = 'ai_inference_rknn'
        self.queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.stop_requested_event = threading.Event()
        self.lock = threading.Lock()
        self.preview_status: dict[str, object] = {
            'ok': False,
            'error': 'waiting for first frame',
            'updated_at': None,
            'size': 0,
        }
        self.thread = threading.Thread(target=self._run, daemon=True, name='ai-render-worker')

    def start(self) -> 'RenderWorker':
        self.thread.start()
        return self

    def snapshot_preview_status(self) -> dict[str, object]:
        with self.lock:
            return dict(self.preview_status)

    def _set_preview_status(self, ok: bool, error: str, updated_at: float | None, size: int) -> None:
        with self.lock:
            self.preview_status = {
                'ok': ok,
                'error': error,
                'updated_at': updated_at,
                'size': size,
            }

    def submit(
        self,
        *,
        image: np.ndarray,
        boxes: np.ndarray,
        classes: np.ndarray,
        scores: np.ndarray,
        aim_stats: dict[str, object],
        infer_fps: float,
        total_latency_ms: float,
        write_preview: bool,
        show_window: bool,
    ) -> None:
        if not write_preview and not show_window:
            return
        payload = {
            'image': image,
            'boxes': boxes,
            'classes': classes,
            'scores': scores,
            'aim_stats': dict(aim_stats),
            'infer_fps': float(infer_fps),
            'total_latency_ms': float(total_latency_ms),
            'write_preview': bool(write_preview),
            'show_window': bool(show_window),
        }
        while not self.stop_event.is_set():
            try:
                self.queue.put_nowait(payload)
                return
            except queue.Full:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    return

    def should_stop(self) -> bool:
        return self.stop_requested_event.is_set()

    def _poll_window_events(self) -> None:
        if not self.window_enabled:
            return
        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord('q')):
            self.stop_requested_event.set()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                payload = self.queue.get(timeout=0.05)
            except queue.Empty:
                self._poll_window_events()
                continue

            try:
                annotated = build_annotated_frame(
                    image=payload['image'],
                    boxes=payload['boxes'],
                    classes=payload['classes'],
                    scores=payload['scores'],
                    aim_stats=payload['aim_stats'],
                    infer_fps=float(payload['infer_fps']),
                    total_latency_ms=float(payload['total_latency_ms']),
                )

                if bool(payload['write_preview']):
                    now = time.perf_counter()
                    try:
                        if write_preview_image(
                            path=self.preview_file,
                            image=annotated,
                            max_width=self.preview_max_width,
                            jpeg_quality=self.preview_jpeg_quality,
                        ):
                            self._set_preview_status(
                                ok=True,
                                error='',
                                updated_at=now,
                                size=self.preview_file.stat().st_size if self.preview_file.exists() else 0,
                            )
                        else:
                            self._set_preview_status(False, 'cv2.imencode returned false', now, 0)
                    except Exception as exc:
                        self._set_preview_status(False, str(exc), now, 0)

                if self.window_enabled and bool(payload['show_window']):
                    cv2.imshow(self.window_name, annotated)
                self._poll_window_events()
            except Exception as exc:
                self._set_preview_status(False, f'render worker: {exc}', time.perf_counter(), 0)

        if self.window_enabled:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1.0)
        if self.window_enabled:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

class AimPrecisionDebugRecorder:
    def __init__(self, root_dir: Path) -> None:
        self.preferred_root_dir = root_dir
        self.root_dir = self._resolve_writable_root(root_dir, quiet=False)
        self.queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=8)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name='aim-precision-debug-writer')
        self.lock = threading.Lock()
        self.active_session: dict[str, object] | None = None
        self.session_counter = 0
        self.last_saved_dir = ''
        self.last_save_error = ''

    def start(self) -> 'AimPrecisionDebugRecorder':
        self.thread.start()
        return self

    def snapshot_status(self) -> dict[str, object]:
        with self.lock:
            return {
                'active': bool(self.active_session is not None),
                'current_session': str(self.active_session.get('session_name', '')) if self.active_session else '',
                'root_dir': str(self.root_dir),
                'last_saved_dir': self.last_saved_dir,
                'last_save_error': self.last_save_error,
            }

    def handle_frame(
        self,
        *,
        enabled: bool,
        frame_id: int,
        frame_shape: tuple[int, int, int],
        source_shape: tuple[int, int, int] | None,
        crop_rect: tuple[int, int, int, int] | None,
        boxes: np.ndarray,
        classes: np.ndarray,
        scores: np.ndarray,
        aim_stats: dict[str, object],
        infer_fps: float,
        total_latency_ms: float,
        runtime_config: dict[str, object],
        now: float,
    ) -> None:
        key_pressed = bool(aim_stats.get('key_pressed', False))

        if enabled and key_pressed and self.active_session is None:
            self._begin_session(
                frame_id=frame_id,
                frame_shape=frame_shape,
                source_shape=source_shape,
                crop_rect=crop_rect,
                aim_stats=aim_stats,
                runtime_config=runtime_config,
                now=now,
            )

        if self.active_session is not None:
            record = self._build_record(
                session=self.active_session,
                frame_id=frame_id,
                boxes=boxes,
                classes=classes,
                scores=scores,
                aim_stats=aim_stats,
                infer_fps=infer_fps,
                total_latency_ms=total_latency_ms,
                now=now,
            )
            records = self.active_session.setdefault('records', [])
            if isinstance(records, list):
                records.append(record)
            self.active_session['end_frame_id'] = int(frame_id)
            self.active_session['end_ts'] = float(now)

        if self.active_session is not None and (not enabled or not key_pressed):
            end_reason = 'disabled' if not enabled else 'key_up'
            self._finish_active(end_reason)

    def flush_active(self, reason: str = 'shutdown') -> None:
        if self.active_session is not None:
            self._finish_active(reason)

    def stop(self) -> None:
        self.flush_active('shutdown')
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3.0)

    def _begin_session(
        self,
        *,
        frame_id: int,
        frame_shape: tuple[int, int, int],
        source_shape: tuple[int, int, int] | None,
        crop_rect: tuple[int, int, int, int] | None,
        aim_stats: dict[str, object],
        runtime_config: dict[str, object],
        now: float,
    ) -> None:
        self.root_dir = self._resolve_writable_root(self.preferred_root_dir, quiet=True)
        self.session_counter += 1
        timestamp = time.strftime('%Y%m%d_%H%M%S', time.localtime())
        millis = int(round((now - math.floor(now)) * 1000.0))
        session_name = f'{timestamp}_{millis:03d}_{self.session_counter:03d}_f{int(frame_id)}'
        self.active_session = {
            'session_name': session_name,
            'base_root': str(self.root_dir),
            'dir': str(self.root_dir / session_name),
            'start_ts': float(now),
            'end_ts': float(now),
            'start_frame_id': int(frame_id),
            'end_frame_id': int(frame_id),
            'pressed_key': str(aim_stats.get('pressed_key') or ''),
            'aim_class': int(aim_stats.get('active_target_class', runtime_config.get('aim_class', 0)) or 0),
            'frame_shape': [int(v) for v in frame_shape],
            'source_shape': [int(v) for v in source_shape] if source_shape is not None else None,
            'crop_rect': [int(v) for v in crop_rect] if crop_rect is not None else None,
            'runtime_config': dict(runtime_config),
            'records': [],
        }

    def _finish_active(self, reason: str) -> None:
        if self.active_session is None:
            return
        session = dict(self.active_session)
        session['end_reason'] = str(reason)
        self.active_session = None
        while not self.stop_event.is_set():
            try:
                self.queue.put_nowait(session)
                return
            except queue.Full:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    return

    def _run(self) -> None:
        while not self.stop_event.is_set() or not self.queue.empty():
            try:
                session = self.queue.get(timeout=0.10)
            except queue.Empty:
                continue
            try:
                saved_dir = self._write_session(session)
                with self.lock:
                    self.last_saved_dir = saved_dir
                    self.last_save_error = ''
                print(f'aim_precision_debug_saved dir={saved_dir}')
            except Exception as exc:
                with self.lock:
                    self.last_save_error = str(exc)
                print(f'aim_precision_debug_save error: {exc}')

    @staticmethod
    def _candidate_root_dirs(preferred: Path) -> list[Path]:
        candidates = [preferred]
        home = Path.home()
        candidates.extend(
            [
                home / 'generated' / 'aim_precision_debug',
                home / '.aim_precision_debug',
                Path('/tmp') / 'aim_precision_debug',
            ]
        )
        deduped: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped

    @staticmethod
    def _ensure_writable_dir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / '.write_test.tmp'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink(missing_ok=True)

    def _resolve_writable_root(self, preferred: Path, quiet: bool) -> Path:
        last_error = ''
        for candidate in self._candidate_root_dirs(preferred):
            try:
                self._ensure_writable_dir(candidate)
                if str(candidate) != str(preferred) and not quiet:
                    print(f'aim_precision_debug_root_fallback preferred={preferred} actual={candidate}')
                return candidate
            except Exception as exc:
                last_error = str(exc)
                continue
        if not quiet and last_error:
            print(f'aim_precision_debug_root_unavailable preferred={preferred} error={last_error}')
        return preferred

    @staticmethod
    def _safe_float(value: object, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not np.isfinite(number):
            return float(default)
        return number

    @staticmethod
    def _safe_int(value: object, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _pack_detections(self, boxes: np.ndarray, classes: np.ndarray, scores: np.ndarray) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        count = min(len(boxes), len(classes), len(scores))
        for idx in range(count):
            box = boxes[idx]
            score = self._safe_float(scores[idx])
            if not np.isfinite(box).all() or not np.isfinite(score):
                continue
            x1 = self._safe_float(box[0])
            y1 = self._safe_float(box[1])
            x2 = self._safe_float(box[2])
            y2 = self._safe_float(box[3])
            items.append(
                {
                    'class_id': self._safe_int(classes[idx]),
                    'score': round(score, 4),
                    'box': [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
                    'center': [round((x1 + x2) * 0.5, 2), round((y1 + y2) * 0.5, 2)],
                    'size': [round(max(0.0, x2 - x1), 2), round(max(0.0, y2 - y1), 2)],
                }
            )
        return items

    def _build_record(
        self,
        *,
        session: dict[str, object],
        frame_id: int,
        boxes: np.ndarray,
        classes: np.ndarray,
        scores: np.ndarray,
        aim_stats: dict[str, object],
        infer_fps: float,
        total_latency_ms: float,
        now: float,
    ) -> dict[str, object]:
        start_ts = self._safe_float(session.get('start_ts'), now)
        return {
            'ts': round(now, 6),
            'rel_ms': round(max(0.0, (now - start_ts) * 1000.0), 3),
            'frame_id': int(frame_id),
            'key_down': bool(aim_stats.get('key_pressed', False)),
            'pressed_key': str(aim_stats.get('pressed_key') or ''),
            'detections': int(len(scores)),
            'detections_detail': self._pack_detections(boxes, classes, scores),
            'active_target_class': self._safe_int(aim_stats.get('active_target_class')),
            'selected_target_class': self._safe_int(aim_stats.get('selected_target_class')),
            'target_found': bool(aim_stats.get('target_found', False)),
            'stable_ready': bool(aim_stats.get('stable_ready', False)),
            'stable_frames': self._safe_int(aim_stats.get('stable_frames')),
            'required_lock_frames': self._safe_int(aim_stats.get('required_lock_frames')),
            'same_visual_frame': bool(aim_stats.get('same_visual_frame', False)),
            'first_frame': bool(aim_stats.get('first_frame', False)),
            'target_score': round(self._safe_float(aim_stats.get('target_score')), 4),
            'target_offset': [
                round(self._safe_float(aim_stats.get('target_offset_x')), 3),
                round(self._safe_float(aim_stats.get('target_offset_y')), 3),
            ],
            'filtered_offset': [
                round(self._safe_float(aim_stats.get('filtered_offset_x')), 3),
                round(self._safe_float(aim_stats.get('filtered_offset_y')), 3),
            ],
            'control_offset': [
                round(self._safe_float(aim_stats.get('control_offset_x')), 3),
                round(self._safe_float(aim_stats.get('control_offset_y')), 3),
            ],
            'move': [
                self._safe_int(aim_stats.get('move_x')),
                self._safe_int(aim_stats.get('move_y')),
            ],
            'moved': bool(aim_stats.get('moved', False)),
            'auto_fired': bool(aim_stats.get('auto_fired', False)),
            'auto_fire_cmd_ms': round(self._safe_float(aim_stats.get('auto_fire_cmd_ms')), 3),
            'target_distance': round(self._safe_float(aim_stats.get('target_distance')), 3),
            'mouse_cmd_ms': round(self._safe_float(aim_stats.get('mouse_cmd_ms')), 3),
            'infer_fps': round(self._safe_float(infer_fps), 3),
            'total_latency_ms': round(self._safe_float(total_latency_ms), 3),
            'aim_error': str(aim_stats.get('error') or ''),
        }

    def _build_summary(self, session: dict[str, object]) -> dict[str, object]:
        raw_records = session.get('records', [])
        records = raw_records if isinstance(raw_records, list) else []
        duration_ms = max(0.0, (self._safe_float(session.get('end_ts')) - self._safe_float(session.get('start_ts'))) * 1000.0)
        cumulative_x = 0.0
        cumulative_y = 0.0
        max_target_distance = 0.0
        max_control_distance = 0.0
        max_move_step = 0.0
        max_detections = 0
        target_found_frames = 0
        stable_ready_frames = 0
        moved_frames = 0
        auto_fired_frames = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            target = record.get('target_offset') or [0.0, 0.0]
            control = record.get('control_offset') or [0.0, 0.0]
            move = record.get('move') or [0, 0]
            max_target_distance = max(max_target_distance, math.hypot(self._safe_float(target[0]), self._safe_float(target[1])))
            max_control_distance = max(max_control_distance, math.hypot(self._safe_float(control[0]), self._safe_float(control[1])))
            max_move_step = max(max_move_step, math.hypot(self._safe_float(move[0]), self._safe_float(move[1])))
            cumulative_x += self._safe_float(move[0])
            cumulative_y += self._safe_float(move[1])
            max_detections = max(max_detections, self._safe_int(record.get('detections')))
            target_found_frames += 1 if bool(record.get('target_found', False)) else 0
            stable_ready_frames += 1 if bool(record.get('stable_ready', False)) else 0
            moved_frames += 1 if bool(record.get('moved', False)) else 0
            auto_fired_frames += 1 if bool(record.get('auto_fired', False)) else 0

        return {
            'session_name': str(session.get('session_name') or ''),
            'dir': str(session.get('dir') or ''),
            'start_frame_id': self._safe_int(session.get('start_frame_id')),
            'end_frame_id': self._safe_int(session.get('end_frame_id')),
            'start_ts': round(self._safe_float(session.get('start_ts')), 6),
            'end_ts': round(self._safe_float(session.get('end_ts')), 6),
            'duration_ms': round(duration_ms, 3),
            'pressed_key': str(session.get('pressed_key') or ''),
            'aim_class': self._safe_int(session.get('aim_class')),
            'end_reason': str(session.get('end_reason') or ''),
            'frame_shape': session.get('frame_shape'),
            'source_shape': session.get('source_shape'),
            'crop_rect': session.get('crop_rect'),
            'records': int(len(records)),
            'target_found_frames': int(target_found_frames),
            'stable_ready_frames': int(stable_ready_frames),
            'moved_frames': int(moved_frames),
            'auto_fired_frames': int(auto_fired_frames),
            'max_detections': int(max_detections),
            'max_target_distance': round(max_target_distance, 3),
            'max_control_distance': round(max_control_distance, 3),
            'max_move_step': round(max_move_step, 3),
            'net_move': [round(cumulative_x, 3), round(cumulative_y, 3)],
            'runtime_config': dict(session.get('runtime_config') or {}),
        }

    def _render_summary_text(self, summary: dict[str, object]) -> str:
        lines = [
            f"session_name={summary.get('session_name', '')}",
            f"start_frame_id={summary.get('start_frame_id', 0)}",
            f"end_frame_id={summary.get('end_frame_id', 0)}",
            f"duration_ms={format_optional_float(summary.get('duration_ms'), 3)}",
            f"pressed_key={summary.get('pressed_key', '')}",
            f"aim_class={summary.get('aim_class', 0)}",
            f"end_reason={summary.get('end_reason', '')}",
            f"records={summary.get('records', 0)}",
            f"target_found_frames={summary.get('target_found_frames', 0)}",
            f"stable_ready_frames={summary.get('stable_ready_frames', 0)}",
            f"moved_frames={summary.get('moved_frames', 0)}",
            f"max_detections={summary.get('max_detections', 0)}",
            f"max_target_distance={format_optional_float(summary.get('max_target_distance'), 3)}",
            f"max_control_distance={format_optional_float(summary.get('max_control_distance'), 3)}",
            f"max_move_step={format_optional_float(summary.get('max_move_step'), 3)}",
            f"net_move=({format_optional_float((summary.get('net_move') or [0.0, 0.0])[0], 3)},{format_optional_float((summary.get('net_move') or [0.0, 0.0])[1], 3)})",
        ]
        return '\n'.join(lines) + '\n'

    def _draw_axes(self, canvas: np.ndarray, center: tuple[int, int], radius: int) -> None:
        cx, cy = center
        color = (220, 220, 220)
        cv2.line(canvas, (cx - radius, cy), (cx + radius, cy), color, 1)
        cv2.line(canvas, (cx, cy - radius), (cx, cy + radius), color, 1)
        for ratio in (0.25, 0.5, 0.75, 1.0):
            step = int(round(radius * ratio))
            cv2.circle(canvas, center, step, (240, 240, 240), 1)

    def _draw_path_panel(
        self,
        canvas: np.ndarray,
        points_map: list[tuple[str, list[tuple[float, float]], tuple[int, int, int]]],
        title: str,
    ) -> None:
        height, width = canvas.shape[:2]
        center = (width // 2, height // 2)
        radius = max(40, min(width, height) // 2 - 40)
        self._draw_axes(canvas, center, radius)

        max_extent = 1.0
        for _, points, _ in points_map:
            for x, y in points:
                max_extent = max(max_extent, abs(float(x)), abs(float(y)))
        scale = radius / max_extent if max_extent > 1e-6 else 1.0

        for name, points, color in points_map:
            if len(points) < 2:
                continue
            prev = None
            for point_x, point_y in points:
                px = int(round(center[0] + float(point_x) * scale))
                py = int(round(center[1] + float(point_y) * scale))
                current = (px, py)
                if prev is not None:
                    cv2.line(canvas, prev, current, color, 2)
                prev = current
            start = (
                int(round(center[0] + float(points[0][0]) * scale)),
                int(round(center[1] + float(points[0][1]) * scale)),
            )
            end = (
                int(round(center[0] + float(points[-1][0]) * scale)),
                int(round(center[1] + float(points[-1][1]) * scale)),
            )
            cv2.circle(canvas, start, 4, (0, 180, 0), -1)
            cv2.circle(canvas, end, 4, (0, 0, 255), -1)
            cv2.putText(canvas, name, (20, 28 + 22 * points_map.index((name, points, color))), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

        cv2.putText(canvas, title, (20, height - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (30, 30, 30), 2)

    def _build_plot(self, summary: dict[str, object], records: list[dict[str, object]]) -> np.ndarray:
        canvas = np.full((900, 1600, 3), 255, dtype=np.uint8)
        left = canvas[:, :800]
        right = canvas[:, 800:]

        target_points: list[tuple[float, float]] = []
        control_points: list[tuple[float, float]] = []
        cumulative_points: list[tuple[float, float]] = [(0.0, 0.0)]
        cumulative_x = 0.0
        cumulative_y = 0.0
        for record in records:
            if not isinstance(record, dict):
                continue
            target = record.get('target_offset') or [0.0, 0.0]
            control = record.get('control_offset') or [0.0, 0.0]
            move = record.get('move') or [0, 0]
            if bool(record.get('target_found', False)):
                target_points.append((self._safe_float(target[0]), self._safe_float(target[1])))
                control_points.append((self._safe_float(control[0]), self._safe_float(control[1])))
            cumulative_x += self._safe_float(move[0])
            cumulative_y += self._safe_float(move[1])
            cumulative_points.append((cumulative_x, cumulative_y))

        self._draw_path_panel(
            left,
            [
                ('target_offset', target_points, (0, 180, 255)),
                ('control_offset', control_points, (255, 120, 0)),
            ],
            'Target / Control Offset Path',
        )
        self._draw_path_panel(
            right,
            [('cumulative_move', cumulative_points, (180, 0, 180))],
            'Cumulative Mouse Move Path',
        )

        title = (
            f"{summary.get('session_name', '')}  "
            f"frames={summary.get('records', 0)}  "
            f"duration_ms={format_optional_float(summary.get('duration_ms'), 1)}  "
            f"max_target={format_optional_float(summary.get('max_target_distance'), 1)}  "
            f"max_move={format_optional_float(summary.get('max_move_step'), 1)}"
        )
        cv2.putText(canvas, title, (24, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (30, 30, 30), 2)
        return canvas

    def _write_session(self, session: dict[str, object]) -> str:
        session_name = str(session.get('session_name') or 'session')
        preferred_root = Path(str(session.get('base_root') or self.preferred_root_dir))
        active_root = self._resolve_writable_root(preferred_root, quiet=False)
        self.root_dir = active_root
        session_dir = active_root / session_name
        session['base_root'] = str(active_root)
        session['dir'] = str(session_dir)
        session_dir.mkdir(parents=True, exist_ok=True)
        raw_records = session.get('records', [])
        records = raw_records if isinstance(raw_records, list) else []

        summary = self._build_summary(session)
        atomic_write_json(session_dir / 'summary.json', summary)
        atomic_write_text(session_dir / 'summary.txt', self._render_summary_text(summary))

        jsonl = '\n'.join(json.dumps(record, ensure_ascii=False) for record in records)
        if jsonl:
            jsonl += '\n'
        atomic_write_text(session_dir / 'trace.jsonl', jsonl)

        plot = self._build_plot(summary, records)
        ok, encoded = cv2.imencode('.png', plot)
        if ok:
            atomic_write_bytes(session_dir / 'trajectory.png', encoded.tobytes())

        return str(session_dir)


def infer_model_input_hw_from_outputs(outputs: list[np.ndarray]) -> tuple[int, int] | None:
    if not outputs:
        return None

    output_shapes = [tuple(int(v) for v in np.array(output).shape) for output in outputs]
    if len(output_shapes) == 1 and len(output_shapes[0]) == 3:
        shape = output_shapes[0]
        points_to_size = {
            1344: 256,
            2100: 320,
            3549: 416,
            5376: 512,
            8400: 640,
            18900: 960,
            33600: 1280,
        }
        positive = [int(v) for v in shape if int(v) > 1]
        for dim in positive:
            if dim in points_to_size:
                size = points_to_size[int(dim)]
                return size, size

    grid_candidates: list[int] = []
    for shape in output_shapes:
        if len(shape) < 4:
            continue
        h = int(shape[-2])
        w = int(shape[-1])
        if h > 0 and w > 0 and abs(h - w) <= 2:
            grid_candidates.append(max(h, w))
    if not grid_candidates:
        return None

    size = max(grid_candidates) * 8
    if size <= 0:
        return None
    return int(size), int(size)

def infer_model_input_hw(model_path: Path) -> tuple[int, int]:
    model_name = model_path.name.lower()
    for candidate in (1280, 960, 800, 768, 736, 672, 640, 512, 416, 320, 256):
        if str(candidate) in model_name:
            return candidate, candidate
    return 640, 640


def prepare_input(frame: np.ndarray, input_hw: tuple[int, int]) -> tuple[np.ndarray, float, float]:
    input_h, input_w = input_hw
    src_h, src_w = frame.shape[:2]
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    if src_h == input_h and src_w == input_w:
        return rgb, 1.0, 1.0
    resized = cv2.resize(rgb, (input_w, input_h), interpolation=cv2.INTER_LINEAR)
    scale_x = input_w / float(src_w)
    scale_y = input_h / float(src_h)
    return resized, scale_x, scale_y


def run_rknn_inference(rknn: RKNNLite, image: np.ndarray) -> tuple[list[np.ndarray], float, float]:
    wrap_started = time.perf_counter()
    batched = np.expand_dims(image, axis=0)
    infer_started = time.perf_counter()
    outputs = rknn.inference(inputs=[batched])
    rknn_infer_ms = (time.perf_counter() - infer_started) * 1000.0
    outputs_np = [np.array(output) for output in outputs]
    infer_wrap_ms = (time.perf_counter() - wrap_started) * 1000.0 - rknn_infer_ms
    return outputs_np, max(0.0, rknn_infer_ms), max(0.0, infer_wrap_ms)


def init_rknn(model_path: Path, core_mask_name: str) -> RKNNLite:
    rknn = RKNNLite()
    ret = rknn.load_rknn(str(model_path))
    if ret != 0:
        raise RuntimeError(f'load_rknn failed: {ret}')
    core_mask = get_core_mask(core_mask_name)
    if core_mask is None:
        ret = rknn.init_runtime()
    else:
        ret = rknn.init_runtime(core_mask=core_mask)
    if ret != 0:
        raise RuntimeError(f'init_runtime failed: {ret}')
    return rknn


def main() -> int:
    args = parse_args()
    model_path = resolve_model_path(args.model)
    preview_file = Path(args.preview_file).expanduser().resolve()
    fallback_h, fallback_w = infer_model_input_hw(model_path)
    print(f'Loading RKNN model: {model_path}')
    rknn = init_rknn(model_path, args.core_mask)

    runtime_hw = query_model_input_hw(rknn)
    if runtime_hw is not None:
        model_input_h, model_input_w = runtime_hw
        input_source = 'runtime'
    else:
        model_input_h, model_input_w = fallback_h, fallback_w
        input_source = 'filename-fallback'

    if args.model_input_width > 0 and args.model_input_height > 0:
        model_input_w = args.model_input_width
        model_input_h = args.model_input_height
        input_source = 'manual-override'

    print(f'Model input: {model_input_w}x{model_input_h} ({input_source})')

    capture = build_capture(args, model_input_w, model_input_h).start()
    runtime_config_file = Path(args.runtime_config_file).expanduser().resolve()
    state_file = Path(args.state_file).expanduser().resolve()
    runtime_config = build_runtime_config_from_args(args)
    if args.aim_keys_text.strip():
        runtime_config['aim_keys_text'] = args.aim_keys_text.strip()
    elif args.aim_key:
        runtime_config['aim_keys_text'] = ','.join(args.aim_key)
    if runtime_config_file.exists():
        try:
            persisted = json.loads(runtime_config_file.read_text(encoding='utf-8'))
            if isinstance(persisted, dict):
                runtime_config = sanitize_runtime_patch(runtime_config, persisted)
        except Exception:
            pass
    runtime_config_mtime = runtime_config_file.stat().st_mtime if runtime_config_file.exists() else None
    apply_runtime_capture_config(capture, runtime_config, model_input_w, model_input_h)

    controller = PassthroughController(args.control_socket)
    move_history = MouseMoveHistory()
    single_loop_aim = SingleLoopAimContext()
    aim_keys = parse_aim_keys(runtime_config.get('aim_keys_text', ''))
    target_hotkeys = parse_target_hotkeys(runtime_config.get('aim_target_keys_text', ''))
    target_classes = parse_target_class_priority(runtime_config.get('aim_classes_text', ''), fallback_class=int(runtime_config.get('aim_class', 0) or 0))
    last_lock_log_at = 0.0
    last_state_write = 0.0
    last_preview_submit = 0.0
    last_window_submit = 0.0
    single_loop_aim.movement_generator.configure(runtime_config)
    render_worker = RenderWorker(
        preview_file=preview_file,
        preview_max_width=args.preview_max_width,
        preview_jpeg_quality=args.preview_jpeg_quality,
        window_enabled=args.show,
    ).start()
    precision_debug_recorder = AimPrecisionDebugRecorder(PRECISION_DEBUG_ROOT).start()
    preview_status = render_worker.snapshot_preview_status()
    precision_debug_status = precision_debug_recorder.snapshot_status()
    latest_state: dict[str, object] = {
        'running': True,
        'ts': time.perf_counter(),
        'config': dict(runtime_config),
            'status': {
                'backend': capture.active_backend,
                'model': str(model_path),
                'model_input_width': model_input_w,
                'model_input_height': model_input_h,
                'input_source': input_source,
                'aim_keys': aim_keys,
                'aim_target_hotkeys': {str(key): value for key, value in target_hotkeys.items()},
                'aim_target_classes': list(target_classes),
                'aim_classes_text': ','.join(str(value) for value in target_classes),
                'aim_class': int(runtime_config.get('aim_class', 0) or 0),
                'preview_file': str(preview_file),
            'preview': dict(preview_status),
            'precision_debug': dict(precision_debug_status),
        },
    }
    atomic_write_json(state_file, latest_state)
    print(f'Capture backend: {capture.active_backend}')
    print(f'Aim keys: {aim_keys}')
    print(f'Target priority classes: {target_classes}')
    print(f'Target hotkeys: {target_hotkeys}')
    print('Press ESC or q to quit.')

    infer_count = 0
    last_stat_at = time.perf_counter()
    last_frame_id = 0
    stale_frame_drops = 0
    last_stale_frame_age_ms = 0.0
    last_stale_log_at = 0.0
    printed_shapes = False

    try:
        while True:
            runtime_config, runtime_config_mtime = maybe_reload_runtime_config(
                config_path=runtime_config_file,
                runtime_config=runtime_config,
                last_mtime=runtime_config_mtime,
                capture=capture,
                model_input_w=model_input_w,
                model_input_h=model_input_h,
            )
            current_aim_keys = parse_aim_keys(runtime_config.get('aim_keys_text', ''))
            if current_aim_keys != aim_keys:
                aim_keys = current_aim_keys
                print(f'Aim keys updated: {aim_keys}')
            current_target_hotkeys = parse_target_hotkeys(runtime_config.get('aim_target_keys_text', ''))
            if current_target_hotkeys != target_hotkeys:
                target_hotkeys = current_target_hotkeys
                print(f'Target hotkeys updated: {target_hotkeys}')
            current_target_classes = parse_target_class_priority(runtime_config.get('aim_classes_text', ''), fallback_class=int(runtime_config.get('aim_class', 0) or 0))
            if current_target_classes != target_classes:
                target_classes = current_target_classes
                print(f'Target priority classes updated: {target_classes}')
            read_started = time.perf_counter()
            ok, frame, frame_id, capture_time, source_shape, crop_rect, capture_read_ms, capture_crop_ms, capture_resize_ms = read_once(capture, last_frame_id)
            read_ms = (time.perf_counter() - read_started) * 1000.0
            if not ok:
                time.sleep(0.001)
                continue

            frame_received_at = time.perf_counter()
            frame_age_ms = max(0.0, (frame_received_at - capture_time) * 1000.0)
            last_frame_id = frame_id
            max_frame_age_ms = max(0.0, float(runtime_config.get('max_frame_age_ms', 80.0) or 0.0))
            if max_frame_age_ms > 0.0 and frame_age_ms > max_frame_age_ms:
                stale_frame_drops += 1
                last_stale_frame_age_ms = frame_age_ms
                move_history.clear()
                single_loop_aim.aim_state.reset()
                single_loop_aim.chris_controller.reset()
                if bool(runtime_config['log_every_frame']) or (frame_received_at - last_stale_log_at >= 0.5):
                    print(
                        f'skip_stale_frame '
                        f'frame_id={frame_id} '
                        f'frame_age_ms={frame_age_ms:.1f} '
                        f'max_frame_age_ms={max_frame_age_ms:.1f} '
                        f'dropped={stale_frame_drops}'
                    )
                    last_stale_log_at = frame_received_at
                time.sleep(0.001)
                continue

            preprocess_started = time.perf_counter()
            input_tensor, scale_x, scale_y = prepare_input(frame, (model_input_h, model_input_w))
            preprocess_ms = (time.perf_counter() - preprocess_started) * 1000.0

            infer_ms = 0.0
            rknn_infer_ms = 0.0
            infer_wrap_ms = 0.0
            outputs, current_rknn_infer_ms, current_infer_wrap_ms = run_rknn_inference(rknn, input_tensor)
            rknn_infer_ms += current_rknn_infer_ms
            infer_wrap_ms += current_infer_wrap_ms
            infer_ms += current_rknn_infer_ms + current_infer_wrap_ms

            if not printed_shapes:
                print(f'RKNN output_count={len(outputs)} output_shapes={[tuple(o.shape) for o in outputs]}')
                inferred_hw = infer_model_input_hw_from_outputs(outputs)
                should_recheck_input = input_source in {'filename-fallback', 'manual-override'} or (args.model_input_width == 0 and args.model_input_height == 0)
                if should_recheck_input and inferred_hw is not None and inferred_hw != (model_input_h, model_input_w):
                    model_input_h, model_input_w = inferred_hw
                    input_source = 'output-shape-inferred'
                    print(f'Model input corrected to: {model_input_w}x{model_input_h} ({input_source})')

                    apply_runtime_capture_config(capture, runtime_config, model_input_w, model_input_h)

                    preprocess_started = time.perf_counter()
                    input_tensor, scale_x, scale_y = prepare_input(frame, (model_input_h, model_input_w))
                    preprocess_ms += (time.perf_counter() - preprocess_started) * 1000.0

                    outputs, current_rknn_infer_ms, current_infer_wrap_ms = run_rknn_inference(rknn, input_tensor)
                    rknn_infer_ms += current_rknn_infer_ms
                    infer_wrap_ms += current_infer_wrap_ms
                    infer_ms += current_rknn_infer_ms + current_infer_wrap_ms
                    print(f'RKNN output_count={len(outputs)} output_shapes={[tuple(o.shape) for o in outputs]}')
                if outputs:
                    sample = np.array(outputs[0], dtype=np.float32)
                    finite = sample[np.isfinite(sample)]
                    if finite.size > 0:
                        print(
                            f'RKNN output0_stats=min={float(finite.min()):.4f} '
                            f'max={float(finite.max()):.4f} mean={float(finite.mean()):.4f}'
                        )
                    else:
                        print('RKNN output0_stats=no finite values')
                printed_shapes = True

            postprocess_started = time.perf_counter()
            boxes, classes, scores = decode_outputs(
                outputs=outputs,
                input_size=(model_input_w, model_input_h),
                conf_thres=float(runtime_config['conf']),
                nms_thres=float(runtime_config['nms']),
            )
            boxes = scale_boxes_back(boxes, scale_x, scale_y, frame.shape[:2])
            boxes, classes, scores = sanitize_detections(boxes, classes, scores)
            postprocess_ms = (time.perf_counter() - postprocess_started) * 1000.0

            aim_stats, active_target_class, active_target_classes, aim_ms = run_single_loop_aim(
                controller=controller,
                move_history=move_history,
                loop_context=single_loop_aim,
                runtime_config=runtime_config,
                frame_id=int(frame_id),
                frame_shape=frame.shape,
                boxes=boxes,
                classes=classes,
                scores=scores,
            )
            active_target_classes_text = ','.join(str(value) for value in active_target_classes)
            if (
                active_target_class != int(runtime_config.get('aim_class', 0) or 0)
                or str(runtime_config.get('aim_classes_text', '') or '').strip() != active_target_classes_text
            ):
                runtime_config['aim_class'] = active_target_class
                runtime_config['aim_classes_text'] = active_target_classes_text

            infer_count += 1
            now = time.perf_counter()
            total_latency_ms = max(0.0, (now - capture_time) * 1000.0)
            should_print = bool(runtime_config['log_every_frame']) or float(runtime_config['print_every']) <= 0 or (now - last_stat_at >= float(runtime_config['print_every']))
            elapsed = max(now - last_stat_at, 1e-6)
            infer_fps_value = float(infer_count / elapsed)
            precision_debug_recorder.handle_frame(
                enabled=bool(runtime_config.get('aim_precision_debug_enabled', False)),
                frame_id=frame_id,
                frame_shape=frame.shape,
                source_shape=source_shape,
                crop_rect=crop_rect,
                boxes=boxes,
                classes=classes,
                scores=scores,
                aim_stats=aim_stats,
                infer_fps=infer_fps_value,
                total_latency_ms=total_latency_ms,
                runtime_config=runtime_config,
                now=now,
            )

            should_render_preview = args.preview_interval <= 0 or (now - last_preview_submit >= args.preview_interval)
            show_interval = max(0.0, float(args.show_interval))
            should_show_window = bool(args.show) and (show_interval <= 0.0 or (now - last_window_submit >= show_interval))
            if should_show_window or should_render_preview:
                render_worker.submit(
                    image=frame,
                    boxes=boxes,
                    classes=classes,
                    scores=scores,
                    aim_stats=aim_stats,
                    infer_fps=infer_fps_value,
                    total_latency_ms=total_latency_ms,
                    write_preview=should_render_preview,
                    show_window=should_show_window,
                )
                if should_render_preview:
                    last_preview_submit = now
                if should_show_window:
                    last_window_submit = now
            preview_status = render_worker.snapshot_preview_status()
            precision_debug_status = precision_debug_recorder.snapshot_status()

            latest_state = {
                'running': True,
                'ts': now,
                'config': dict(runtime_config),
                'status': {
                    'backend': capture.active_backend,
                    'model': str(model_path),
                    'model_input_width': model_input_w,
                    'model_input_height': model_input_h,
                    'input_source': input_source,
                    'aim_keys': aim_keys,
                    'aim_target_hotkeys': {str(key): value for key, value in target_hotkeys.items()},
                    'aim_target_classes': list(active_target_classes),
                    'aim_classes_text': str(runtime_config.get('aim_classes_text', '') or ''),
                    'aim_class': int(runtime_config.get('aim_class', 0) or 0),
                    'preview_file': str(preview_file),
                    'preview': dict(preview_status),
                    'precision_debug': dict(precision_debug_status),
                    'frame_id': frame_id,
                    'detections': int(len(scores)),
                    'infer_fps': infer_fps_value,
                    'read_ms': read_ms,
                    'capture_read_ms': capture_read_ms,
                    'capture_crop_ms': capture_crop_ms,
                    'capture_resize_ms': capture_resize_ms,
                    'frame_age_ms': frame_age_ms,
                    'stale_frame_drops': int(stale_frame_drops),
                    'last_stale_frame_age_ms': float(last_stale_frame_age_ms),
                    'max_frame_age_ms': max_frame_age_ms,
                    'preprocess_ms': preprocess_ms,
                    'infer_ms': infer_ms,
                    'rknn_infer_ms': rknn_infer_ms,
                    'infer_wrap_ms': infer_wrap_ms,
                    'postprocess_ms': postprocess_ms,
                    'aim_ms': aim_ms,
                    'mouse_cmd_ms': float(aim_stats['mouse_cmd_ms']),
                    'key_state_ms': float(aim_stats['key_query_ms']),
                    'total_latency_ms': total_latency_ms,
                    'capture_shape': shape_to_json(source_shape),
                    'crop_rect': shape_to_json(crop_rect),
                    'infer_shape': shape_to_json(frame.shape),
                    'aim': {
                        'pressed_key': aim_stats['pressed_key'],
                        'key_down': bool(aim_stats['key_pressed']),
                        'active_target_class': active_target_class,
                        'active_target_classes': list(active_target_classes),
                        'selected_target_class': int(aim_stats.get('selected_target_class', -1) or -1),
                        'switch_key': aim_stats.get('switch_key', ''),
                        'target_class_changed': bool(aim_stats.get('target_class_changed', False)),
                        'target_found': bool(aim_stats['target_found']),
                        'stable_ready': bool(aim_stats['stable_ready']),
                        'stable_frames': int(aim_stats.get('stable_frames', 0) or 0),
                        'required_lock_frames': int(aim_stats.get('required_lock_frames', 0) or 0),
                        'first_frame': bool(aim_stats.get('first_frame', False)),
                        'same_visual_frame': bool(aim_stats.get('same_visual_frame', False)),
                        'move_x': int(aim_stats['move_x']),
                        'move_y': int(aim_stats['move_y']),
                        'auto_fired': bool(aim_stats.get('auto_fired', False)),
                        'target_offset_x': float(aim_stats['target_offset_x']),
                        'target_offset_y': float(aim_stats['target_offset_y']),
                        'filtered_offset_x': float(aim_stats['filtered_offset_x']),
                        'filtered_offset_y': float(aim_stats['filtered_offset_y']),
                        'control_offset_x': float(aim_stats.get('control_offset_x', 0.0) or 0.0),
                        'control_offset_y': float(aim_stats.get('control_offset_y', 0.0) or 0.0),
                        'target_score': float(aim_stats['target_score']),
                        'target_distance': float(aim_stats.get('target_distance', 0.0) or 0.0),
                        'mouse_cmd_ms': float(aim_stats.get('mouse_cmd_ms', 0.0) or 0.0),
                        'auto_fire_cmd_ms': float(aim_stats.get('auto_fire_cmd_ms', 0.0) or 0.0),
                        'error': aim_stats['error'],
                    },
                },
            }
            if now - last_state_write >= args.state_interval:
                atomic_write_json(state_file, latest_state)
                last_state_write = now

            lock_debug_due = bool(aim_stats["key_pressed"])
            should_print_lock = bool(runtime_config["log_every_frame"]) or (
                lock_debug_due
                and not bool(runtime_config.get("aim_precision_debug_enabled", False))
                and (now - last_lock_log_at >= 0.25)
            )

            if should_print_lock:
                print(
                    f'lock_debug '
                    f'frame_id={frame_id} '
                    f'lat={total_latency_ms:.1f} '
                    f'det={len(scores)} '
                    f'key={1 if bool(aim_stats["key_pressed"]) else 0} '
                    f'found={1 if bool(aim_stats["target_found"]) else 0} '
                    f'class={int(aim_stats.get("selected_target_class", -1) or -1)} '
                    f'stable={int(aim_stats.get("stable_frames", 0) or 0)}/{int(aim_stats.get("required_lock_frames", 0) or 0)} '
                    f'same_frame={1 if bool(aim_stats.get("same_visual_frame", False)) else 0} '
                    f'first={1 if bool(aim_stats.get("first_frame", False)) else 0} '
                    f'dist={format_optional_float(aim_stats.get("target_distance"), 1)} '
                    f'tgt=({format_optional_float(aim_stats["target_offset_x"], 1)},{format_optional_float(aim_stats["target_offset_y"], 1)}) '
                    f'ctrl=({format_optional_float(aim_stats.get("control_offset_x"), 1)},{format_optional_float(aim_stats.get("control_offset_y"), 1)}) '
                    f'move=({int(aim_stats["move_x"])},{int(aim_stats["move_y"])}) '
                    f'mouse_ms={format_optional_float(aim_stats.get("mouse_cmd_ms"), 3)} '
                    f'err={aim_stats["error"] or "none"}'
                )
                last_lock_log_at = now

            if should_print:
                print(
                    f'infer_fps={infer_fps_value:.2f} '
                    f'frame_id={frame_id} '
                    f'total_latency_ms={total_latency_ms:.1f} '
                    f'frame_age_ms={frame_age_ms:.1f} '
                    f'detections={len(scores)} '
                    f'stale_drops={stale_frame_drops} '
                    f'key_down={1 if bool(aim_stats["key_pressed"]) else 0} '
                    f'pressed_key={aim_stats["pressed_key"] or "none"} '
                    f'target_found={1 if bool(aim_stats["target_found"]) else 0} '
                    f'stable_ready={1 if bool(aim_stats["stable_ready"]) else 0} '
                    f'stable_frames={int(aim_stats.get("stable_frames", 0) or 0)}/{int(aim_stats.get("required_lock_frames", 0) or 0)} '
                    f'same_visual_frame={1 if bool(aim_stats.get("same_visual_frame", False)) else 0} '
                    f'first_frame={1 if bool(aim_stats.get("first_frame", False)) else 0} '
                    f'selected_target_class={int(aim_stats.get("selected_target_class", -1) or -1)} '
                    f'target_score={format_optional_float(aim_stats["target_score"], 3)} '
                    f'moved={1 if bool(aim_stats["moved"]) else 0} '
                    f'move=({int(aim_stats["move_x"])},{int(aim_stats["move_y"])}) '
                    f'target_offset=({format_optional_float(aim_stats["target_offset_x"], 1)},{format_optional_float(aim_stats["target_offset_y"], 1)}) '
                    f'control_offset=({format_optional_float(aim_stats.get("control_offset_x"), 1)},{format_optional_float(aim_stats.get("control_offset_y"), 1)}) '
                    f'mouse_cmd_ms={format_optional_float(aim_stats.get("mouse_cmd_ms"), 3)} '
                    f'aim_error={aim_stats["error"] or "none"}'
                )
                infer_count = 0
                last_stat_at = now

            if render_worker.should_stop():
                break

            loop_interval_ms = max(0.0, float(runtime_config.get('loop_interval_ms', 0.0) or 0.0))
            if loop_interval_ms > 0.0:
                time.sleep(loop_interval_ms / 1000.0)

    except KeyboardInterrupt:
        pass
    finally:
        try:
            latest_state['running'] = False
            latest_state['ts'] = time.perf_counter()
            latest_state.setdefault('status', {})
            if isinstance(latest_state.get('status'), dict):
                latest_state['status']['precision_debug'] = precision_debug_recorder.snapshot_status()
            atomic_write_json(state_file, latest_state)
        except Exception:
            pass
        precision_debug_recorder.stop()
        render_worker.stop()
        capture.stop()
        rknn.release()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())




