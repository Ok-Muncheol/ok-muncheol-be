"""시연 접수 하나를 넣는다 — 10초 뒤 관제 화면에 나타나고 1분 뒤 지워지는 가상 접수(시연 영상용).

관리자 화면에는 단추를 두지 않고 이 스크립트로만 넣는다. DATABASE_URL(.env)의 DB에 쓴다.
설명은 app/api/admin.py register_demo_intake와 app/dispatch/simulation.py의 DEMO_*.

    .venv/bin/python scripts/demo_intake.py                # 지역은 가상 접수 비중대로
    .venv/bin/python scripts/demo_intake.py --region 서울  # 그 시·도로
    .venv/bin/python scripts/demo_intake.py --rule         # LLM 없이 규칙으로 배정

가장 먼저 만든 관리자 계정(app_metadata.staff_role = admin)이 넣은 것으로 남긴다 — 등록한
관리자가 있는 가상 접수가 시연 접수의 표시다. 지우는 일은 배포된 BE가 조회할 때 한다.
"""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.api import admin  # noqa: E402
from app.api.errors import ApiException  # noqa: E402
from app.dispatch import service  # noqa: E402

ADMIN_SQL = """
select id from auth.users
where raw_app_meta_data ->> 'staff_role' = 'admin'
order by created_at
limit 1
"""


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env")


async def run(args: argparse.Namespace) -> int:
    async with db.connect() as conn:
        row = await (await conn.execute(ADMIN_SQL)).fetchone()
    if row is None:
        print("DEMO result=NO_ADMIN 관리자 계정(app_metadata.staff_role = admin)이 없다")
        return 1
    if args.rule:
        admin.configure(decide=service.decide_by_rule)
    try:
        view = await admin.register_demo_intake(str(row["id"]), region=args.region)
    except ApiException as exc:
        print(f"DEMO result=INVALID {exc.error['message']}")
        return 1
    note = view["assignment"]
    print(f"지역 {view['region']} · {view['location']}")
    print(f"접수 시각 {view['received_at']}(10초 뒤 나타나고 1분 뒤 지워진다)")
    print(f"상태 {view['status']}"
          + (f" · {note['method']}: {note['reason']}" if note else "")
          + (f" · 까닭: {view['unassigned_reason']}" if view["unassigned_reason"] else ""))
    print(f"DEMO result=OK intake_id={view['intake_id']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", help="시·도 이름(예: 서울). 없으면 가상 접수의 지역 비중대로")
    parser.add_argument("--rule", action="store_true", help="LLM 없이 규칙으로 배정한다")
    args = parser.parse_args()
    load_env()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
