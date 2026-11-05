"""B: 검증 ③ DB 기반 과실 계산·코드 검증. 금액 산정은 D 소유다."""

from app.schemas.judgment import JudgmentResult, ValidationCheck


def calculate_fault(base_A: int, modifiers: list[dict[str, object]]) -> tuple[int, int]:
    """검수한 구조 레코드만 사용한다.

    A = clip(base_A + A측 가감합 - B측 가감합, 0, 100), B = 100 - A.
    조건/증거·중복·상호배타·방향/부호를 검사한 뒤 계산한다.
    """
    raise NotImplementedError("B: 수정요소 검증·과실 산식")


def check_numerical(result: JudgmentResult) -> ValidationCheck:
    """0~100·A+B=100·DB 원값·적용판·계산 로그·서술/수치 일치 검사."""
    raise NotImplementedError("B: 과실 계산 후검증")
