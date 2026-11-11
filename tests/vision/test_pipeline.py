"""A: 비전 파이프라인 — workers/vision/contract.py(v1 변환)와 workers/vision/pipeline.py(영상
맞추기·단계).

무거운 모델(YOLO·VideoMAE·VLM)은 부르지 않는다. 비전 분석·카드 조립은 가짜로 바꿔 끼운다.
영상이 필요한 테스트는 OpenCV가 없으면 건너뛴다(팀 CI는 비전 패키지를 설치하지 않음).
"""

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


def test_v1_has_exactly_twelve_fields_in_english():
    out = contract.to_contract_card(internal_card())
    assert set(out["fields"]) == {
        "accident_type",
        "location_type",
        "road_priority",
        "signal_A",
        "signal_B",
        "movement_A",
        "movement_B",
        "relative_position",
        "entry_order",
        "night",
        "collision_time_ms",
        "narrative",
    }
    v = {k: f["value"] for k, f in out["fields"].items()}
    assert v["accident_type"] == "car_to_car"
    assert v["location_type"] == "unsignalized_intersection"  # T자형 묶음
    assert v["road_priority"] == "major_B"
    assert v["signal_B"] == "green_left_arrow"
    assert v["movement_A"] == "lane_change" and v["movement_B"] == "other"
    assert v["relative_position"] == "side" and v["entry_order"] == "first"
    assert v["night"] is True and v["collision_time_ms"] == 4667


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
    monkeypatch.setattr(pl, "build_detections", lambda clip, work, use_j3=True: {"use_j3": use_j3})
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
    from workers.vision import fact_card

    monkeypatch.setattr(pl, "build_detections", lambda clip, work, use_j3=True: {})

    def boom(*a, **k):
        raise TimeoutError("vlm down")

    monkeypatch.setattr(fact_card, "extract_fact_card_sync", boom)
    video = make_video(tmp_path / "e.mp4", 15, 10)
    with pytest.raises(pl.VisionPipelineError) as e:
        pl.analyze_video_sync("case-1", video, options=pl.PipelineOptions(vlm=object()))
    assert (e.value.stage, e.value.code, e.value.retryable) == ("vlm", "vlm_failed", True)
