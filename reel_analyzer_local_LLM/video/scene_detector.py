from __future__ import annotations

from pathlib import Path


def detect_scene_changes(video_path: Path, duration_sec: float, threshold: float = 0.45) -> list[float]:
    """Return coarse scene changes using sparse HSV histogram comparisons.

    This intentionally has no hard dependency on PySceneDetect: OpenCV is already
    required for frame extraction and a detector failure must not stop analysis.
    """
    try:
        import cv2
    except ImportError as error:
        raise RuntimeError("OpenCV is not installed") from error
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError("Unable to open video for scene detection")
    sample_interval = 0.5
    previous = None
    changes: list[float] = []
    try:
        timestamp = 0.0
        while timestamp <= duration_sec:
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
            ok, frame = capture.read()
            if not ok:
                timestamp += sample_interval
                continue
            small = cv2.resize(frame, (160, 90))
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            histogram = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
            cv2.normalize(histogram, histogram)
            if previous is not None and cv2.compareHist(previous, histogram, cv2.HISTCMP_BHATTACHARYYA) >= threshold:
                changes.append(round(timestamp, 3))
            previous = histogram
            timestamp += sample_interval
    finally:
        capture.release()
    return changes
