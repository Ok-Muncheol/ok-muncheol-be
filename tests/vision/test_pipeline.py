"""A: 비전 파이프라인 — workers/vision/contract.py(v1 변환)와 workers/vision/pipeline.py(영상
맞추기·단계).

무거운 모델(YOLO·VideoMAE·VLM)은 부르지 않는다. 비전 분석·카드 조립은 가짜로 바꿔 끼운다.
영상이 필요한 테스트는 OpenCV가 없으면 건너뛴다(팀 CI는 비전 패키지를 설치하지 않음).
"""

import threading

import pytest

from workers.vision import contract
from workers.vision import pipeline as pl


def field(value, status="unverified", **detail):
    return {
        "value": value,
        "model_value": value,
        "source": "vision",
        "model_confidence": 0.9,
        "verification_status": status,
        "evidence_frame_ids": ["f075"],
        "source_document_ids": [],
        "vision_detail": {
            "detection": None,
            "vlm": {"value": value, "confidence": 0.9},
            "agree": None,
            **detail,
        },
    }


def internal_card(**overrides):
    fields = {
        "accident_object": field("차대차"),
        "party_count": field(2),
        "location_type": field("T자형교차로", "needs_review"),
        "road_priority": field("B대로"),
        "signal_A": field("불명"),
        "signal_B": field("녹색직진좌회전"),
        "movement_A": field("추월", "needs_review"),
        "movement_B": field("주정차"),
        "relative_position": field("왼쪽"),
        "entry_order": field("A먼저"),
        "time_condition": field("야간"),
        "collision_time_ms": field(4667, "needs_review"),
        "narrative": field("설명"),
        "collision_part_A": field("앞"),
        "weather": field("맑음"),
    }
    fields.update(overrides)
    return {
        "case_id": "c1",
        "version": 1,
        "accident_date": None,
        "fields": fields,
        "parties": [{"party_id": "P1", "role": "촬영 차량", "track_id": None}],
        "party_mapping": {
            "video_ego_party_id": "P1",
            "insured_party_id": None,
            "chart_party_A_id": "P2",
            "chart_party_B_id": "P1",
        },
        "evidence_frames": [{"frame_id": "f075", "timestamp_ms": 5000, "storage_path": None}],
        "status": "draft",
        "confirmed_by": None,
        "confirmed_at": None,
        "model_info": {"vlm": "x"},
    }


# ---------------------------------------------------------------- v1 변환


def test_v1_has_every_contract_field_in_english():
    from app.schemas.facts import FACT_FIELDS

    out = contract.to_contract_card(internal_card())
    # 11/16 교차로 밖 도표 칸 22개가 더해져 39칸이다(담당자 입력 18 + 이륜차·긴급자동차 4)
    assert tuple(out["fields"]) == FACT_FIELDS and len(FACT_FIELDS) == 39
    assert set(out["fields"]) >= {
        "accident_type",
        "location_type",
        "road_priority",
        "signal_A",
        "signal_B",
        "collision_signal_A",
        "collision_signal_B",
        "movement_A",
        "movement_B",
        "lane_marking_A",
        "lane_marking_B",
        "relative_position",
        "entry_order",
        "night",
        "unprotected_left_sign",
        "collision_time_ms",
        "narrative",
    }
    v = {k: f["value"] for k, f in out["fields"].items()}
    assert v["accident_type"] == "car_to_car"
    assert v["location_type"] == "unsignalized_intersection"  # T자형 묶음
    assert v["road_priority"] == "major_B"
    assert v["signal_B"] == "green_left_arrow"
    assert v["movement_A"] == "lane_change" and v["movement_B"] == "other"
    assert v["relative_position"] == "left" and v["entry_order"] == "first"
    assert v["night"] is True and v["collision_time_ms"] == 4667


def test_manual_fields_are_null_unverified_and_do_not_block_confirm():
    """비전이 채우지 않는 칸은 null + unverified(출처 manual)라 확정을 막지 않는다."""
    from app.api import facts

    out = contract.to_contract_card(internal_card())
    for name in contract.MANUAL_FIELDS:
        f = out["fields"][name]
        assert (f["value"], f["model_value"], f["source"], f["verification_status"]) == (
            None, None, "manual", "unverified"), name
    problems = facts.confirm_problems(out)
    assert not any(name in p for p in problems for name in contract.MANUAL_FIELDS)


def test_vision_review_fields_stay_empty_until_the_vision_step():
    """11/16 이륜차·긴급자동차 칸은 영상 AI 연결(2단계) 전까지 담당자 입력 칸처럼 빈다."""
    from app.schemas.facts import VISION_REVIEW_FIELDS

    out = contract.to_contract_card(internal_card())
    for name in VISION_REVIEW_FIELDS:
        f = out["fields"][name]
        assert (f["value"], f["source"], f["verification_status"]) == (
            None, "manual", "unverified"), name
    assert not contract.to_contract_card(internal_card())["fields"]["two_wheeler_A"].get(
        "vision_detail")


def test_unknown_becomes_null_and_needs_review():
    out = contract.to_contract_card(internal_card())
    sig = out["fields"]["signal_A"]
    assert sig["value"] is None and sig["model_value"] is None
    assert sig["verification_status"] == "needs_review"


def test_internal_value_kept_and_status_kept():
    out = contract.to_contract_card(internal_card())
    mv = out["fields"]["movement_A"]
    assert mv["vision_detail"]["internal_value"] == "추월"
    assert mv["verification_status"] == "needs_review"
    assert out["fields"]["road_priority"]["verification_status"] == "unverified"


def test_offset_moves_times_back_to_original_video():
    out = contract.to_contract_card(
        internal_card(), offset_ms=20000, accident_date="2026-11-11", version=3
    )
    assert out["fields"]["collision_time_ms"]["value"] == 24667
    assert out["evidence_frames"][0]["timestamp_ms"] == 25000
    assert out["accident_date"] == "2026-11-11" and out["version"] == 3


def test_each_scored_field_carries_eval_accuracy():
    out = contract.to_contract_card(internal_card())
    assert out["fields"]["location_type"]["vision_detail"]["eval"]["accuracy"] == 0.80
    assert "92건" in out["fields"]["movement_A"]["vision_detail"]["eval"]["basis"]
    assert "eval" not in (out["fields"]["narrative"].get("vision_detail") or {})
    assert set(contract.FIELD_ACCURACY) <= set(out["fields"])


def test_three_or_more_vehicles_out_of_scope():
    out = contract.to_contract_card(internal_card(party_count=field(3)))
    assert out["fields"]["accident_type"]["value"] == "other"


def test_original_card_not_changed():
    card = internal_card()
    contract.to_contract_card(card, offset_ms=1000)
    assert card["fields"]["movement_A"]["value"] == "추월"
    assert card["fields"]["collision_time_ms"]["value"] == 4667


# ---------------------------------------------------------------- 영상 맞추기(OpenCV 필요)


def make_video(path, fps: float, seconds: float, size=(320, 180)):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    for i in range(round(fps * seconds)):
        img = np.full((size[1], size[0], 3), (i * 7) % 255, np.uint8)
        w.write(img)
    w.release()
    return str(path)


def test_probe_rejects_missing_and_short(tmp_path):
    pytest.importorskip("cv2")
    with pytest.raises(pl.VisionPipelineError) as e:
        pl.probe(str(tmp_path / "none.mp4"))
    assert e.value.code == "video_not_found"
    short = make_video(tmp_path / "s.mp4", 15, 1)
    with pytest.raises(pl.VisionPipelineError) as e:
        pl.probe(short)
    assert e.value.code == "video_too_short"


def test_aihub_shape_used_as_is(tmp_path):
    video = make_video(tmp_path / "a.mp4", 15, 10)
    clip, cut = pl.normalize(video, pl.probe(video), tmp_path)
    assert clip == video and cut == {"mode": "as_is", "offset_ms": 0, "auto_cut": False}


def test_30fps_resampled_by_time(tmp_path):
    video = make_video(tmp_path / "b.mp4", 30, 6)
    clip, cut = pl.normalize(video, pl.probe(video), tmp_path)
    info = pl.probe(clip)
    assert cut["mode"] == "resampled"
    assert round(info["fps"]) == 15 and abs(info["seconds"] - 6) < 0.2


def test_long_video_cut_around_collision(tmp_path, monkeypatch):
    from workers.vision import ego_motion

    class FakeEgo:
        collision_frame = 30 * 15  # 30초

    monkeypatch.setattr(ego_motion, "analyze_video", lambda path: FakeEgo())
    video = make_video(tmp_path / "c.mp4", 30, 60)
    clip, cut = pl.normalize(video, pl.probe(video), tmp_path)
    assert cut["auto_cut"] and cut["collision_found"]
    assert cut["cut_seconds"] == [25.0, 35.0] and cut["offset_ms"] == 25000
    assert abs(pl.probe(clip)["seconds"] - 10) < 0.2


# ---------------------------------------------------------------- 전체 흐름(분석·카드 조립은 가짜)


def test_pipeline_stages_and_auto_cut_flag(tmp_path, monkeypatch):
    from workers.vision import ego_motion, fact_card

    class FakeEgo:
        collision_frame = 450

    monkeypatch.setattr(ego_motion, "analyze_video", lambda path: FakeEgo())
    monkeypatch.setattr(
        pl, "build_detections", lambda clip, work, use_j3=True, ego=None: {"use_j3": use_j3}
    )
    seen = {}

    def fake_extract(case_id, clip, detections, **kw):
        seen.update(detections)
        return internal_card(collision_time_ms=field(5000, "unverified"))

    monkeypatch.setattr(fact_card, "extract_fact_card_sync", fake_extract)
    video = make_video(tmp_path / "d.mp4", 30, 60)
    out = pl.analyze_video_sync(
        "case-1", video, version=2, options=pl.PipelineOptions(vlm=object(), use_j3=False)
    )
    assert seen == {"use_j3": False}
    ct = out["fields"]["collision_time_ms"]
    assert ct["value"] == 25000 + 5000 and ct["verification_status"] == "needs_review"
    info = out["model_info"]["pipeline"]
    assert info["cut"]["mode"] == "auto_cut" and info["use_j3"] is False
    assert set(info["seconds"]) == {"normalize", "detect", "card"} and out["version"] == 2


def test_vlm_failure_is_retryable(tmp_path, monkeypatch):
    from workers.vision import ego_motion, fact_card

    monkeypatch.setattr(ego_motion, "analyze_video", lambda path: HintEgo())
    monkeypatch.setattr(pl, "build_detections", lambda clip, work, use_j3=True, ego=None: {})

    def boom(*a, **k):
        raise TimeoutError("vlm down")

    monkeypatch.setattr(fact_card, "extract_fact_card_sync", boom)
    video = make_video(tmp_path / "e.mp4", 15, 10)
    with pytest.raises(pl.VisionPipelineError) as e:
        pl.analyze_video_sync("case-1", video, options=pl.PipelineOptions(vlm=object()))
    assert (e.value.stage, e.value.code, e.value.retryable) == ("vlm", "vlm_failed", True)


# ---------------------------------------------------------------- VLM 질문 먼저 보내기(11/16)
# 영상·광학 흐름·영상 모델·사진 그리기는 가짜라 OpenCV 없이 돈다(팀 CI에서도 돈다).


class HintEgo:
    collision_frame = None

    def summary(self):
        return "카메라: 직진 중 충돌"


class RecordingVLM:
    """받은 질문과 부른 스레드를 남긴다. 질문이 들어오면 started가 켜진다."""

    name = "fake:vlm"

    def __init__(self, fail: Exception | None = None):
        self.calls: list[tuple[str, str]] = []
        self.started = threading.Event()
        self.fail = fail

    def generate(self, prompt, images, schema):
        self.calls.append((threading.current_thread().name, prompt))
        self.started.set()
        if self.fail:
            raise self.fail
        meta = {"seconds": 0.0, "prompt_tokens": 1, "output_tokens": 1}
        return {"scene_notes": f"답 {len(self.calls)}"}, meta


def fake_stages(monkeypatch, *, wait_for=None, extra=None, build_error=None):
    """카드 조립은 진짜처럼 fact_card.vlm_request로 질문을 만들어 VLM을 한 번 부른다.

    wait_for가 있으면 영상 모델 단계가 그 신호(VLM 질문이 나갔다)를 기다린다. 차례로 돌면
    VLM이 영상 모델 뒤에 불려 5초 뒤 실패한다.
    """
    from workers.vision import ego_motion, fact_card

    monkeypatch.setattr(
        pl, "probe", lambda path: {"fps": 15.0, "frames": 150, "seconds": 10.0,
                                   "width": 320, "height": 180}
    )
    monkeypatch.setattr(
        pl, "normalize",
        lambda path, info, work: (path, {"mode": "as_is", "offset_ms": 0, "auto_cut": False}),
    )
    monkeypatch.setattr(ego_motion, "analyze_video", lambda path: HintEgo())

    def fake_request(video_path, detections=None, *, viewpoint=1, collision_hint=75,
                     context=None, prompt_version="v0.5"):
        d = detections or {}
        keys = ("tracks", "mark_tracks", "hint", "facts")
        prompt = "\n".join(str(d[k]) for k in keys if d.get(k))
        return {"prompt": f"질문 {prompt_version}\n{prompt}", "images": ["사진"],
                "schema": {"version": prompt_version}}

    seen: dict = {}

    def fake_build(clip, work, use_j3=True, ego=None):
        seen["ego"] = ego
        if build_error:
            raise build_error
        if wait_for is not None:
            assert wait_for.wait(5), "VLM 질문이 영상 모델보다 먼저 나가야 한다"
        return {"hint": ego.summary(), "ego": ego, **(extra or {})}

    def fake_extract(case_id, clip, detections, *, vlm, prompt_version, **kw):
        req = fact_card.vlm_request(clip, detections, viewpoint=1, prompt_version=prompt_version)
        seen["raw"], _ = vlm.generate(req["prompt"], req["images"], req["schema"])
        seen["vlm_name"] = vlm.name
        return internal_card()

    monkeypatch.setattr(fact_card, "vlm_request", fake_request)
    monkeypatch.setattr(pl, "build_detections", fake_build)
    monkeypatch.setattr(fact_card, "extract_fact_card_sync", fake_extract)
    return seen


def test_vlm_question_goes_out_before_the_video_models_and_its_answer_is_used(monkeypatch):
    vlm = RecordingVLM()
    seen = fake_stages(monkeypatch, wait_for=vlm.started)
    out = pl.analyze_video_sync("case-1", "v.mp4", options=pl.PipelineOptions(vlm=vlm))
    # 한 번만 물었고(먼저 받은 답을 썼다), 카드 조립 스레드가 아닌 곳에서 물었다
    assert len(vlm.calls) == 1 and vlm.calls[0][0] != threading.current_thread().name
    assert "카메라: 직진 중 충돌" in vlm.calls[0][1]  # 광학 흐름 요약이 질문에 들어간다
    assert seen["raw"] == {"scene_notes": "답 1"} and seen["vlm_name"] == "fake:vlm"
    assert isinstance(seen["ego"], HintEgo)  # 광학 흐름은 한 번만 계산해 넘긴다
    info = out["model_info"]["pipeline"]
    assert info["parallel_vlm"] is True and "vlm_wait" in info["seconds"]
    assert info["version"] == pl.PIPELINE_VERSION


def test_parallel_vlm_off_asks_after_the_video_models(monkeypatch):
    vlm = RecordingVLM()
    fake_stages(monkeypatch)
    out = pl.analyze_video_sync(
        "case-1", "v.mp4", options=pl.PipelineOptions(vlm=vlm, parallel_vlm=False)
    )
    assert [thread for thread, _ in vlm.calls] == [threading.current_thread().name]
    info = out["model_info"]["pipeline"]
    assert info["parallel_vlm"] is False and set(info["seconds"]) == {"normalize", "detect", "card"}


def test_question_changed_by_later_detections_is_asked_again(monkeypatch):
    """질문에 쓰는 탐지 결과(예: 신호등 사실)가 영상 모델 단계에서 늘면 먼저 받은 답을 버린다."""
    vlm = RecordingVLM()
    seen = fake_stages(monkeypatch, wait_for=vlm.started, extra={"facts": "신호등: 적색"})
    out = pl.analyze_video_sync("case-1", "v.mp4", options=pl.PipelineOptions(vlm=vlm))
    assert len(vlm.calls) == 2
    assert "신호등: 적색" not in vlm.calls[0][1] and "신호등: 적색" in vlm.calls[1][1]
    assert seen["raw"] == {"scene_notes": "답 2"}  # 다시 물은 답으로 카드를 만든다
    assert out["model_info"]["pipeline"]["parallel_vlm"] is False


def test_failed_early_answer_is_a_retryable_vlm_error_without_asking_twice(monkeypatch):
    vlm = RecordingVLM(fail=TimeoutError("vlm down"))
    fake_stages(monkeypatch, wait_for=vlm.started)
    with pytest.raises(pl.VisionPipelineError) as e:
        pl.analyze_video_sync("case-1", "v.mp4", options=pl.PipelineOptions(vlm=vlm))
    assert (e.value.stage, e.value.code, e.value.retryable) == ("vlm", "vlm_failed", True)
    assert len(vlm.calls) == 1


def test_detection_failure_stays_a_detect_error_while_the_question_is_out(monkeypatch):
    vlm = RecordingVLM()
    fake_stages(monkeypatch, build_error=RuntimeError("GPU 오류"))
    with pytest.raises(pl.VisionPipelineError) as e:
        pl.analyze_video_sync("case-1", "v.mp4", options=pl.PipelineOptions(vlm=vlm))
    assert (e.value.stage, e.value.code, e.value.retryable) == ("detect", "vision_failed", False)


def test_question_that_cannot_be_sent_early_is_asked_in_order(monkeypatch):
    """미리 못 보내면(예: VLM에 generate가 없음) 카드 조립이 원래대로 묻고, 오류도 원래 단계다."""
    from workers.vision import fact_card

    fake_stages(monkeypatch)
    monkeypatch.setattr(
        fact_card, "extract_fact_card_sync",
        lambda case_id, clip, detections, *, vlm, **kw: vlm.generate("q", [], {}),
    )
    with pytest.raises(pl.VisionPipelineError) as e:
        pl.analyze_video_sync("case-1", "v.mp4", options=pl.PipelineOptions(vlm=object()))
    assert (e.value.stage, e.value.code) == ("vlm", "vlm_failed")


def test_v1_tables_only_produce_allowed_values():
    """변환표가 내는 값이 팀 계약 허용값(app/schemas/facts.py) 안에 있다. 카드 저장 API가 받는다."""
    from app.schemas.facts import FACT_FIELD_OPTIONS, FACT_FIELDS

    assert tuple(contract.V1_FIELDS) == FACT_FIELDS
    for v1_name, (_, table) in contract.V1_FIELDS.items():
        if table is not None and v1_name != "accident_type":
            assert set(table.values()) <= set(FACT_FIELD_OPTIONS[v1_name]), v1_name
    assert set(contract.MOVE.values()) | {"other"} <= set(FACT_FIELD_OPTIONS["movement_A"])


def test_default_prompt_has_same_direction_lane_rule():
    """v0.5(11/13): 같은 방향 옆 차로 차는 '왼쪽'·'오른쪽'이 아니라 같은방향_앞/뒤(계약 4절)."""
    from workers.vision import prompts

    assert prompts.PROMPT_VERSION == "v0.5"
    default = prompts.build_prompt(1, prompts.FRAMES_V02, 75)
    assert prompts.RELPOS_RULE in default
    old = prompts.build_prompt(1, prompts.FRAMES_V02, 75, version="v0.2")
    assert prompts.RELPOS_RULE not in old
