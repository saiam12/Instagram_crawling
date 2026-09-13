from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VideoMetadata:
    duration_sec: float
    width: int
    height: int
    fps: float
    frame_count: int


def read_video_metadata(video_path: Path) -> VideoMetadata:
    try:
        import cv2
    except ImportError as error:
        raise RuntimeError("OpenCV is not installed") from error
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError("Unable to open video file")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    finally:
        capture.release()
    if fps <= 0 or frame_count <= 0 or width <= 0 or height <= 0:
        raise RuntimeError("Unable to read video metadata")
    return VideoMetadata(frame_count / fps, width, height, fps, frame_count)
