"""D: 금액 산정 규칙 값과 근거 조항 ID.

⚠ 모든 값은 '예시'다(rule_version이 'example-'로 시작). 실무·약관 검수 전에는
   보고서에 '예시 규칙'으로 표시되고, 계산 결과를 확정(confirmed)으로 내지 않는다.
   C가 약관 원문(policy_terms)을 넘겨주고 실무 검수가 끝나면 이 파일만 고친다.
   계약마다 다른 값(자기부담금 등)은 OwnDamageContractDoc 값이 우선한다.
"""

from dataclasses import dataclass, field

_OWN_DAMAGE_EXCLUSIONS = "samsung-personal-auto-2026-11-11:general_article:23:p67"


@dataclass(frozen=True)
class RuleSet:
    version: str = "example-2026-11-05"

    # 자기부담금 기본값 (계약에 값이 없을 때). TODO(검수): 상품·특약별 조건
    deductible_rate: float = 0.20
    deductible_min: int = 200_000
    deductible_max: int = 500_000

    # 전손 판단: 손해액 ≥ 차량가액 × 비율. TODO(검수): 전손 기준·보상액 기준
    total_loss_ratio: float = 1.0

    # 근거 조항 ID. TODO(C·검수): policy_terms 청크의 실제 parent_id로 교체
    clause_payout: str = "example:own_damage.payout"
    clause_deductible: str = "example:own_damage.deductible"
    clause_total_loss: str = "example:own_damage.total_loss"
    clause_subrogation: str = "example:subrogation"
    clause_period: str = "example:policy_period"
    clause_driver_scope: str = "example:driver_scope_rider"

    # 면책 조항 parent_id: 벡터 검색 없이 직접 가져온다. 주제 → 조항 ID.
    # 삼성화재 개인용 2026.11.11판 제23조(자기차량손해 보상하지 않는 손해): 고의 제1호(p.67),
    # 무면허·음주 제14호(p.68). 한 조항이 청크 2개라 판정기에는 둘 다 넘어간다.
    # TODO(검수): 다른 회사·판의 약관이 들어오면 약관별로 나눈다
    exclusion_clause_parents: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "무면허 운전 면책": (_OWN_DAMAGE_EXCLUSIONS,),
            "음주 운전 자기차량손해 면책": (_OWN_DAMAGE_EXCLUSIONS,),
            "고의 사고 면책": (_OWN_DAMAGE_EXCLUSIONS,),
        },
        hash=False,
    )

    # 면책·감액 확인 주제. 판정은 exclusions.py
    exclusion_topics: tuple[str, ...] = field(
        default=(
            "무면허 운전 면책",
            "음주 운전 자기차량손해 면책",
            "고의 사고 면책",
        )
    )

    @property
    def is_example(self) -> bool:
        return self.version.startswith("example-")


DEFAULT_RULES = RuleSet()


def deductible_for(
    recognized: int,
    *,
    fixed: int | None,
    rate: float | None,
    min_: int | None,
    max_: int | None,
    rules: RuleSet,
) -> int:
    """자기부담금. 계약 값 우선, 없으면 규칙 기본값. 손해액을 넘지 않는다."""
    if fixed is not None:
        return min(fixed, recognized)
    r = rules.deductible_rate if rate is None else rate
    lo = rules.deductible_min if min_ is None else min_
    hi = rules.deductible_max if max_ is None else max_
    return min(max(lo, min(hi, int(recognized * r))), recognized)


def share(amount: int, fault_percent: int) -> int:
    """금액 × 과실(%). 원 미만 버림."""
    return amount * fault_percent // 100
