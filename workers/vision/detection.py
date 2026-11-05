"""A: YOLO 탐지/추적·키프레임·특징. 모델은 구현 시 함수 안에서 로딩한다."""


def detect_and_track(video_path: str) -> dict[str, object]:
    """가림·트랙·타임스탬프·특징과 가중치 버전을 반환한다.

    픽셀 이동으로 실제 속도·과속을 확정하지 않는다. SAM2는 선택 기능이다.
    """
    raise NotImplementedError("A: YOLO·OpenCV·선택적 SAM2")
