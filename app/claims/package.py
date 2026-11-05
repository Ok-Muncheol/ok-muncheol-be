"""D: 1차 확장 구상 청구 패키지. PDF/JSON 생성·전달은 아직 미구현."""

from app.schemas.reports import RecoveryAmounts, ReviewReport


def build_claim_package(
    approved_report: ReviewReport, amounts: RecoveryAmounts
) -> dict[str, object]:
    """권한·승인·입력 버전·필수 서류 검사 후 근거/계산 로그와 패키지 조립.

    예정액·청구액·회수액·고객 자기부담금 검토액을 분리한다.
    '지급액 × 상대 과실'만으로 실제 청구/회수액을 확정하지 않는다.
    """
    raise NotImplementedError("D: 구상 서식·금액 검수·PDF/JSON 패키지")
