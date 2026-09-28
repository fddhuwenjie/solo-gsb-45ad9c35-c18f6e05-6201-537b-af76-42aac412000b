from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

PLAIN_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
}

INSIDE = (0.005, 0.005)
OUTSIDE = (0.02, 0.02)


def pt(device, sec, inside=True):
    lat, lon = INSIDE if inside else OUTSIDE
    return {
        "device_id": device,
        "ts": (BASE + timedelta(seconds=sec)).isoformat(),
        "lat": lat,
        "lon": lon,
    }


def iso(sec):
    return (BASE + timedelta(seconds=sec)).isoformat()


def ts(sec):
    return BASE + timedelta(seconds=sec)


def create_fence(client):
    r = client.post("/fences", json={"name": "容量测试", "geometry": PLAIN_FENCE})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_rule(client, fence_id, sec, max_occupancy):
    r = client.post(
        f"/fences/{fence_id}/capacity-rules",
        json={"effective_from": iso(sec), "max_occupancy": max_occupancy},
    )
    assert r.status_code == 201, r.text
    return r.json()


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def occupancy(client, fence_id, start, end):
    r = client.get(
        f"/fences/{fence_id}/occupancy", params={"start": iso(start), "end": iso(end)}
    )
    assert r.status_code == 200, r.text
    return r.json()["segments"]


def seg_times(seg):
    return (
        datetime.fromisoformat(seg["start"]),
        datetime.fromisoformat(seg["end"]),
    )


def test_capacity_rule_crud_and_validation(client):
    fence_id = create_fence(client)

    # 围栏不存在 -> 404
    r = client.post(
        "/fences/999/capacity-rules",
        json={"effective_from": iso(0), "max_occupancy": 5},
    )
    assert r.status_code == 404
    assert client.get("/fences/999/capacity-rules").status_code == 404

    # 上限必须是正整数
    for bad in (0, -3):
        r = client.post(
            f"/fences/{fence_id}/capacity-rules",
            json={"effective_from": iso(0), "max_occupancy": bad},
        )
        assert r.status_code == 422

    rule = add_rule(client, fence_id, 100, 3)
    assert rule["fence_id"] == fence_id
    assert rule["max_occupancy"] == 3
    assert datetime.fromisoformat(rule["effective_from"]) == ts(100)

    # 同一围栏同一生效时刻不得重复 -> 409；不同时刻可以
    r = client.post(
        f"/fences/{fence_id}/capacity-rules",
        json={"effective_from": iso(100), "max_occupancy": 9},
    )
    assert r.status_code == 409
    add_rule(client, fence_id, 50, 7)

    # 列表按生效时刻升序
    rules = client.get(f"/fences/{fence_id}/capacity-rules").json()
    assert [r["max_occupancy"] for r in rules] == [7, 3]

    # 删除
    rid = rules[0]["id"]
    assert client.delete(f"/fences/{fence_id}/capacity-rules/{rid}").status_code == 204
    assert client.delete(f"/fences/{fence_id}/capacity-rules/{rid}").status_code == 404
    remaining = client.get(f"/fences/{fence_id}/capacity-rules").json()
    assert [r["max_occupancy"] for r in remaining] == [3]


def test_delete_fence_removes_capacity_rules(client):
    fence_id = create_fence(client)
    add_rule(client, fence_id, 0, 2)
    assert client.delete(f"/fences/{fence_id}").status_code == 204
    assert client.get(f"/fences/{fence_id}/capacity-rules").status_code == 404


def test_occupancy_segments_with_truncation_and_exceedance(client):
    fence_id = create_fence(client)
    add_rule(client, fence_id, 0, 1)     # t=0 起上限 1
    add_rule(client, fence_id, 120, 2)   # t=120 起上限 2

    submit(client, [
        pt("dev1", 0), pt("dev1", 100, inside=False),   # 查询开始前已进入
        pt("dev2", 50), pt("dev2", 150, inside=False),
        pt("dev3", 200),                                # 查询结束仍未离开
    ])

    segs = occupancy(client, fence_id, 25, 250)
    assert [seg_times(s) for s in segs] == [
        (ts(a), ts(b))
        for a, b in [(25, 50), (50, 100), (100, 120), (120, 150), (150, 200), (200, 250)]
    ]
    assert [s["count"] for s in segs] == [1, 2, 1, 1, 0, 1]
    assert [s["limit"] for s in segs] == [1, 1, 1, 2, 2, 2]

    over = segs[1]
    assert over["exceeded"] is True
    assert over["limit"] == 1
    assert over["count"] == 2
    assert over["excess"] == 1
    assert over["devices"] == ["dev1", "dev2"]

    for s in segs[:1] + segs[2:]:
        assert s["exceeded"] is False
        assert s["excess"] is None
        assert s["devices"] is None


def test_simultaneous_events_and_rule_switch_form_single_boundary(client):
    fence_id = create_fence(client)
    add_rule(client, fence_id, 0, 5)
    add_rule(client, fence_id, 100, 1)

    submit(client, [
        pt("dev1", 0), pt("dev1", 100, inside=False),    # t=100 离开
        pt("dev2", 100), pt("dev2", 200, inside=False),  # t=100 进入
        pt("dev3", 100), pt("dev3", 150, inside=False),  # t=100 进入
    ])

    segs = occupancy(client, fence_id, 0, 200)
    # t=100 的 1 次离开 + 2 次进入 + 规则切换合并为一个原子边界
    assert [seg_times(s) for s in segs] == [
        (ts(0), ts(100)),
        (ts(100), ts(150)),
        (ts(150), ts(200)),
    ]
    assert segs[0]["count"] == 1 and segs[0]["limit"] == 5
    assert segs[0]["exceeded"] is False

    assert segs[1]["exceeded"] is True
    assert segs[1]["limit"] == 1
    assert segs[1]["count"] == 2
    assert segs[1]["excess"] == 1
    assert segs[1]["devices"] == ["dev2", "dev3"]

    assert segs[2]["count"] == 1 and segs[2]["limit"] == 1
    assert segs[2]["exceeded"] is False


def test_adjacent_identical_state_segments_are_merged(client):
    fence_id = create_fence(client)
    add_rule(client, fence_id, 0, 3)
    add_rule(client, fence_id, 50, 3)  # 上限相同，规则切换不产生新分段
    submit(client, [pt("dev1", 0), pt("dev1", 100, inside=False)])

    segs = occupancy(client, fence_id, 0, 100)
    assert len(segs) == 1
    assert seg_times(segs[0]) == (ts(0), ts(100))
    assert segs[0]["count"] == 1 and segs[0]["limit"] == 3


def test_occupancy_without_rules_never_exceeds(client):
    fence_id = create_fence(client)
    submit(client, [pt("dev1", 10), pt("dev2", 20)])

    segs = occupancy(client, fence_id, 0, 100)
    assert [s["count"] for s in segs] == [0, 1, 2]
    assert all(s["limit"] is None for s in segs)
    assert all(s["exceeded"] is False for s in segs)


def test_occupancy_empty_window_is_single_segment(client):
    fence_id = create_fence(client)
    segs = occupancy(client, fence_id, 0, 100)
    assert len(segs) == 1
    assert seg_times(segs[0]) == (ts(0), ts(100))
    assert segs[0]["count"] == 0
    assert segs[0]["limit"] is None
    assert segs[0]["exceeded"] is False


def test_half_open_window_boundaries(client):
    fence_id = create_fence(client)
    submit(client, [
        pt("dev1", 0), pt("dev1", 50, inside=False),  # 恰好在 start 离开 -> 不计入
        pt("dev2", 100),                              # 恰好在 end 进入 -> 不计入
        pt("dev3", 60), pt("dev3", 90, inside=False),
    ])

    segs = occupancy(client, fence_id, 50, 100)
    assert [seg_times(s) for s in segs] == [
        (ts(50), ts(60)),
        (ts(60), ts(90)),
        (ts(90), ts(100)),
    ]
    assert [s["count"] for s in segs] == [0, 1, 0]


def test_occupancy_requires_valid_window_and_fence(client):
    fence_id = create_fence(client)
    r = client.get(
        f"/fences/{fence_id}/occupancy", params={"start": iso(100), "end": iso(100)}
    )
    assert r.status_code == 422
    r = client.get(
        f"/fences/{fence_id}/occupancy", params={"start": iso(200), "end": iso(100)}
    )
    assert r.status_code == 422
    r = client.get("/fences/999/occupancy", params={"start": iso(0), "end": iso(1)})
    assert r.status_code == 404


def test_backfilled_points_trigger_recomputed_occupancy(client):
    fence_id = create_fence(client)
    add_rule(client, fence_id, 0, 1)

    submit(client, [
        pt("dev1", 0), pt("dev1", 300, inside=False),
        pt("dev2", 100), pt("dev2", 200, inside=False),
    ])
    over = [s for s in occupancy(client, fence_id, 0, 300) if s["exceeded"]]
    assert len(over) == 1
    assert seg_times(over[0]) == (ts(100), ts(200))
    assert over[0]["devices"] == ["dev1", "dev2"]

    # 补录乱序点：dev2 实际在 t=150 已离开 -> 重算后超限区间缩短，旧片段不残留
    submit(client, [pt("dev2", 150, inside=False)])

    segs = occupancy(client, fence_id, 0, 300)
    over = [s for s in segs if s["exceeded"]]
    assert len(over) == 1
    assert seg_times(over[0]) == (ts(100), ts(150))
    assert over[0]["devices"] == ["dev1", "dev2"]
    later = [s for s in segs if datetime.fromisoformat(s["start"]) >= ts(150)]
    assert all(not s["exceeded"] for s in later)
    assert [s["count"] for s in later] == [1]
