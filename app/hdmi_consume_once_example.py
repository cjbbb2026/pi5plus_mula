#!/usr/bin/env python3
from __future__ import annotations

import time

from hdmi_low_latency import LatestFrameCapture


def main() -> int:
    capture = LatestFrameCapture(
        device="/dev/video0",
        width=1920,
        height=1080,
        fps=60,
        backend="v4l2",
        crop_width=500,
        crop_height=500,
        process_width=0,
        process_height=0,
    ).start()

    print("Started. Each frame can be consumed only once.")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            ok, frame, frame_id, capture_time, source_shape, crop_rect = capture.consume_latest()
            if not ok:
                time.sleep(0.001)
                continue

            age_ms = (time.time() - capture_time) * 1000.0
            print(
                f"frame_id={frame_id} age_ms={age_ms:.1f} "
                f"source_shape={source_shape} crop_rect={crop_rect} process_shape={frame.shape}"
            )

            # Put your algorithm here. This frame_id will not be returned again.

    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
