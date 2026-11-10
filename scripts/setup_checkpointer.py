"""B: LangGraph 체크포인트 테이블을 공용 DB의 langgraph 스키마에 만든다. 한 번, 사람이 실행한다.

사용: .venv/bin/python scripts/setup_checkpointer.py
DATABASE_URL은 환경 변수, 없으면 .env에서 읽는다(Session pooler 주소).
주소·비밀번호는 출력하지 않는다.
langgraph 스키마와 권한은 마이그레이션(20261109163901_judgment_runs.sql)이 먼저 만들어 둬야 한다.
이미 있으면 라이브러리의 checkpoint_migrations 표에 없는 변경만 적용한다(여러 번 실행해도 된다).
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url and (ROOT / ".env").is_file():
        from dotenv import dotenv_values

        url = dotenv_values(ROOT / ".env").get("DATABASE_URL") or ""
    if not url:
        sys.exit("DATABASE_URL이 없다(.env 또는 환경 변수)")
    return url


async def main() -> None:
    from app.db import connect
    from app.workflow.runs import setup_checkpoints

    async with connect(_database_url()) as conn:
        await setup_checkpoints(conn)
        cur = await conn.execute(
            "select tablename from pg_tables where schemaname = 'langgraph' order by 1"
        )
        tables = [row["tablename"] for row in await cur.fetchall()]
        cur = await conn.execute(
            "select count(*) as n from pg_tables where schemaname = 'public'"
            " and tablename like 'checkpoint%%'"
        )
        stray = (await cur.fetchone())["n"]
    print(f"langgraph 스키마 테이블: {', '.join(tables) or '없음'}")
    if stray:
        sys.exit(f"public에 체크포인트 테이블이 {stray}개 있다. search_path를 확인한다")


if __name__ == "__main__":
    asyncio.run(main())
