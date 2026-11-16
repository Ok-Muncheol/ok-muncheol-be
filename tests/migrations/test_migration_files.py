"""B(통합): 마이그레이션 파일 규칙과 코드 계약 동기화. DB 없이 CI에서 돈다.

SQL의 상태값·단계·재실행 상한은 이름 붙은 제약으로 두고, 여기서 Python 계약과 같은지 본다.
계약을 바꾸면 새 마이그레이션으로 제약을 바꿔야 이 검사가 통과한다.
"""

import re
import typing
from pathlib import Path

from app.schemas.admin import AssignmentMethod, Coverage, IntakeStatus, Region
from app.schemas.common import JudgmentStatus, TaskStatus
from app.schemas.judgment import ValidationCheck
from app.workflow.graph import MAX_REBUTTAL_RETRIES, STAGES

MIGRATION_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
FILES = sorted(MIGRATION_DIR.glob("*.sql"))
SQL = "\n".join(path.read_text(encoding="utf-8") for path in FILES)


def _in_list(constraint: str) -> set[str]:
    pattern = rf"constraint {constraint}\s+check\s*\(\s*\w+\s+in\s*\((.*?)\)\s*\)"
    match = re.search(pattern, SQL, re.S)
    assert match, f"{constraint} 제약을 찾지 못했다"
    return set(re.findall(r"'([^']*)'", match.group(1)))


def _upper_bound(constraint: str) -> int:
    pattern = rf"constraint {constraint}\s+check\s*\(\s*\w+\s+between\s+0\s+and\s+(\d+)\s*\)"
    match = re.search(pattern, SQL)
    assert match, f"{constraint} 제약을 찾지 못했다"
    return int(match.group(1))


def test_file_names_follow_supabase_cli():
    assert FILES, "supabase/migrations/*.sql이 없다"
    for path in FILES:
        assert re.fullmatch(r"\d{14}_[a-z0-9_]+\.sql", path.name), path.name
    stamps = [path.name[:14] for path in FILES]
    assert len(set(stamps)) == len(stamps), "마이그레이션 타임스탬프가 겹친다"


def test_every_public_table_enables_rls():
    tables = set(re.findall(r"create table (?:if not exists )?public\.(\w+)", SQL, re.I))
    rls = r"alter table (?:only )?public\.(\w+)\s+enable row level security"
    enabled = set(re.findall(rls, SQL, re.I))
    assert tables, "public 테이블이 없다"
    assert tables <= enabled, f"RLS를 켜지 않은 public 테이블: {sorted(tables - enabled)}"


def test_status_values_match_contracts():
    assert _in_list("judgment_runs_status_check") == set(typing.get_args(TaskStatus))
    assert _in_list("judgment_runs_judgment_status_check") == set(typing.get_args(JudgmentStatus))
    assert _in_list("judgment_runs_stage_check") == set(STAGES)
    layer = typing.get_type_hints(ValidationCheck)["layer"]
    assert _in_list("validation_checks_layer_check") == set(typing.get_args(layer))


def test_rebuttal_retry_cap_matches_graph():
    assert _upper_bound("judgment_runs_retry_cap") == MAX_REBUTTAL_RETRIES
    assert _upper_bound("validation_checks_attempt_check") == MAX_REBUTTAL_RETRIES


def test_intake_values_match_admin_contract():
    """접수·배정 표의 지역·상태·배정 방식·분야가 app/schemas/admin.py와 같다."""
    assert _in_list("case_intakes_region_check") == set(typing.get_args(Region))
    assert _in_list("case_intakes_status_check") == set(typing.get_args(IntakeStatus))
    assert _in_list("case_assignments_method_check") == set(typing.get_args(AssignmentMethod))
    assert _in_list("case_assignments_coverage_check") == set(typing.get_args(Coverage))
