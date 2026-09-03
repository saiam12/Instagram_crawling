from __future__ import annotations


def frames_are_similar(first, second, threshold: float = 0.08) -> bool:
    """Compare two OpenCV BGR frames using normalized HSV histograms."""
    import cv2

    def histogram(frame):
        small = cv2.resize(frame, (160, 90))
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        value = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
        cv2.normalize(value, value)
        return value

    return cv2.compareHist(histogram(first), histogram(second), cv2.HISTCMP_BHATTACHARYYA) < threshold
