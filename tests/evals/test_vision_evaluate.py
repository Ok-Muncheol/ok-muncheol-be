from evals.vision.evaluate import evaluate, format_table
from evals.vision.keyframes import pick_adaptive, pick_uniform


def gt(example_id, fields, known=True, in_scope=True):
    return {"example_id": example_id, "fields": fields, "party_order_known": known,
            "in_mvp_scope": in_scope}


def pred(example_id, **fields):
    return {"example_id": example_id,
            "fields": {k: {"value": v, "model_confidence": 0.8} for k, v in fields.items()}}


def test_evaluate_skips_none_gt_and_counts_null_predictions():
    report = evaluate(
        [gt("a", {"location_type": "signalized_intersection", "night": None,
                  "movement_A": "straight"})],
        [pred("a", location_type="signalized_intersection", movement_A=None)],
    )
    assert report["scored_examples"] == 1
    assert "night" not in report["fields"]
    assert report["fields"]["location_type"]["accuracy"] == 1.0
    assert report["fields"]["movement_A"]["pred_null_rate"] == 1.0
    assert report["fields"]["movement_A"]["confusion"] == {"straight": {"None": 1}}


def test_swapped_pair_counts_only_when_party_order_unknown():
    gts = [gt("cc", {"movement_A": "left_turn", "movement_B": "straight"}, known=False),
           gt("bb", {"movement_A": "left_turn", "movement_B": "straight"}, known=True)]
    preds = [pred("cc", movement_A="straight", movement_B="left_turn"),
             pred("bb", movement_A="straight", movement_B="left_turn")]
    report = evaluate(gts, preds)
    assert report["fields"]["movement_A"]["correct"] == 1
    assert report["fields"]["movement_A"]["swapped_correct"] == 1
    assert report["fields"]["movement_A"]["swap_would_fix"] == 2


def test_mvp_only_filter_and_missing_predictions():
    report = evaluate([gt("x", {"location_type": "other"}, in_scope=False),
                       gt("y", {"location_type": "other"})], [])
    assert report["scored_examples"] == 0
    assert report["missing_predictions"] == 1
    assert "field" in format_table(report)


def test_pick_uniform_and_adaptive_shapes():
    assert pick_uniform(150, 5) == [0, 37, 74, 112, 149]
    assert pick_uniform(3, 5) == [0, 1, 2]
    flat = [0.0] * 150
    assert pick_adaptive(flat, 5) == pick_uniform(150, 5)
    spike = [0.0] * 150
    spike[100] = 10.0
    picks = pick_adaptive(spike, 6)
    assert picks[0] == 0 and picks[-1] == 149 and 100 in picks and len(picks) == 6
    burst = [1.0 if 90 <= i <= 110 else 0.01 for i in range(150)]
    picks = pick_adaptive(burst, 8)
    assert sum(1 for p in picks if 90 <= p <= 110) >= 4


def test_field_mean_excludes_given_fields():
    gt = [{"example_id": "x", "in_mvp_scope": True, "party_order_known": True,
           "fields": {"location_type": "other", "movement_A": "straight", "accident_type": "c"}}]
    pred = [{"example_id": "x", "meta": {"known_fields": ["location_type"]},
             "fields": {"location_type": {"value": "other"}, "movement_A": {"value": "left_turn"},
                        "accident_type": {"value": "c"}}}]
    mean = evaluate(gt, pred)["field_mean"]
    assert mean["fields"] == ["movement_A"]
    assert mean["macro_accuracy"] == 0.0
    assert "location_type" in mean["given_fields"]


def test_classify_turn_thresholds():
    from evals.vision.trajectory import classify_turn

    assert classify_turn(5.0) == "straight"
    assert classify_turn(40.0) == "left_turn"
    assert classify_turn(-40.0) == "right_turn"
    assert classify_turn(150.0) == "u_turn"


def test_describe_mentions_ego_turn_and_tracks():
    from evals.vision.trajectory import TrackPoint, TrajectoryFeatures, VehicleTrack, describe

    track = VehicleTrack(1, "car", [TrackPoint(0.0, 0.5, 0.5, 0.1, 0.1)])
    text = describe(TrajectoryFeatures(15.0, 150, [10.0, 40.0], 40.0, "left_turn", [track]))
    assert "left_turn" in text and "#1(car" in text


def test_select_rows_applies_gate_and_simple_scope():
    from evals.vision.evaluate import is_simple_scope, select_rows

    rows = [
        {"in_mvp_scope": True, "video_quality": {"sharpness": 20.0},
         "fields": {"movement_A": "straight", "movement_B": "straight"}},
        {"in_mvp_scope": True, "video_quality": {"sharpness": 90.0},
         "fields": {"movement_A": "u_turn", "movement_B": "straight"}},
        {"in_mvp_scope": True, "video_quality": {"sharpness": 90.0},
         "fields": {"movement_A": "left_turn", "movement_B": "straight"}},
    ]
    assert len(select_rows(rows, min_sharpness=50.0)) == 2
    assert len(select_rows(rows, min_sharpness=50.0, simple_scope=True)) == 1
    assert not is_simple_scope({"movement_A": "right_turn", "movement_B": "right_turn"})


def test_select_rows_drops_duplicates_by_default():
    from evals.vision.evaluate import select_rows

    rows = [
        {"in_mvp_scope": True, "fields": {}, "duplicate_conflict": True},
        {"in_mvp_scope": True, "fields": {}, "duplicate_of": "x"},
        {"in_mvp_scope": True, "fields": {}},
    ]
    assert len(select_rows(rows)) == 1
    assert len(select_rows(rows, include_duplicates=True)) == 3


def test_group_duplicates_by_hamming_distance():
    from evals.vision.dedupe import annotate, group_duplicates

    hashes = {"a": [0b1111, 0, 0, 0], "b": [0b1110, 0, 0, 0], "c": [0, 0xFFFF, 0xFFFF, 0]}
    groups = group_duplicates(hashes, hash_sure=2)
    assert groups == [["a", "b"]]
    rows = [{"example_id": i, "aihub": {"accident_type": t}}
            for i, t in (("a", 1), ("b", 2), ("c", 1))]
    assert annotate(rows, groups) == (1, 0)
    assert rows[0]["duplicate_conflict"] and rows[1]["duplicate_conflict"]
    assert not rows[2]["duplicate_conflict"]


def test_manual_pairs_force_a_group():
    from evals.vision.dedupe import group_duplicates

    hashes = {"a": [0, 0, 0, 0], "b": [0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF]}
    assert group_duplicates(hashes, hash_sure=2) == []
    assert group_duplicates(hashes, hash_sure=2, manual_pairs=[("a", "b")]) == [["a", "b"]]


def test_distance_allows_time_shift():
    from evals.vision.dedupe import distance

    a = [1, 2, 3, 4, 5, 6]
    shifted = [9, 1, 2, 3, 4, 5]  # 한 프레임 늦게 시작한 사본
    assert distance(a, shifted, max_shift=1) == 0.0
    assert distance(a, shifted, max_shift=0) > 0.0
