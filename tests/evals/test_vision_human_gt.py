from evals.vision.human_gt import apply_label, build


def _gt(**fields):
    base = {
        "accident_type": "car_to_car", "location_type": "unsignalized_intersection",
        "road_priority": None, "movement_A": "straight", "movement_B": "left_turn",
        "signal_A": "green", "signal_B": "red", "entry_order": "first", "night": None,
    }
    base.update(fields)
    return {"example_id": "x", "fields": base, "party_order_known": False,
            "party_mapping": {"chart_party_A_id": "A", "chart_party_B_id": "B"}}


def test_ego_is_b_keeps_sides_and_overwrites_movement():
    row = apply_label(_gt(), {"ego_party": "B", "movement_ego": "right_turn",
                              "movement_other": "u_turn", "status": "scored"})
    f = row["fields"]
    assert (f["movement_B"], f["movement_A"]) == ("right_turn", "u_turn")
    assert (f["signal_B"], f["signal_A"]) == ("red", "green")
    assert f["entry_order"] == "first"
    assert row["party_order_known"] is True
    assert row["human_label"]["aihub_movement"] == {"A": "straight", "B": "left_turn"}


def test_ego_is_a_swaps_signals_and_entry_order():
    row = apply_label(_gt(), {"ego_party": "A", "movement_ego": "straight",
                              "movement_other": "left_turn", "status": "scored"})
    f = row["fields"]
    assert (f["signal_B"], f["signal_A"]) == ("green", "red")
    assert f["entry_order"] == "second"  # A가 먼저 → 촬영 차량(B로 옮김)이 먼저
    assert row["party_mapping"]["video_ego_party_id"] == "A"


def test_entry_order_filled_from_note_only_when_missing():
    label = {"ego_party": "B", "movement_ego": "straight", "movement_other": "straight",
             "note": "촬영 차량 선진입 직진"}
    filled = apply_label(_gt(entry_order=None), label)
    assert filled["fields"]["entry_order"] == "second"
    kept = apply_label(_gt(entry_order="first"),
                       {"ego_party": "B", "movement_ego": "straight", "movement_other": "straight",
                        "note": "선진입"})
    assert kept["fields"]["entry_order"] == "first"


def test_build_skips_excluded_and_reports_missing():
    gt_rows = [dict(_gt(), example_id="a"), dict(_gt(), example_id="b")]
    labels = [
        {"example_id": "a", "status": "scored", "ego_party": "B",
         "movement_ego": "straight", "movement_other": "straight"},
        {"example_id": "b", "status": "excluded", "ego_party": "exclude"},
        {"example_id": "c", "status": "duplicate", "ego_party": "A",
         "movement_ego": "straight", "movement_other": "straight"},
    ]
    rows, missing = build(gt_rows, labels)
    assert [r["example_id"] for r in rows] == ["a"]
    assert missing == ["c"]
