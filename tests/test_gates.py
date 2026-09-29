from datetime import datetime, timedelta, timezone

import pytest

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

# 东西向门线（坐标顺序：西 -> 东），右=南、左=北
GATE_EW = {
    "type": "LineString",
    "coordinates": [[0.0025, 0.005], [0.0075, 0.005]],
}


def pt(device, sec, lat, lon):
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


def create_fence(client, geometry=PLAIN_FENCE):
    r = client.post("/fences", json={"name": "门线测试围栏", "geometry": geometry})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def create_gate(client, fence_id, geometry, fwd="入场", rev="出场", min_disp=0.0,
                expect=201):
    payload = {
        "geometry": geometry,
        "forward_name": fwd,
        "reverse_name": rev,
        "min_displacement_m": min_disp,
    }
    r = client.post(f"/fences/{fence_id}/gates", json=payload)
    assert r.status_code == expect, r.text
    return r.json() if expect == 201 else None


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def get_crossings(client, **params):
    r = client.get("/crossings", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_forward_and_reverse_crossing(client):
    fid = create_fence(client)
    gate = create_gate(client, fid, GATE_EW, fwd="南->北", rev="北->南")
    assert gate["fence_id"] == fid
    assert gate["forward_name"] == "南->北"
    assert gate["min_displacement_m"] == 0.0

    submit(client, [
        pt("dev1", 0, 0.002, 0.005),    # 门线以南
        pt("dev1", 10, 0.004, 0.005),   # 仍在南侧
        pt("dev1", 20, 0.006, 0.005),   # 北侧 -> 段 10~20 正向横穿
        pt("dev1", 30, 0.004, 0.005),   # 回到南侧 -> 段 20~30 反向横穿
    ])

    crossings = get_crossings(client, device_id="dev1")
    assert len(crossings) == 2
    c0, c1 = crossings
    assert c0["gate_id"] == gate["id"] and c0["fence_id"] == fid
    assert c0["direction"] == "forward" and c0["direction_name"] == "南->北"
    assert c1["direction"] == "reverse" and c1["direction_name"] == "北->南"

    # 穿越时刻按交点在线段上的比例插值：门线位于 0.005，两侧等距 -> t=0.5
    assert datetime.fromisoformat(c0["ts"]) == ts(15)
    assert datetime.fromisoformat(c1["ts"]) == ts(25)
    assert c0["lat"] == pytest.approx(0.005, abs=1e-9)
    assert c0["lon"] == pytest.approx(0.005, abs=1e-9)


def test_interpolated_ts_follows_intersection_ratio(client):
    fid = create_fence(client)
    create_gate(client, fid, GATE_EW)
    # 段 (0.002,0.003) -> (0.008,0.006)，交点处纬度占比 (0.005-0.002)/(0.008-0.002)=0.5，
    # 但交点横坐标按线性插值得 lon≈0.0045，时间比例由米制投影决定
    submit(client, [
        pt("dev1", 0, 0.002, 0.003),
        pt("dev1", 100, 0.008, 0.006),
    ])
    crossings = get_crossings(client, device_id="dev1")
    assert len(crossings) == 1
    frac_x = (0.005 - 0.002) / (0.008 - 0.002)  # 纬度方向（投影 y）比例
    expect_sec = frac_x * 100
    actual = (datetime.fromisoformat(crossings[0]["ts"]) - BASE).total_seconds()
    assert actual == pytest.approx(expect_sec, abs=1e-6)
    assert crossings[0]["lon"] == pytest.approx(0.003 + frac_x * (0.006 - 0.003), abs=1e-9)


def test_along_line_endpoint_touch_and_same_side_ignored(client):
    fid = create_fence(client)
    create_gate(client, fid, GATE_EW)
    submit(client, [
        # 仅接触门线端点：线段终点落在门线西端点
        pt("d", 0, 0.004, 0.0025),
        pt("d", 10, 0.005, 0.0025),
        # 仅接触门线端点：从门线东端点出发
        pt("d", 20, 0.005, 0.0075),
        pt("d", 30, 0.004, 0.009),
        # 沿线：与门线共线
        pt("d", 40, 0.005, 0.003),
        pt("d", 50, 0.005, 0.007),
        # 同侧擦碰：两端都在南侧，线段不跨门线
        pt("d", 60, 0.004, 0.003),
        pt("d", 70, 0.004, 0.007),
        # 端点恰好落在门线内部但不穿越
        pt("d", 80, 0.005, 0.005),
        pt("d", 90, 0.004, 0.006),
        pt("d", 100, 0.004, 0.007),  # 下一段两端同侧
    ])
    assert get_crossings(client, device_id="d") == []


def test_min_displacement_threshold(client):
    fid = create_fence(client)
    create_gate(client, fid, GATE_EW, min_disp=600.0)
    # 纬度跨 0.005 度约 556m < 600m，几何上横穿但位移不足 -> 忽略
    submit(client, [
        pt("dev1", 0, 0.0025, 0.005),
        pt("dev1", 10, 0.0075, 0.005),
    ])
    assert get_crossings(client, device_id="dev1") == []

    # 纬度跨 0.006 度约 667m >= 600m -> 正常核算（用另一台设备，避免与上面的点连成段）
    submit(client, [
        pt("dev2", 20, 0.002, 0.005),
        pt("dev2", 30, 0.008, 0.005),
    ])
    crossings = get_crossings(client, device_id="dev2")
    assert len(crossings) == 1
    assert datetime.fromisoformat(crossings[0]["ts"]) == ts(25)


def test_bent_gate_segments(client):
    fid = create_fence(client)
    # 折角门线：先东西向后南北向，折点 (lon=0.005,lat=0.005)
    bent = {
        "type": "LineString",
        "coordinates": [[0.002, 0.005], [0.005, 0.005], [0.005, 0.008]],
    }
    create_gate(client, fid, bent)

    # 横穿第一段（东西向）：南->北 = 正向
    submit(client, [
        pt("dev1", 0, 0.004, 0.003),
        pt("dev1", 10, 0.006, 0.003),
    ])
    crossings = get_crossings(client, device_id="dev1")
    assert len(crossings) == 1 and crossings[0]["direction"] == "forward"

    # 横穿第二段（南北向，坐标顺序南->北；右=东）：东->西 = 正向
    submit(client, [
        pt("dev2", 0, 0.006, 0.006),
        pt("dev2", 10, 0.006, 0.004),
    ])
    crossings = get_crossings(client, device_id="dev2")
    assert len(crossings) == 1 and crossings[0]["direction"] == "forward"

    # 沿底臂到达折点并继续东行：与第一段共线（沿线）-> 忽略
    submit(client, [
        pt("dev3", 0, 0.004, 0.005),
        pt("dev3", 10, 0.006, 0.005),
    ])
    assert get_crossings(client, device_id="dev3") == []

    # 真正穿过折点（轨迹自东南穿到西北，折点两侧邻接段分居轨迹两侧）：
    # 归并为一次 forward，交点即折点，t=0.5 -> 时刻插值在中点
    submit(client, [
        pt("dev4", 0, 0.004, 0.006),
        pt("dev4", 10, 0.006, 0.004),
    ])
    crossings = get_crossings(client, device_id="dev4")
    assert len(crossings) == 1
    assert crossings[0]["direction"] == "forward"
    assert datetime.fromisoformat(crossings[0]["ts"]) == ts(5)
    assert crossings[0]["lat"] == pytest.approx(0.005, abs=1e-9)
    assert crossings[0]["lon"] == pytest.approx(0.005, abs=1e-9)

    # 反向穿过同一折点：一次 reverse
    submit(client, [
        pt("dev5", 0, 0.006, 0.004),
        pt("dev5", 10, 0.004, 0.006),
    ])
    crossings = get_crossings(client, device_id="dev5")
    assert len(crossings) == 1 and crossings[0]["direction"] == "reverse"
    assert datetime.fromisoformat(crossings[0]["ts"]) == ts(5)

    # 折点同侧擦碰（轨迹自西南穿向东北，两邻接臂同在轨迹一侧）-> 忽略
    submit(client, [
        pt("dev6", 0, 0.004, 0.004),
        pt("dev6", 10, 0.006, 0.006),
    ])
    assert get_crossings(client, device_id="dev6") == []


def test_vertex_crossing_interpolation_ratio(client):
    fid = create_fence(client)
    bent = {
        "type": "LineString",
        "coordinates": [[0.002, 0.005], [0.005, 0.005], [0.005, 0.008]],
    }
    create_gate(client, fid, bent)
    # 自东南（lat 低、lon 大）穿过折点到西北（lat 高、lon 小），折点处 t=0.75
    submit(client, [
        pt("dev1", 0, 0.0045, 0.0055),
        pt("dev1", 100, 0.005166666666666667, 0.004833333333333333),
    ])
    crossings = get_crossings(client, device_id="dev1")
    assert len(crossings) == 1
    assert crossings[0]["direction"] == "forward"
    assert crossings[0]["lat"] == pytest.approx(0.005, abs=1e-9)
    assert crossings[0]["lon"] == pytest.approx(0.005, abs=1e-9)
    actual = (datetime.fromisoformat(crossings[0]["ts"]) - BASE).total_seconds()
    assert actual == pytest.approx(75.0, abs=1e-6)


def test_crossing_at_internal_vertex_of_multi_point_gate(client):
    fid = create_fence(client)
    # 含三个坐标点的直线门线（中间是内部折点）
    create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.002, 0.005], [0.005, 0.005], [0.008, 0.005]],
    })
    # 轨迹恰好穿过中间折点：归并为一次 forward
    submit(client, [
        pt("dev1", 0, 0.004, 0.005),
        pt("dev1", 20, 0.006, 0.005),
    ])
    crossings = get_crossings(client, device_id="dev1")
    assert len(crossings) == 1
    assert crossings[0]["direction"] == "forward"
    assert datetime.fromisoformat(crossings[0]["ts"]) == ts(10)
    assert crossings[0]["lat"] == pytest.approx(0.005, abs=1e-9)
    assert crossings[0]["lon"] == pytest.approx(0.005, abs=1e-9)

    # 反向穿过同一内部折点：reverse
    submit(client, [
        pt("dev2", 0, 0.006, 0.005),
        pt("dev2", 20, 0.004, 0.005),
    ])
    crossings = get_crossings(client, device_id="dev2")
    assert len(crossings) == 1 and crossings[0]["direction"] == "reverse"
    assert datetime.fromisoformat(crossings[0]["ts"]) == ts(10)


def test_vertex_crossing_backfill_recompute(client):
    fid = create_fence(client)
    create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.002, 0.005], [0.005, 0.005], [0.005, 0.008]],
    })
    # 先有 t=0、t=20 两点：直接连线在门线东端以外，不触门线
    submit(client, [
        pt("dev1", 0, 0.006, 0.005),
        pt("dev1", 20, 0.006, 0.004),
    ])
    assert get_crossings(client, device_id="dev1") == []

    # 乱序补录 t=10 折点东南侧点：重排后段 0~10 不穿门线，段 10~20 沿 SE->NW
    # 恰好穿过内部折点 (lat .005, lon .005)。重算补出一次 forward（@15），
    # 不残留旧结果、不重复
    submit(client, [pt("dev1", 10, 0.004, 0.006)])
    crossings = get_crossings(client, device_id="dev1")
    assert len(crossings) == 1
    assert crossings[0]["direction"] == "forward"
    assert datetime.fromisoformat(crossings[0]["ts"]) == ts(15)
    assert crossings[0]["lat"] == pytest.approx(0.005, abs=1e-9)
    assert crossings[0]["lon"] == pytest.approx(0.005, abs=1e-9)

    # 重复提交不产生重复记录
    submit(client, [pt("dev1", 10, 0.004, 0.006)])
    assert len(get_crossings(client, device_id="dev1")) == 1


def test_vertex_crossing_endpoints_and_track_ends_ignored(client):
    fid = create_fence(client)
    bent = {
        "type": "LineString",
        "coordinates": [[0.002, 0.005], [0.005, 0.005], [0.005, 0.008]],
    }
    create_gate(client, fid, bent)

    # 穿过门线首端点（西端点）：忽略
    submit(client, [
        pt("ep", 0, 0.004, 0.002),
        pt("ep", 10, 0.006, 0.002),
    ])
    # 穿过门线末端点（北端点）：忽略
    submit(client, [
        pt("np", 0, 0.004, 0.005),
        pt("np", 10, 0.006, 0.005),
    ])
    # 轨迹起点恰好落在内部折点：忽略
    submit(client, [
        pt("st", 0, 0.005, 0.005),
        pt("st", 10, 0.004, 0.006),
    ])
    # 轨迹终点恰好落在内部折点：忽略
    submit(client, [
        pt("en", 0, 0.006, 0.004),
        pt("en", 10, 0.005, 0.005),
    ])
    # 轨迹止于门线首端点 / 始于门线末端点：忽略
    submit(client, [
        pt("te", 0, 0.004, 0.003),
        pt("te", 10, 0.005, 0.002),
    ])
    submit(client, [
        pt("tn", 0, 0.005, 0.008),
        pt("tn", 10, 0.006, 0.009),
    ])
    assert get_crossings(client) == []


def test_multiple_gates_same_segment_ordered_by_ts_then_gate_id(client):
    fid = create_fence(client)
    g_lo = create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.002, 0.004], [0.008, 0.004]],
    })
    g_hi = create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.002, 0.006], [0.008, 0.006]],
    })

    # 单条线段依次穿过低纬度门线（t=1/3）与高纬度门线（t=2/3）
    submit(client, [
        pt("dev1", 0, 0.002, 0.005),
        pt("dev1", 60, 0.008, 0.005),
    ])
    crossings = get_crossings(client, device_id="dev1")
    assert [c["gate_id"] for c in crossings] == [g_lo["id"], g_hi["id"]]
    assert datetime.fromisoformat(crossings[0]["ts"]) == ts(20)
    assert datetime.fromisoformat(crossings[1]["ts"]) == ts(40)


def test_query_filters_and_half_open_window(client):
    fid = create_fence(client)
    gate = create_gate(client, fid, GATE_EW)
    submit(client, [
        pt("dev1", 0, 0.002, 0.005),
        pt("dev1", 30, 0.008, 0.005),   # 正向穿越 @15
        pt("dev1", 60, 0.002, 0.005),   # 反向穿越 @45
    ])

    # 方向过滤
    fwd = get_crossings(client, gate_id=gate["id"], direction="forward")
    rev = get_crossings(client, gate_id=gate["id"], direction="reverse")
    assert len(fwd) == 1 and fwd[0]["direction"] == "forward"
    assert len(rev) == 1 and rev[0]["direction"] == "reverse"

    # fence_id 过滤
    assert len(get_crossings(client, fence_id=fid)) == 2
    assert get_crossings(client, fence_id=999) == []

    # 半开窗口 [start, end)
    win = get_crossings(client, device_id="dev1", start=iso(15), end=iso(45))
    assert [c["direction"] for c in win] == ["forward"]  # @15 含，@45 不含
    assert get_crossings(client, start=iso(0), end=iso(15)) == []
    assert len(get_crossings(client, start=iso(15), end=iso(46))) == 2

    # 只给一个边界 -> 422；start>=end -> 422
    r = client.get("/crossings", params={"start": iso(0)})
    assert r.status_code == 422
    r = client.get("/crossings", params={"start": iso(10), "end": iso(10)})
    assert r.status_code == 422

    # gate_id 不存在 -> 404
    assert client.get("/crossings", params={"gate_id": 999}).status_code == 404


def test_backfill_out_of_order_recomputes_without_duplicates(client):
    fid = create_fence(client)
    gate = create_gate(client, fid, GATE_EW)

    # 先有 t=0 南侧、t=20 南侧：无穿越
    submit(client, [
        pt("dev1", 0, 0.002, 0.005),
        pt("dev1", 20, 0.002, 0.005),
    ])
    assert get_crossings(client, device_id="dev1") == []

    # 乱序补录 t=10（门线北侧）：重排后段 0~10 正向横穿（@5）、段 10~20 反向横穿（@15）
    submit(client, [pt("dev1", 10, 0.008, 0.005)])
    crossings = get_crossings(client, device_id="dev1")
    assert [c["direction"] for c in crossings] == ["forward", "reverse"]
    assert datetime.fromisoformat(crossings[0]["ts"]) == ts(5)
    assert datetime.fromisoformat(crossings[1]["ts"]) == ts(15)
    assert all(c["gate_id"] == gate["id"] for c in crossings)

    # 继续乱序补录 t=5（更靠北，仍在北侧）：方向与条数不变；正向穿越时刻按
    # 新线段 (lat .002@t0 -> .009@t5) 重新插值为 5 * (.005-.002)/(.009-.002) = 15/7
    submit(client, [pt("dev1", 5, 0.009, 0.005)])
    again = get_crossings(client, device_id="dev1")
    assert len(again) == 2
    assert again[0]["direction"] == "forward"
    assert (datetime.fromisoformat(again[0]["ts"]) - BASE).total_seconds() == pytest.approx(
        5 * (0.005 - 0.002) / (0.009 - 0.002)
    )
    assert again[1]["direction"] == "reverse"
    assert datetime.fromisoformat(again[1]["ts"]) == ts(15)

    # 重复提交点不产生新穿越
    submit(client, [pt("dev1", 10, 0.008, 0.005)])
    assert len(get_crossings(client, device_id="dev1")) == 2


def test_gate_applies_to_existing_points(client):
    submit(client, [
        pt("dev1", 0, 0.002, 0.005),
        pt("dev1", 10, 0.008, 0.005),
    ])
    fid = create_fence(client)
    assert get_crossings(client, device_id="dev1") == []
    create_gate(client, fid, GATE_EW)
    crossings = get_crossings(client, device_id="dev1")
    assert len(crossings) == 1
    assert datetime.fromisoformat(crossings[0]["ts"]) == ts(5)


def test_delete_gate_and_fence_cascades_crossings(client):
    fid = create_fence(client)
    gate = create_gate(client, fid, GATE_EW)
    submit(client, [
        pt("dev1", 0, 0.002, 0.005),
        pt("dev1", 10, 0.008, 0.005),
    ])
    assert len(get_crossings(client, device_id="dev1")) == 1

    assert client.delete(f"/fences/{fid}/gates/{gate['id']}").status_code == 204
    assert get_crossings(client, device_id="dev1") == []
    assert client.get(f"/fences/{fid}/gates").json() == []
    # 重复删除 / 不匹配围栏 -> 404
    assert client.delete(f"/fences/{fid}/gates/{gate['id']}").status_code == 404

    # 删围栏连带清除门线与穿越
    gate2 = create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.002, 0.005], [0.008, 0.005]],
    })
    # gate2 创建即对存量 dev1 重算补出一条；再提交 dev2
    assert len(get_crossings(client, gate_id=gate2["id"], device_id="dev1")) == 1
    submit(client, [
        pt("dev2", 0, 0.002, 0.005),
        pt("dev2", 10, 0.008, 0.005),
    ])
    assert len(get_crossings(client, gate_id=gate2["id"], device_id="dev2")) == 1
    assert client.delete(f"/fences/{fid}").status_code == 204
    assert client.get("/crossings").json() == []
    assert client.get(f"/fences/{fid}/gates").status_code == 404


def test_gate_validation(client):
    fid = create_fence(client, geometry=FENCE_WITH_HOLE)

    # 围栏不存在
    assert client.post("/fences/999/gates", json={
        "geometry": GATE_EW, "forward_name": "a", "reverse_name": "b",
    }).status_code == 404

    # 不是 LineString
    create_gate(client, fid, {"type": "Point", "coordinates": [0, 0]}, expect=422)

    # 越出围栏
    create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.005, 0.005], [0.02, 0.02]],
    }, expect=422)

    # 整条在孔洞内
    create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.0045, 0.0045], [0.0055, 0.0045]],
    }, expect=422)

    # 穿过孔洞
    create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.002, 0.005], [0.008, 0.005]],
    }, expect=422)

    # 贴在孔洞边界（接触孔洞也不允许）
    create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.003, 0.004], [0.005, 0.004]],
    }, expect=422)

    # 重复坐标点 / 只有一个点
    create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.002, 0.005], [0.002, 0.005]],
    }, expect=422)
    create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.002, 0.005]],
    }, expect=422)

    # 自相交折线
    create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.002, 0.002], [0.008, 0.008], [0.008, 0.002], [0.002, 0.008]],
    }, expect=422)

    # 名称为空、负位移 -> 422（pydantic）
    r = client.post(f"/fences/{fid}/gates", json={
        "geometry": GATE_EW, "forward_name": "", "reverse_name": "b",
    })
    assert r.status_code == 422
    r = client.post(f"/fences/{fid}/gates", json={
        "geometry": GATE_EW, "forward_name": "a", "reverse_name": "b",
        "min_displacement_m": -1,
    })
    assert r.status_code == 422


def test_gate_on_fence_boundary_accepted(client):
    fid = create_fence(client)
    # 门线落在围栏外边界上（有效区域边界允许）
    gate = create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.002, 0.0], [0.008, 0.0]],
    })
    assert gate["id"] > 0
    assert [g["id"] for g in client.get(f"/fences/{fid}/gates").json()] == [gate["id"]]


def test_crossing_independent_of_fence_inside_state(client):
    # 穿越核算是纯几何事件：门线横穿与 enter/leave 状态机互不影响
    fid = create_fence(client)
    create_gate(client, fid, GATE_EW)
    submit(client, [
        pt("dev1", 0, 0.002, 0.005),
        pt("dev1", 10, 0.008, 0.005),
    ])
    events = client.get("/events", params={"device_id": "dev1"}).json()
    assert [e["type"] for e in events] == ["enter"]
    crossings = get_crossings(client, device_id="dev1")
    assert len(crossings) == 1 and crossings[0]["direction"] == "forward"
