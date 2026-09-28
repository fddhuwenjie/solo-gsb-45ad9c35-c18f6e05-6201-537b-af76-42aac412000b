from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

# 外环 (0,0)-(0.01,0.01)，约 1.1km 见方；中央孔洞 (0.004,0.004)-(0.006,0.006)
FENCE_WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]],
        [[0.004, 0.004], [0.006, 0.004], [0.006, 0.006], [0.004, 0.006], [0.004, 0.004]],
    ],
}

PLAIN_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
}


def pt(device, sec, lat, lon):
    return {
        "device_id": device,
        "ts": (BASE + timedelta(seconds=sec)).isoformat(),
        "lat": lat,
        "lon": lon,
    }


def create_fence(client, geometry=PLAIN_FENCE, config=None, name="测试围栏"):
    payload = {"name": name, "geometry": geometry, "config": config or {}}
    r = client.post("/fences", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def get_events(client, **params):
    r = client.get("/events", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# 外→内→孔洞(外)→内→外，默认零缓冲零停留
BASIC_TRACK = [
    pt("dev1", 0, 0.02, 0.02),      # 外部
    pt("dev1", 10, 0.002, 0.005),   # 内部（孔洞下方）
    pt("dev1", 20, 0.005, 0.005),   # 孔洞内 -> 外部
    pt("dev1", 30, 0.005, 0.002),   # 内部（孔洞左侧）
    pt("dev1", 40, 0.02, 0.02),     # 外部
]


def test_enter_leave_with_hole(client):
    create_fence(client, geometry=FENCE_WITH_HOLE)
    result = submit(client, BASIC_TRACK)
    assert result == {"accepted": 5, "duplicates": 0, "devices_updated": 1}

    events = get_events(client, device_id="dev1")
    assert [e["type"] for e in events] == ["enter", "leave", "enter", "leave"]
    # 事件时间取条件首次满足的轨迹时刻
    assert datetime.fromisoformat(events[0]["ts"]) == BASE + timedelta(seconds=10)
    assert datetime.fromisoformat(events[1]["ts"]) == BASE + timedelta(seconds=20)


def test_out_of_order_points_same_result(client):
    create_fence(client, geometry=FENCE_WITH_HOLE)
    submit(client, [BASIC_TRACK[2], BASIC_TRACK[0]])            # t=20, t=0
    submit(client, [BASIC_TRACK[4], BASIC_TRACK[1], BASIC_TRACK[3]])  # t=40,10,30

    events = get_events(client, device_id="dev1")
    assert [e["type"] for e in events] == ["enter", "leave", "enter", "leave"]
    assert [datetime.fromisoformat(e["ts"]) for e in events] == [
        BASE + timedelta(seconds=s) for s in (10, 20, 30, 40)
    ]


def test_duplicate_submission_is_idempotent(client):
    create_fence(client, geometry=FENCE_WITH_HOLE)
    # 同一批次内的重复点也要去重
    first = submit(client, BASIC_TRACK + [BASIC_TRACK[1]])
    assert first["accepted"] == 5
    assert first["duplicates"] == 1

    again = submit(client, BASIC_TRACK)
    assert again["accepted"] == 0
    assert again["duplicates"] == 5

    overlap = submit(client, [BASIC_TRACK[0], pt("dev1", 50, 0.02, 0.02)])
    assert overlap["accepted"] == 1
    assert overlap["duplicates"] == 1

    events = get_events(client, device_id="dev1")
    assert [e["type"] for e in events] == ["enter", "leave", "enter", "leave"]


def test_exit_buffer_suppresses_boundary_jitter(client):
    # 离开缓冲 100m：越过边界但在 100m 内徘徊不算离开
    create_fence(client, config={"exit_buffer_m": 100})
    submit(client, [
        pt("dev1", 0, 0.005, 0.005),     # 内部 -> enter
        pt("dev1", 10, 0.005, 0.0105),   # 界外约 56m，迟滞带内 -> 保持 inside
        pt("dev1", 20, 0.005, 0.0095),   # 回到界内
        pt("dev1", 30, 0.005, 0.012),    # 界外约 222m > 100m -> leave
    ])
    events = get_events(client, device_id="dev1")
    assert [e["type"] for e in events] == ["enter", "leave"]
    assert datetime.fromisoformat(events[1]["ts"]) == BASE + timedelta(seconds=30)


def test_enter_dwell_debounces_short_visits(client):
    # 进入需持续 30s 才确认；短暂进入又离开不产生事件
    create_fence(client, config={"enter_dwell_s": 30})
    submit(client, [
        pt("dev1", 0, 0.02, 0.02),      # 外部
        pt("dev1", 10, 0.005, 0.005),   # 进入
        pt("dev1", 20, 0.02, 0.02),     # 10s 后离开，未达 dwell -> 无事件
        pt("dev1", 40, 0.005, 0.005),   # 再次进入，dwell 从 t=40 计
        pt("dev1", 75, 0.005, 0.005),   # 持续 35s >= 30s -> enter@t=40
    ])
    events = get_events(client, device_id="dev1")
    assert [e["type"] for e in events] == ["enter"]
    assert datetime.fromisoformat(events[0]["ts"]) == BASE + timedelta(seconds=40)


def test_stay_event_after_min_duration(client):
    create_fence(client, config={"stay_min_s": 60})
    submit(client, [
        pt("dev1", 0, 0.005, 0.005),
        pt("dev1", 30, 0.005, 0.005),
        pt("dev1", 100, 0.005, 0.005),   # 连续停留 100s >= 60s -> stay@t=60
        pt("dev1", 200, 0.02, 0.02),
    ])
    events = get_events(client, device_id="dev1")
    assert [e["type"] for e in events] == ["enter", "stay", "leave"]
    assert datetime.fromisoformat(events[1]["ts"]) == BASE + timedelta(seconds=60)


def test_device_status(client):
    fence_id = create_fence(client, geometry=FENCE_WITH_HOLE)
    submit(client, BASIC_TRACK)                       # dev1 最终在外部
    submit(client, [pt("dev2", 0, 0.002, 0.005)])     # dev2 在内部

    r = client.get("/devices/dev1/status")
    assert r.status_code == 200
    st = r.json()["fences"][0]
    assert st["fence_id"] == fence_id
    assert st["state"] == "outside"
    assert datetime.fromisoformat(st["since"]) == BASE + timedelta(seconds=40)

    st2 = client.get("/devices/dev2/status").json()["fences"][0]
    assert st2["state"] == "inside"
    assert datetime.fromisoformat(st2["since"]) == BASE


def test_events_time_range_and_type_filter(client):
    fence_id = create_fence(client, geometry=FENCE_WITH_HOLE)
    submit(client, BASIC_TRACK)

    events = get_events(
        client,
        device_id="dev1",
        start=(BASE + timedelta(seconds=15)).isoformat(),
        end=(BASE + timedelta(seconds=35)).isoformat(),
    )
    assert [e["type"] for e in events] == ["leave", "enter"]

    enters = get_events(client, fence_id=fence_id, type="enter")
    assert len(enters) == 2 and all(e["type"] == "enter" for e in enters)


def test_fence_applies_to_existing_points(client):
    submit(client, BASIC_TRACK)  # 先有点，后建围栏
    assert get_events(client, device_id="dev1") == []
    create_fence(client, geometry=FENCE_WITH_HOLE)
    assert len(get_events(client, device_id="dev1")) == 4


def test_invalid_fence_rejected(client):
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [0.01, 0.01], [0.01, 0], [0, 0.01], [0, 0]]],
    }
    r = client.post("/fences", json={"name": "bad", "geometry": bowtie})
    assert r.status_code == 422

    r = client.post("/fences", json={
        "name": "bad-config",
        "geometry": PLAIN_FENCE,
        "config": {"enter_buffer_m": 200, "exit_buffer_m": 100},
    })
    assert r.status_code == 422


def test_delete_fence_removes_events_and_state(client):
    fence_id = create_fence(client, geometry=FENCE_WITH_HOLE)
    submit(client, BASIC_TRACK)
    assert len(get_events(client, fence_id=fence_id)) == 4

    r = client.delete(f"/fences/{fence_id}")
    assert r.status_code == 204
    assert get_events(client, fence_id=fence_id) == []
    assert client.get("/devices/dev1/status").json()["fences"] == []
