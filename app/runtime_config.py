from __future__ import annotations

from typing import Any


def _field(
    name: str,
    field_type: str,
    default: Any,
    *,
    label: str,
    group: str,
    desc: str,
    hidden: bool = False,
    min: Any | None = None,
    max: Any | None = None,
    step: Any | None = None,
) -> dict[str, Any]:
    field: dict[str, Any] = {
        'name': name,
        'type': field_type,
        'default': default,
        'label': label,
        'group': group,
        'desc': desc,
    }
    if hidden:
        field['hidden'] = True
    if min is not None:
        field['min'] = min
    if max is not None:
        field['max'] = max
    if step is not None:
        field['step'] = step
    return field


RUNTIME_SCHEMA: list[dict[str, Any]] = [
    _field('conf', 'float', 0.25, min=0.0, max=1.0, step=0.01, label='检测置信度阈值', group='1. 检测与目标', desc='低于这个分数的检测框会直接丢弃。'),
    _field('nms', 'float', 0.45, min=0.0, max=1.0, step=0.01, label='NMS 阈值', group='1. 检测与目标', desc='检测框重叠过滤阈值。'),
    _field('aim_class', 'int', 0, min=0, max=999, step=1, label='目标类别 ID', group='1. 检测与目标', desc='只跟随这个类别 ID 的目标。'),
    _field('aim_classes_text', 'str', '', label='目标优先级列表', group='1. 检测与目标', desc='按优先级填写类别 ID，逗号分隔。只会在这些类别中选目标，前面的优先级更高；留空时回退到单个目标类别。'),
    _field('target_switch_stable_frames', 'int', 3, min=1, max=30, step=1, label='切目标稳定帧数', group='1. 检测与目标', desc='检测到新的候选目标后，必须连续稳定这么多帧才允许真正切换，避免被偶发帧干扰。'),
    _field('target_track_radius_px', 'float', 40.0, min=8.0, max=256.0, step=1.0, label='目标跟踪半径', group='1. 检测与目标', desc='当前候选中心与上一帧锁定中心的最大连续匹配距离。超过这个范围就视为新目标，稳定帧会重新计算。'),
    _field('target_motion_compensation_delay_ms', 'float', 35.0, min=0.0, max=200.0, step=1.0, label='移动补偿延迟 ms', group='1. 检测与目标', desc='鼠标移动发出后，延迟这么久才用于目标连续性补偿。用于避免画面尚未响应移动时提前补偿导致丢锁卡顿。'),
    _field('lock_settle_frames', 'int', 1, min=0, max=10, step=1, label='锁定建立缓冲帧', group='1. 检测与目标', desc='新目标刚完成锁定后，额外等待这么多帧再开始移动，避免第一脚就大步弹跳。0 表示关闭。'),
    _field('aim_keys_text', 'str', 'KEY_LEFTSHIFT,KEY_RIGHTSHIFT', label='触发按键列表', group='1. 检测与目标', desc='按住这些键中的任意一个时才开始移动。'),
    _field('aim_target_keys_text', 'str', '', label='目标切换热键映射', group='1. 检测与目标', desc='模型页维护的类别切换热键配置。', hidden=True),
    _field('crop_width', 'int', 500, min=0, max=4096, step=1, label='截图宽度', group='2. 画面采集', desc='从 HDMI 画面中心裁切的宽度，0 表示不裁剪。'),
    _field('crop_height', 'int', 500, min=0, max=4096, step=1, label='截图高度', group='2. 画面采集', desc='从 HDMI 画面中心裁切的高度，0 表示不裁剪。'),
    _field('process_width', 'int', 0, min=0, max=4096, step=1, label='预处理宽度', group='2. 画面采集', desc='裁切后先缩放到这个宽度，0 表示跟随模型输入。'),
    _field('process_height', 'int', 0, min=0, max=4096, step=1, label='预处理高度', group='2. 画面采集', desc='裁切后先缩放到这个高度，0 表示跟随模型输入。'),

    _field('aim_axis_x_scale', 'float', 1.0, min=0.1, max=4.0, step=0.01, label='X 轴倍率', group='3. 极简移动', desc='move_x = 原始目标 X 偏差 * 这个倍率。'),
    _field('aim_axis_y_scale', 'float', 1.0, min=0.1, max=4.0, step=0.01, label='Y 轴倍率', group='3. 极简移动', desc='move_y = 原始目标 Y 偏差 * 这个倍率。'),
    _field('auto_fire_enabled', 'bool', False, label='自动左键点击', group='3. 极简移动', desc='开启后，当目标锁定稳定且进入命中半径时，会按冷却间隔持续触发左键点击。'),
    _field('auto_fire_stable_frames', 'int', 6, min=1, max=120, step=1, label='自动点击稳定帧', group='3. 极简移动', desc='目标连续稳定这么多帧以后，才允许自动左键点击。'),
    _field('auto_fire_radius_px', 'float', 4.0, min=0.0, max=64.0, step=0.1, label='自动点击命中半径', group='3. 极简移动', desc='目标中心偏差距离进入这个半径后，才允许触发自动左键点击。0 表示只按稳定帧判断。'),
    _field('auto_fire_cooldown_ms', 'float', 250.0, min=0.0, max=5000.0, step=1.0, label='自动点击间隔 ms', group='3. 极简移动', desc='目标持续满足条件时，两次自动左键点击之间的最小间隔。'),
    _field('aim_deadzone_px', 'float', 12.0, min=0.0, max=64.0, step=0.1, label='目标静止区阈值', group='3. 极简移动', desc='目标偏差距离低于这个值时直接停止移动，并重置控制器残余状态，避免中心附近来回晃动。'),
    _field('near_target_slow_radius_px', 'float', 18.0, min=0.0, max=128.0, step=0.1, label='近点速度衰减半径', group='3. 极简移动', desc='目标进入这个半径后开始自动降速，距离越接近静止区，移动越慢，用于高灵敏度下减少开镜后的小幅摆动。0 表示关闭。'),
    _field('near_target_min_scale', 'float', 0.35, min=0.0, max=1.0, step=0.01, label='近点最小速度倍率', group='3. 极简移动', desc='目标刚进入近点速度衰减半径时的最小速度倍率。越小越稳，但也会更肉。1 表示关闭衰减效果。'),
    _field('fine_aim_radius_px', 'float', 24.0, min=0.0, max=128.0, step=0.1, label='近点限幅半径', group='3. 极简移动', desc='目标进入这个范围后，每轴移动不会超过当前剩余误差，并抑制反向推，避免小目标附近过冲。0 表示关闭。'),
    _field('micro_move_hold_radius_px', 'float', 7.0, min=0.0, max=64.0, step=0.1, label='近点微调抑制半径', group='3. 极简移动', desc='目标已经足够接近中心时，进入这个半径后会额外抑制很小的最终移动，避免锁到目标身上后还在轻微摆动。0 表示关闭。'),
    _field('micro_move_min_step_px', 'float', 2.0, min=0.0, max=16.0, step=0.1, label='最小有效步长', group='3. 极简移动', desc='当目标在近点微调抑制半径内时，最终移动距离低于这个值就直接不发送，避免 1 到 2 像素的小步来回修正。0 表示关闭。'),
    _field('move_async_enabled', 'bool', True, label='异步平滑移动', group='3. 极简移动', desc='开启后，拆分后的小步移动由后台线程发送，主推理循环不会等待每个小步 sleep 完成。'),
    _field('move_substep_max_px', 'float', 0.0, min=0.0, max=32.0, step=0.1, label='移动小步最大像素', group='3. 极简移动', desc='贝塞尔每一段会继续拆成不超过这个长度的小步发送。比如 2 表示每小步大约 2 像素。0 表示不拆分。'),
    _field('move_step_sleep_ms', 'float', 0.0, min=0.0, max=100.0, step=0.1, label='每小步后休眠 ms', group='3. 极简移动', desc='每次发送拆分后的小 mouse_move 后强制 sleep 的时长。配合移动小步最大像素可实现 2 像素 sleep 1ms 这类平滑移动。0 表示不额外休眠。'),
    _field('control_max_step', 'int', 127, min=1, max=127, step=1, label='单帧步长上限', group='3. 极简移动', desc='最终发送给控制端的单轴最大步长。'),
    _field('bezier_segment_dist_1', 'float', 3.0, min=0.0, max=512.0, step=0.1, label='贝塞尔分段阈值 1', group='3. 极简移动', desc='距离低于这个值时只发 1 段。'),
    _field('bezier_segment_dist_2', 'float', 8.0, min=0.0, max=512.0, step=0.1, label='贝塞尔分段阈值 2', group='3. 极简移动', desc='距离低于这个值时发 2 段。'),
    _field('bezier_segment_dist_3', 'float', 20.0, min=0.0, max=512.0, step=0.1, label='贝塞尔分段阈值 3', group='3. 极简移动', desc='距离低于这个值时发 3 段，更大时发 4 段。'),
    _field('bezier_curve_min_distance', 'float', 15.0, min=0.0, max=512.0, step=0.1, label='贝塞尔弯曲启用距离', group='3. 极简移动', desc='只有距离超过这个值才加横向弯曲。设为 0 仅在弯曲强度非 0 时生效。'),
    _field('bezier_curve_scale', 'float', 0.0, min=0.0, max=0.2, step=0.001, label='贝塞尔弯曲强度', group='3. 极简移动', desc='曲线横向偏移强度。0 表示关闭，优先保证直线稳定。'),
    _field('bezier_jitter_min_distance', 'float', 10.0, min=0.0, max=512.0, step=0.1, label='贝塞尔抖动启用距离', group='3. 极简移动', desc='只有距离超过这个值才加细微抖动。设为 0 仅在抖动强度非 0 时生效。'),
    _field('bezier_jitter_scale', 'float', 0.0, min=0.0, max=0.1, step=0.001, label='贝塞尔抖动强度', group='3. 极简移动', desc='轨迹抖动强度。0 表示关闭，避免远距离画 Z。'),
    _field('bezier_jitter_damping', 'float', 0.3, min=0.0, max=1.0, step=0.01, label='贝塞尔抖动衰减', group='3. 极简移动', desc='抖动附加系数，越小越稳。'),

    _field('luma_enabled', 'bool', True, label='启用 LUMA 控制器', group='4. LUMA 控制器', desc='关闭后跳过 LUMA PID/预测器，直接用目标偏差进入倍率、限幅和贝塞尔输出链路，便于调试对比。'),
    _field('chris_kp', 'float', 0.45, min=0.0, max=5.0, step=0.01, label='LUMA Kp', group='4. LUMA 控制器', desc='LUMA 控制器的比例项增益。'),
    _field('chris_ki', 'float', 0.02, min=0.0, max=1.0, step=0.001, label='LUMA Ki', group='4. LUMA 控制器', desc='LUMA 控制器的积分项增益。'),
    _field('chris_kd', 'float', 0.04, min=0.0, max=1.0, step=0.001, label='LUMA Kd', group='4. LUMA 控制器', desc='LUMA 控制器的微分项增益。'),
    _field('chris_predict_weight_x', 'float', 0.5, min=0.0, max=2.0, step=0.01, label='预测权重 X', group='4. LUMA 控制器', desc='预测位移在 X 轴上的融合权重。'),
    _field('chris_predict_weight_y', 'float', 0.1, min=0.0, max=2.0, step=0.01, label='预测权重 Y', group='4. LUMA 控制器', desc='预测位移在 Y 轴上的融合权重。'),
    _field('chris_init_scale', 'float', 0.6, min=0.0, max=2.0, step=0.01, label='起步缩放', group='4. LUMA 控制器', desc='刚锁定目标时对 Kp 的初始缩放。'),
    _field('chris_ramp_time', 'float', 0.5, min=0.0, max=3.0, step=0.01, label='爬升时间', group='4. LUMA 控制器', desc='从起步缩放恢复到完整 Kp 的时间，单位秒。'),
    _field('chris_output_max', 'float', 512.0, min=1.0, max=4096.0, step=1.0, label='控制器输出上限', group='4. LUMA 控制器', desc='LUMA 控制器单轴内部输出裁剪上限。'),
    _field('chris_i_max', 'float', 100.0, min=0.0, max=1000.0, step=1.0, label='积分项上限', group='4. LUMA 控制器', desc='积分项累计上限，防止积分爆炸。'),
    _field('chris_predict_alpha_vel', 'float', 0.25, min=0.0, max=1.0, step=0.01, label='速度平滑 Alpha', group='4. LUMA 控制器', desc='速度估计的一阶平滑系数。'),
    _field('chris_predict_alpha_acc', 'float', 0.15, min=0.0, max=1.0, step=0.01, label='加速度平滑 Alpha', group='4. LUMA 控制器', desc='加速度估计的一阶平滑系数。'),
    _field('chris_predict_max_vel', 'float', 3000.0, min=1.0, max=20000.0, step=1.0, label='最大预测速度', group='4. LUMA 控制器', desc='预测器内部允许的最大速度。'),
    _field('chris_predict_max_acc', 'float', 5000.0, min=1.0, max=40000.0, step=1.0, label='最大预测加速度', group='4. LUMA 控制器', desc='预测器内部允许的最大加速度。'),

    _field('loop_interval_ms', 'float', 0.0, min=0.0, max=1000.0, step=0.1, label='主循环尾部休眠 ms', group='5. 节奏与调试', desc='主循环每轮结尾额外 sleep 的时长。0 表示不休眠。'),
    _field('max_frame_age_ms', 'float', 80.0, min=0.0, max=1000.0, step=1.0, label='最大帧滞后 ms', group='5. 节奏与调试', desc='超过这个滞后时间的采集帧会直接丢弃，并重置当前目标跟踪，避免后续坐标继续沿用旧画面。0 表示不启用。'),
    _field('aim_precision_debug_enabled', 'bool', False, label='精准调试记录', group='5. 节奏与调试', desc='按下瞄准键时保存详细轨迹和图像。'),
    _field('print_every', 'float', 2.0, min=0.0, max=10.0, step=0.1, label='日志输出间隔', group='5. 节奏与调试', desc='汇总日志打印间隔，单位秒。'),
    _field('log_every_frame', 'bool', False, label='每帧输出日志', group='5. 节奏与调试', desc='开启后每一帧都打印详细日志。'),
]

RUNTIME_FIELD_MAP = {field['name']: field for field in RUNTIME_SCHEMA}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {'1', 'true', 'yes', 'on'}


def coerce_runtime_value(name: str, value: Any) -> Any:
    field = RUNTIME_FIELD_MAP[name]
    field_type = field['type']
    if field_type == 'bool':
        coerced: Any = _coerce_bool(value)
    elif field_type == 'int':
        coerced = int(float(value))
    elif field_type == 'str':
        coerced = str(value).strip()
    else:
        coerced = float(value)

    if field_type != 'str':
        if 'min' in field:
            coerced = max(field['min'], coerced)
        if 'max' in field:
            coerced = min(field['max'], coerced)
    return coerced


def _defaults_raw() -> dict[str, Any]:
    return {field['name']: field['default'] for field in RUNTIME_SCHEMA}


def apply_simple_aim_tuning(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config)
    for field in RUNTIME_SCHEMA:
        name = field['name']
        if name not in cfg:
            cfg[name] = field['default']
        cfg[name] = coerce_runtime_value(name, cfg[name])
    return cfg


def build_runtime_config_from_args(args: Any) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for field in RUNTIME_SCHEMA:
        raw_value = getattr(args, field['name'], field['default'])
        config[field['name']] = coerce_runtime_value(field['name'], raw_value)
    return apply_simple_aim_tuning(config)


def sanitize_runtime_updates(updates: dict[str, Any]) -> dict[str, Any]:
    merged = _defaults_raw()
    for name, value in updates.items():
        if name not in RUNTIME_FIELD_MAP:
            continue
        merged[name] = coerce_runtime_value(name, value)
    return apply_simple_aim_tuning(merged)


def sanitize_runtime_patch(base: dict[str, Any] | None, updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or runtime_defaults())
    if not merged:
        merged = runtime_defaults()
    for name, value in updates.items():
        if name not in RUNTIME_FIELD_MAP:
            continue
        merged[name] = coerce_runtime_value(name, value)
    return apply_simple_aim_tuning(merged)


def runtime_defaults() -> dict[str, Any]:
    return apply_simple_aim_tuning(_defaults_raw())
