"""围栏事件判定引擎。

将经纬度投影到以围栏质心为原点的局部平面坐标系（等距圆柱近似），
在米制坐标下用 Shapely 做距离计算，配合进入/离开缓冲距离形成迟滞区间，
再叠加停留时长（dwell）做抖动去抖，产出 enter / leave / stay 事件。

另含容量核算：由 enter/leave 事件还原设备半开在场区间，结合容量规则
把查询窗口切分为状态恒定的占用分段并标注超限明细。

门线穿越核算：在围栏有效区域内定义定向 GeoJSON LineString 门线，按设备
时间重排轨迹，只承认线段对门线的单点横穿（proper crossing），穿越时刻按
交点在线段上的时间比例插值。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import Point, Polygon

EARTH_RADIUS_M = 6_371_000.0

EVENT_ENTER = "enter"
EVENT_LEAVE = "leave"
EVENT_STAY = "stay"

DIRECTION_FORWARD = "forward"    # 由门线右侧穿到左侧（沿门线坐标顺序看）
DIRECTION_REVERSE = "reverse"    # 由门线左侧穿到右侧

# 交点参数严格位于 (0, 1) 内的容差（米制投影坐标下）
_CROSS_EPS = 1e-9


def _project(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """经纬度 -> 以 (lat0, lon0) 为原点的局部米制坐标（等距圆柱近似）。"""
    x = math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return (x, y)


@dataclass
class FenceConfig:
    enter_buffer_m: float = 0.0   # 距多边形 <= 该距离即视为“进入”（提前触发）
    exit_buffer_m: float = 0.0    # 距多边形 > 该距离才视为“离开”（迟滞带）
    enter_dwell_s: float = 0.0    # 持续在内部这么久才确认 enter
    exit_dwell_s: float = 0.0     # 持续在外部这么久才确认 leave
    stay_min_s: float = 300.0     # 单次访问内连续停留达到该时长产生一次 stay


@dataclass
class TrackPoint:
    ts: float   # epoch 秒
    lat: float
    lon: float


@dataclass
class FenceEvent:
    fence_id: int
    type: str   # enter | leave | stay
    ts: float   # 事件发生的轨迹时间（非确认时间）


def _unproject(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    """局部米制坐标 -> 经纬度，_project 的逆变换。"""
    lat = lat0 + math.degrees(y / EARTH_RADIUS_M)
    lon = lon0 + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))
    return (lat, lon)


def project_points(
    coords: list[tuple[float, float]], lat0: float, lon0: float
) -> list[tuple[float, float]]:
    """把 (lon, lat) 坐标序列投影到以 (lat0, lon0) 为原点的局部米制坐标。"""
    return [_project(lat, lon, lat0, lon0) for lon, lat in coords]


class ProjectedFence:
    """投影到局部米制坐标系的围栏多边形，支持带孔多边形。"""

    def __init__(self, fence_id: int, polygon: Polygon, config: FenceConfig):
        self.fence_id = fence_id
        self.config = config
        centroid = polygon.centroid
        self._lat0, self._lon0 = centroid.y, centroid.x
        shell = [_project(lat, lon, self._lat0, self._lon0)
                 for lon, lat in polygon.exterior.coords]
        holes = [
            [_project(lat, lon, self._lat0, self._lon0) for lon, lat in ring.coords]
            for ring in polygon.interiors
        ]
        self._poly = Polygon(shell, holes)

    @property
    def origin(self) -> tuple[float, float]:
        """投影原点 (lat0, lon0)。"""
        return (self._lat0, self._lon0)

    def classify(self, lat: float, lon: float, prev_inside: bool) -> bool:
        """判定点是否在围栏内。

        点到多边形的距离 d（内部为 0，孔洞内 > 0，因此孔洞天然视为外部）：
        - d <= enter_buffer_m  -> 内部
        - d >  exit_buffer_m   -> 外部
        - 两者之间为迟滞灰区   -> 维持上一状态，抑制边界抖动
        """
        x, y = _project(lat, lon, self._lat0, self._lon0)
        d = self._poly.distance(Point(x, y))
        if d <= self.config.enter_buffer_m:
            return True
        if d > self.config.exit_buffer_m:
            return False
        return prev_inside


def compute_events(
    fence: ProjectedFence, points: list[TrackPoint]
) -> tuple[list[FenceEvent], bool, float | None]:
    """对单个 (设备, 围栏) 按时间有序轨迹计算事件。

    points 必须已按 ts 升序排列。返回 (事件列表, 最终是否在围栏内, 当前状态起始时间)。
    状态机：
    - 分类结果与当前状态一致 -> 取消未确认的待定迁移；
    - 分类结果不同 -> 记录待定迁移起点，持续满足 dwell 时长后才确认迁移并产出事件，
      事件时间取条件首次满足的时刻；期间条件被打破则待定迁移作废（去抖）。
    - 进入围栏后连续停留达到 stay_min_s，产出一次 stay（每次访问最多一次）。
    """
    cfg = fence.config
    events: list[FenceEvent] = []
    inside = False
    state_since: float | None = None
    pending_since: float | None = None
    stay_emitted = False

    for pt in points:
        now_inside = fence.classify(pt.lat, pt.lon, inside)
        if now_inside == inside:
            pending_since = None
        else:
            if pending_since is None:
                pending_since = pt.ts
            dwell = cfg.enter_dwell_s if now_inside else cfg.exit_dwell_s
            if pt.ts - pending_since >= dwell:
                inside = now_inside
                state_since = pending_since
                events.append(FenceEvent(
                    fence.fence_id,
                    EVENT_ENTER if inside else EVENT_LEAVE,
                    pending_since,
                ))
                pending_since = None
                stay_emitted = False
        if inside and not stay_emitted and pt.ts - state_since >= cfg.stay_min_s:
            events.append(FenceEvent(fence.fence_id, EVENT_STAY, state_since + cfg.stay_min_s))
            stay_emitted = True

    return events, inside, state_since


# ---------- 容量核算 ----------


@dataclass
class OccupancySegment:
    """[start, end) 内状态恒定（在场设备集合与生效上限均不变）的占用分段。"""

    start: float
    end: float
    devices: tuple[str, ...]   # 在场设备 ID，已排序
    limit: int | None          # 该分段生效的容量上限；无规则时为 None

    @property
    def count(self) -> int:
        return len(self.devices)

    @property
    def exceeded(self) -> bool:
        return self.limit is not None and self.count > self.limit

    @property
    def excess(self) -> int | None:
        return self.count - self.limit if self.exceeded else None


def compute_occupancy_segments(
    events: list[tuple[str, str, float]],
    rules: list[tuple[float, int]],
    start: float,
    end: float,
) -> list[OccupancySegment]:
    """由 enter/leave 事件与容量规则还原 [start, end) 内的占用分段。

    - 重放事件流，把每台设备的在场时间还原为半开区间 [enter, leave)；
      查询前已进入、查询结束仍未离开的区间被截断到查询窗口。
    - 边界取窗口端点、区间端点与窗口内的规则生效时刻；同一时刻的
      设备进出与规则切换在边界集合中天然合并，按一个原子边界处理。
    - 相邻状态（在场设备集合 + 生效上限）相同的分段被合并，保证分段尽可能长。
    """
    # 1) 还原每台设备的半开在场区间 [enter, leave)
    by_device: dict[str, list[tuple[float, str]]] = {}
    for device_id, etype, ts in events:
        if etype in (EVENT_ENTER, EVENT_LEAVE):
            by_device.setdefault(device_id, []).append((ts, etype))

    intervals: list[tuple[float, float, str]] = []  # (起, 止, 设备)
    for device_id, evs in by_device.items():
        evs.sort()
        open_since: float | None = None
        for ts, etype in evs:
            if etype == EVENT_ENTER:
                if open_since is None:
                    open_since = ts
            elif open_since is not None:
                intervals.append((open_since, ts, device_id))
                open_since = None
        if open_since is not None:  # 仍未离开：延伸到 +inf，交由查询窗口截断
            intervals.append((open_since, math.inf, device_id))

    # 2) 截断到查询窗口 [start, end)，丢弃窗口外的零长区间
    clipped = [
        (max(s, start), min(e, end), dev)
        for s, e, dev in intervals
        if max(s, start) < min(e, end)
    ]

    # 3) 边界集合：同一时刻的进出与规则切换合并为一个原子边界
    boundaries = {start, end}
    for cs, ce, _ in clipped:
        boundaries.add(cs)
        boundaries.add(ce)
    ordered_rules = sorted(rules)
    for effective_from, _ in ordered_rules:
        if start < effective_from < end:
            boundaries.add(effective_from)
    ordered = sorted(boundaries)

    # 4) 逐段求恒定状态，相邻同状态分段合并
    segments: list[OccupancySegment] = []
    for a, b in zip(ordered, ordered[1:]):
        devices = tuple(sorted(dev for cs, ce, dev in clipped if cs <= a < ce))
        limit: int | None = None
        for effective_from, cap in ordered_rules:
            if effective_from > a:
                break
            limit = cap
        if segments and segments[-1].devices == devices and segments[-1].limit == limit:
            segments[-1].end = b
        else:
            segments.append(OccupancySegment(a, b, devices, limit))
    return segments


# ---------- 门线穿越核算 ----------


@dataclass
class GateLine:
    """定向门线（已投影到所属围栏的局部米制坐标系）。

    coords 为 LineString 折点序列；方向按坐标顺序判定：
    沿坐标顺序看，由右侧穿到左侧为正向（forward），反之为反向（reverse）。
    """

    gate_id: int
    coords: list[tuple[float, float]]
    forward_name: str
    reverse_name: str
    min_displacement_m: float

    def crossing(
        self, a: tuple[float, float], b: tuple[float, float]
    ) -> tuple[float, str, tuple[float, float]] | None:
        """求轨迹线段 a->b 对门线的单点横穿。

        返回 (交点在线段 a->b 上的比例 t, 方向, 交点米制坐标)；
        不构成横穿时返回 None。以下情况一律忽略：
        - 沿线（与某门线段共线/重合/重叠）；
        - 仅接触门线端点或门线折点（交点不在两条线段的严格内部）；
        - 两端在门线同一侧（同侧擦碰）；
        - 同一轨迹线段与门线产生多于一个交点（门线仅 simple，理论上不出现，防御处理）。
        """
        hits: list[tuple[float, str, tuple[float, float]]] = []
        for p, q in zip(self.coords, self.coords[1:]):
            hit = _segment_proper_crossing(p, q, a, b)
            if hit is not None:
                hits.append(hit)
        if len(hits) != 1:
            return None
        return hits[0]


def _segment_proper_crossing(
    p: tuple[float, float],
    q: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> tuple[float, str, tuple[float, float]] | None:
    """两线段的严格内部单点横穿（proper crossing）。

    门线段 p->q，轨迹线段 a->b。交点必须严格位于两条线段内部
    （即参数 u、t 都在 (0, 1)），因此共线、接触端点/折点都被排除。
    """
    rx, ry = q[0] - p[0], q[1] - p[1]
    sx, sy = b[0] - a[0], b[1] - a[1]
    denom = rx * sy - ry * sx
    if denom == 0.0:
        return None  # 平行或共线（沿线）
    qpx, qpy = a[0] - p[0], a[1] - p[1]
    u = (qpx * sy - qpy * sx) / denom
    t = (qpx * ry - qpy * rx) / denom
    if not (_CROSS_EPS < u < 1.0 - _CROSS_EPS and _CROSS_EPS < t < 1.0 - _CROSS_EPS):
        return None  # 交于端点/延长线（仅接触端点）或不相交

    # 两端相对定向门线 p->q 的侧别（标准右手系）：
    # cross(d, r) > 0 在门线右侧，< 0 在门线左侧。
    side_a = qpx * ry - qpy * rx          # cross(A - P, r)
    side_b = (qpx + sx) * ry - (qpy + sy) * rx
    if side_a * side_b >= 0.0:
        return None  # 两端同侧（同侧擦碰）或端点恰在门线上
    point = (a[0] + t * sx, a[1] + t * sy)

    # 沿门线坐标顺序看：右 -> 左为正向，左 -> 右为反向
    direction = DIRECTION_FORWARD if side_a > 0 else DIRECTION_REVERSE
    return (t, direction, point)


@dataclass
class GateCrossing:
    gate_id: int
    direction: str   # forward | reverse
    ts: float        # 按交点在轨迹线段上的比例插值得到的穿越时刻
    lat: float
    lon: float


def compute_crossings(
    gates: list[GateLine],
    points: list[TrackPoint],
    lat0: float,
    lon0: float,
) -> list[GateCrossing]:
    """对单个设备在某围栏内的门线集合计算穿越记录。

    points 必须已按 ts 升序排列。对每对相邻点形成的轨迹线段逐一检测：
    - 与门线单点横穿、两端分居两侧才承认（GateLine.crossing 内判定）；
    - 相邻点位移不足门线 min_displacement_m 时忽略；
    - 穿越时刻按交点在线段上的比例在两端点时间戳间线性插值。
    同一轨迹段命中多条门线时，结果按 (插值时刻, gate_id) 排序。
    """
    crossings: list[GateCrossing] = []
    for prev, cur in zip(points, points[1:]):
        ax, ay = _project(prev.lat, prev.lon, lat0, lon0)
        bx, by = _project(cur.lat, cur.lon, lat0, lon0)
        dx, dy = bx - ax, by - ay
        segment_len = math.hypot(dx, dy)
        dt = cur.ts - prev.ts
        a, b = (ax, ay), (bx, by)
        for gate in gates:
            if segment_len < gate.min_displacement_m:
                continue  # 位移不足
            hit = gate.crossing(a, b)
            if hit is None:
                continue
            t, direction, (ix, iy) = hit
            ilat, ilon = _unproject(ix, iy, lat0, lon0)
            crossings.append(GateCrossing(
                gate_id=gate.gate_id,
                direction=direction,
                ts=prev.ts + t * dt,
                lat=ilat,
                lon=ilon,
            ))
    crossings.sort(key=lambda c: (c.ts, c.gate_id))
    return crossings
