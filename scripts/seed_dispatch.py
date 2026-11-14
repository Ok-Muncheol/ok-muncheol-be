"""가상 접수를 미리 만들어 관제 화면을 채운다(시연용). 설명은 app/dispatch/seeding.py.

기본은 계획만 출력한다. 실제로 만들려면 --apply를 붙인다. DATABASE_URL(.env)의 DB에 쓴다.

    .venv/bin/python scripts/seed_dispatch.py                 # 계획만
    .venv/bin/python scripts/seed_dispatch.py --apply         # 배정 에이전트(LLM)로 채운다
    .venv/bin/python scripts/seed_dispatch.py --apply --rule  # LLM 없이 규칙으로 채운다
    .venv/bin/python scripts/seed_dispatch.py --apply --reset # 가상 접수를 지우고 다시 채운다

가상 접수가 이미 있으면 멈춘다(--reset으로 지우고 다시 만든다). 실제 접수·사건은 건드리지 않는다.
"""

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.agents import dispatch as dispatch_agent  # noqa: E402
from app.dispatch import repository, seeding, service  # noqa: E402
from app.dispatch.simulation import MEAN_DAYS  # noqa: E402


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env")


async def run(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    start, horizon = now - timedelta(days=args.days_back), now + timedelta(days=args.days_ahead)
    per_slot = (args.days_back + args.days_ahead) / MEAN_DAYS
    expected = round(args.slots * per_slot)
    print(f"기간 {start:%m-%d %H:%M} ~ {horizon:%m-%d %H:%M}(UTC), 사건 자리 {args.slots}개")
    print(f"예상 접수 건 약 {expected:,}건, 하루 약 {args.slots / MEAN_DAYS:.0f}건 접수·종결")
    if not args.rule:
        print(f"배정 에이전트 호출 약 {expected * 2:,}회({dispatch_agent.model_name()}) — "
              "실패한 건은 규칙으로 배정된다")
    async with db.connect() as conn:
        existing = await repository.count_simulated(conn)
    print(f"지금 있는 가상 접수: {existing:,}건")
    if not args.apply:
        print("SEED result=PLAN_ONLY (만들려면 --apply)")
        return 0
    if existing and not args.reset:
        print("SEED result=ABORTED 가상 접수가 이미 있다(--reset으로 지우고 다시 만든다)")
        return 1
    if existing:
        async with db.connect() as conn:
            print(f"가상 접수 {await repository.delete_simulated(conn):,}건을 지웠다")

    started, last = time.monotonic(), [0.0]

    def progress(report: seeding.FillReport) -> None:
        if time.monotonic() - last[0] >= 15:
            last[0] = time.monotonic()
            print(f"  {report.intakes:,}건 ({time.monotonic() - started:.0f}초)", flush=True)

    decide = service.decide_by_rule if args.rule else dispatch_agent.decide
    report = await seeding.fill(
        db.connect, start=start, horizon=horizon, slots=args.slots, decide=decide,
        seed=args.seed, concurrency=args.concurrency, progress=progress,
    )
    print(f"접수 건 {report.intakes:,}건, 상태 {dict(report.statuses)}, "
          f"배정 방식 {dict(report.methods)}, {time.monotonic() - started:.0f}초")
    print("SEED result=OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="실제로 만든다(없으면 계획만 출력)")
    parser.add_argument("--reset", action="store_true", help="있던 가상 접수를 지우고 다시 만든다")
    parser.add_argument("--rule", action="store_true", help="LLM 없이 규칙으로 배정한다")
    parser.add_argument("--slots", type=int, default=450, help="진행 중으로 둘 사건 수")
    parser.add_argument("--days-back", type=float, default=7, help="지금부터 며칠 전부터 채울지")
    parser.add_argument("--days-ahead", type=float, default=5, help="지금부터 며칠 뒤까지 채울지")
    parser.add_argument("--concurrency", type=int, default=8, help="한 번에 배정할 접수 수")
    parser.add_argument("--seed", type=int, default=20261113)
    args = parser.parse_args()
    load_env()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
