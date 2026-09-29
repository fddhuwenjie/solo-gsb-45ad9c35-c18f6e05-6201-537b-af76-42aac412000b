# 轨迹围栏事件 API（Geofence Trajectory Events）

基于 **FastAPI + SQLite + Shapely** 的地理围栏事件服务：

- 创建**带孔多边形**围栏（GeoJSON Polygon，支持 holes），每围栏可配置判定参数
- 批量提交轨迹点（设备 ID + 时间戳 + 经纬度），**乱序点按设备时间自动重排**
- 边界抖动去抖：可配置的**进入/离开缓冲距离**（迟滞带）+ **停留时长**（dwell）
- 产出 `enter` / `leave` / `stay` 事件；**重复提交轨迹点不会重复产出事件**（幂等）
- **围栏容量核算**：按生效时刻维护正整数容量上限规则，查询任意 `[start, end)` 窗口的占用分段与超限明细
- **定向门线穿越核算**：围栏内保存简单 GeoJSON LineString 门线（正/反向名称 + 最小位移），只承认相邻轨迹点对门线的**单点横穿**，穿越时刻按交点比例插值
- 按时间范围/设备/围栏/类型查询事件，查询设备当前状态

## 快速开始（Docker Compose 一键启动）

```bash
docker compose up --build
```

- 服务端口：**8000**（http://localhost:8000）
- 交互式 API 文档：http://localhost:8000/docs
- SQLite 数据持久化在名为 `geofence-data` 的卷中

停止并清理：`docker compose down`（加 `-v` 同时删除数据卷）

## 判定逻辑说明

1. **投影**：以围栏质心为原点做等距圆柱投影，把经纬度转为米制坐标后用 Shapely 计算点到多边形的距离 `d`（点在多边形内 `d=0`；**孔洞内 `d>0`，天然视为外部**）。
2. **缓冲迟滞**：
   - `d <= enter_buffer_m` → 判定为内部（可略提前触发进入）
   - `d > exit_buffer_m` → 判定为外部
   - 两者之间为迟滞灰区 → **维持上一状态**，抑制边界来回抖动
3. **停留去抖（dwell）**：状态迁移条件首次满足时开始计时，持续满足 `enter_dwell_s` / `exit_dwell_s` 才确认迁移并产出事件；期间条件被打破则作废。事件时间取**条件首次满足的轨迹时刻**。
4. **stay**：进入围栏后连续停留达到 `stay_min_s` 产出一次 `stay`（每次访问最多一次）。
5. **乱序与幂等**：轨迹点按 `(device_id, ts)` 唯一约束去重；每次摄入后按设备全量轨迹（时间升序）重算事件，因此乱序到达与按序到达结果一致，重复提交结果不变。

## 门线穿越判定逻辑

1. **门线几何**：门线是围栏内的简单（不自交、无重复点）GeoJSON `LineString`，**整条线必须位于围栏有效区域内**——不得越出外环（落在外边界上允许），也不得穿入孔洞或贴着孔洞边界。创建时按围栏投影做 `covers` + 孔洞边界相交校验，不满足返回 `422`。
2. **方向定义**：方向按门线坐标顺序判定。沿坐标顺序看，**右 → 左为正向（forward）**，左 → 右为反向（reverse），查询结果分别以门线的 `forward_name` / `reverse_name` 表示。
3. **横穿条件**：轨迹按设备时间升序重排，逐对相邻点形成的线段与门线检测，**只承认单点横穿（proper crossing）**——交点严格位于轨迹线段内部，且轨迹两端分居门线两侧。交点严格落在某段内部时按一次横穿；交点恰好落在门线**内部折点**（非首尾端点）时，在整条门线层面归并共享该折点的两侧邻接段：
   - 折点两侧邻接段**分居轨迹两侧**（方向判断一致）→ 归并为**一次**横穿（避免逐段统计时漏记或重复）；
   - 两侧邻接段在轨迹**同侧** → 凸角擦碰，忽略。
   以下情况一律忽略：
   - **沿线**：轨迹线段与门线段共线（含重合/部分重叠）；
   - **仅接触端点**：轨迹止于/始于门线，或交点恰好是门线首尾端点；轨迹端点恰落在内部折点同样忽略；
   - **同侧擦碰**：两端在门线同一侧，或穿内部折点时折点两侧邻接段同处轨迹一侧；
   - **多交点歧义**：同一轨迹段与门线产生多于一个有效交点（如同时横穿两段）；
   - **位移不足**：相邻点直线位移 `< min_displacement_m`。
4. **穿越时刻**：取交点在轨迹线段上的长度/参数比例 `t∈(0,1)`，在两端点时间戳间线性插值 `ts = t0 + t·(t1 - t0)`；交点经纬度随结果一并返回。
5. **多门线排序**：同一轨迹段命中多条门线时，结果按 **(插值时刻, 门线 ID)** 升序排列。
6. **乱序重算与幂等**：穿越记录与事件一样在每次摄入后按设备全量重算并整体替换；补录乱序点（包括更早的点）后结果自动更新，**不残留旧记录、不产生重复**。新建门线也会对存量轨迹补算。
7. **与围栏状态机解耦**：穿越是纯几何事件，不受 enter/leave 缓冲迟滞、dwell 与孔洞状态判定影响（例如门线贴在外环上时，围栏外的点横穿门线同样核算）。

## 示例请求

### 1. 创建带孔围栏

```bash
curl -X POST http://localhost:8000/fences \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "园区A（中央湖泊为孔洞）",
    "geometry": {
      "type": "Polygon",
      "coordinates": [
        [[116.30, 39.90], [116.32, 39.90], [116.32, 39.92], [116.30, 39.92], [116.30, 39.90]],
        [[116.308, 39.908], [116.312, 39.908], [116.312, 39.912], [116.308, 39.912], [116.308, 39.908]]
      ]
    },
    "config": {
      "enter_buffer_m": 0,
      "exit_buffer_m": 50,
      "enter_dwell_s": 10,
      "exit_dwell_s": 10,
      "stay_min_s": 600
    }
  }'
```

返回 `201` 及围栏对象（含 `id`，下文假设为 `1`）。

### 2. 提交轨迹点（乱序、含重复）

```bash
curl -X POST http://localhost:8000/points \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:20Z", "lat": 39.910, "lon": 116.305},
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:00Z", "lat": 39.900, "lon": 116.300},
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:10Z", "lat": 39.905, "lon": 116.302},
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:20Z", "lat": 39.910, "lon": 116.305}
    ]
  }'
```

返回：`{"accepted": 3, "duplicates": 1, "devices_updated": 1}`

### 3. 按时间范围查询事件

```bash
curl 'http://localhost:8000/events?device_id=dev-001&start=2026-09-22T00:00:00Z&end=2026-09-23T00:00:00Z'
# 可选过滤：fence_id=1&type=enter&limit=100&offset=0
```

### 4. 查询设备当前状态

```bash
curl http://localhost:8000/devices/dev-001/status
```

返回示例：

```json
{
  "device_id": "dev-001",
  "fences": [
    {"fence_id": 1, "fence_name": "园区A（中央湖泊为孔洞）", "state": "inside", "since": "2026-09-22T08:00:10Z"}
  ]
}
```

### 5. 新增容量规则

```bash
curl -X POST http://localhost:8000/fences/1/capacity-rules \
  -H 'Content-Type: application/json' \
  -d '{"effective_from": "2026-09-22T08:00:00Z", "max_occupancy": 2}'
```

- `max_occupancy` 必须是**正整数**（否则 `422`）
- 同一围栏同一 `effective_from` 重复提交返回 `409`
- 某时刻生效的上限 = 生效时刻 ≤ 该时刻的最近一条规则；此前无规则则该时段不限容（`limit` 为 `null`）
- 管理接口：`GET /fences/1/capacity-rules`（按生效时刻升序）、`DELETE /fences/1/capacity-rules/{rule_id}`

### 6. 查询占用分段（容量核算）

```bash
curl 'http://localhost:8000/fences/1/occupancy?start=2026-09-22T08:00:00Z&end=2026-09-22T09:00:00Z'
```

返回示例：

```json
{
  "fence_id": 1,
  "start": "2026-09-22T08:00:00Z",
  "end": "2026-09-22T09:00:00Z",
  "segments": [
    {"start": "2026-09-22T08:00:00Z", "end": "2026-09-22T08:10:00Z", "count": 1, "limit": 2, "exceeded": false, "excess": null, "devices": null},
    {"start": "2026-09-22T08:10:00Z", "end": "2026-09-22T08:20:00Z", "count": 3, "limit": 2, "exceeded": true, "excess": 1, "devices": ["dev-001", "dev-002", "dev-003"]},
    {"start": "2026-09-22T08:20:00Z", "end": "2026-09-22T09:00:00Z", "count": 2, "limit": 2, "exceeded": false, "excess": null, "devices": null}
  ]
}
```

**语义说明**：

1. **在场区间还原**：由该围栏的 `enter`/`leave` 事件重放出每台设备的半开区间 `[enter, leave)`；查询开始前已进入、查询结束仍未离开的区间被正确截断到 `[start, end)`。
2. **原子边界**：同一时刻的设备进/出与容量规则切换合并为一个边界，分段在该时刻一次性切换，不产生中间态或零长分段。
3. **恒定状态分段**：分段内在场设备集合与生效上限均不变；相邻同状态分段已合并，返回尽可能长的分段。
4. **超限明细**：`exceeded=true` 的分段给出 `limit`（上限）、`count`（实际人数）、`excess`（超额数）与按 ID 排序的 `devices`；其余分段 `excess`/`devices` 为 `null`。
5. **重算一致**：占用在查询时由事件现算；补录乱序轨迹点触发事件重算后，再次查询即反映最新结果，不残留旧的占用或超限片段。

### 7. 新增定向门线

```bash
curl -X POST http://localhost:8000/fences/1/gates \
  -H 'Content-Type: application/json' \
  -d '{
    "geometry": {
      "type": "LineString",
      "coordinates": [[116.305, 39.910], [116.315, 39.910]]
    },
    "forward_name": "南向北（入园）",
    "reverse_name": "北向南（出园）",
    "min_displacement_m": 5
  }'
```

- 门线坐标顺序为 **西 → 东** 时，门线右侧为南、左侧为北：南→北穿入即 `forward`（入园），北→南即 `reverse`（出园）。
- 整条线越出围栏有效区域、穿过/接触孔洞、非 `LineString`、单点、重复点或自相交，均返回 `422`；围栏不存在返回 `404`。
- 管理接口：`GET /fences/1/gates`、`DELETE /fences/1/gates/{gate_id}`（删除门线同时清除其穿越记录）。

### 8. 查询门线穿越

提交一段南北往返轨迹后：

```bash
curl -X POST http://localhost:8000/points -H 'Content-Type: application/json' \
  -d '{"points": [
    {"device_id": "dev-001", "ts": "2026-09-22T08:00:00Z", "lat": 39.909, "lon": 116.310},
    {"device_id": "dev-001", "ts": "2026-09-22T08:00:20Z", "lat": 39.911, "lon": 116.310},
    {"device_id": "dev-001", "ts": "2026-09-22T08:01:00Z", "lat": 39.909, "lon": 116.310}
  ]}'

curl 'http://localhost:8000/crossings?device_id=dev-001&start=2026-09-22T08:00:00Z&end=2026-09-22T09:00:00Z'
```

返回（穿越时刻分别插值到 `08:00:10` 与 `08:00:40`）：

```json
[
  {"id": 1, "device_id": "dev-001", "fence_id": 1, "gate_id": 1,
   "direction": "forward", "direction_name": "南向北（入园）",
   "ts": "2026-09-22T08:00:10Z", "lat": 39.91, "lon": 116.31},
  {"id": 2, "device_id": "dev-001", "fence_id": 1, "gate_id": 1,
   "direction": "reverse", "direction_name": "北向南（出园）",
   "ts": "2026-09-22T08:00:40Z", "lat": 39.91, "lon": 116.31}
]
```

- 查询参数：`device_id`、`gate_id`、`fence_id`、`direction=forward|reverse`、`start`/`end`（半开 `[start,end)`，必须同时给出，否则 `422`）、`limit`、`offset`。
- 结果固定按 `(ts, gate_id, id)` 升序返回，便于同一轨迹段命中多门线时还原顺序。

### 9. 边界情形示例（均不产生 crossing）

门线取 `LineString [(lon=116.305,lat=39.910), (116.315,39.910)]`（西→东），表中点按 `(纬度, 经度)` 书写：

| 情形 | 轨迹相邻点 | 结果 |
| --- | --- | --- |
| 沿线 | `(39.910,116.308)` → `(39.910,116.312)` | 共线，忽略 |
| 仅接触端点 | `(39.909,116.305)` → `(39.910,116.305)` | 交点是门线端点，忽略 |
| 跨端点穿过 | `(39.909,116.305)` → `(39.911,116.305)` | 交点仍在门线端点，忽略 |
| 同侧擦碰 | `(39.909,116.308)` → `(39.909,116.312)` | 两端同在南侧，忽略 |
| 端点落在线上 | `(39.910,116.310)` → `(39.909,116.311)` | 一端在线上未穿越，忽略 |
| 位移不足 | 南→北跨门线但位移 `< min_displacement_m` | 忽略 |
| 正向横穿 | `(39.909,116.310)` → `(39.911,116.310)` | **forward，时刻插值在中点** |
| 反向横穿 | `(39.911,116.310)` → `(39.909,116.310)` | **reverse，时刻插值在中点** |

含折点门线（在示例门线东端 `(39.910,116.315)` 续接一段南北向门线，折点即该端点）的补充情形（点仍按 `(纬度, 经度)` 书写）：

| 情形 | 轨迹相邻点 | 结果 |
| --- | --- | --- |
| 穿内部折点横穿 | `(39.909,116.316)` → `(39.911,116.314)` | 折点两侧邻接段分居轨迹两侧，**归并一次 crossing，交点取折点、时刻按比例插值** |
| 折点同侧擦碰 | `(39.909,116.314)` → `(39.911,116.316)` | 两侧邻接段同在轨迹一侧，忽略 |
| 轨迹端点在折点 | `(39.910,116.315)` → `(39.911,116.314)` | 交点恰在轨迹端点，忽略 |
| 沿门线段穿折点 | `(39.910,116.312)` → `(39.910,116.317)` | 与西→东门线段共线（沿线），忽略 |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/fences` | 创建围栏（GeoJSON Polygon，支持孔洞，附判定配置） |
| GET | `/fences` / `/fences/{id}` | 围栏列表 / 详情 |
| DELETE | `/fences/{id}` | 删除围栏及其事件、状态、容量规则 |
| POST | `/points` | 批量提交轨迹点（乱序重排、重复幂等） |
| GET | `/events` | 事件查询（device_id / fence_id / type / start / end / limit / offset） |
| GET | `/devices/{device_id}/status` | 设备当前相对各围栏的状态 |
| POST | `/fences/{id}/capacity-rules` | 新增容量规则（生效时刻 + 正整数上限，同时刻唯一） |
| GET | `/fences/{id}/capacity-rules` | 容量规则列表（按生效时刻升序） |
| DELETE | `/fences/{id}/capacity-rules/{rule_id}` | 删除容量规则 |
| GET | `/fences/{id}/occupancy` | 查询 `[start, end)` 占用分段与超限明细（必填 start/end） |
| POST | `/fences/{id}/gates` | 新增定向门线（围栏内简单 LineString + 正/反向名称 + 最小位移） |
| GET | `/fences/{id}/gates` | 门线列表（按 id 升序） |
| DELETE | `/fences/{id}/gates/{gate_id}` | 删除门线及其穿越记录 |
| GET | `/crossings` | 门线穿越查询（device_id / gate_id / fence_id / direction / start / end，半开窗口） |
| GET | `/health` | 健康检查 |

## 围栏配置参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `enter_buffer_m` | 0 | 进入缓冲距离（米），距多边形 ≤ 该值即视为内部 |
| `exit_buffer_m` | 0 | 离开缓冲距离（米），距多边形 > 该值才视为外部；须 ≥ `enter_buffer_m` |
| `enter_dwell_s` | 0 | 进入确认所需持续时长（秒） |
| `exit_dwell_s` | 0 | 离开确认所需持续时长（秒） |
| `stay_min_s` | 300 | 产生 `stay` 事件的最小连续停留时长（秒） |

## 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

pytest                      # 运行测试
uvicorn app.main:app --reload --port 8000   # 本地启动
```

数据库位置由环境变量 `DATABASE_URL` 控制，默认 `sqlite:///./geofence.db`。

## 项目结构

```
app/
  main.py      # FastAPI 路由与摄入/重算流程
  engine.py    # 投影、缓冲迟滞 + dwell 状态机（事件判定核心）、容量占用分段、门线穿越核算
  models.py    # SQLAlchemy 模型（fences / points / events / device_state / capacity_rules / gate_lines / crossings）
  schemas.py   # Pydantic 请求/响应模型
  database.py  # SQLite 连接与会话
tests/         # pytest 端到端测试（孔洞、乱序、幂等、抖动、停留、查询、容量核算、门线穿越）
Dockerfile / docker-compose.yml
```

## 说明与取舍

- 事件按设备全量轨迹重算并整体替换，保证乱序/重复提交下结果一致；事件 `id` 可能随重算变化，请以 `(device_id, fence_id, type, ts)` 识别事件。门线穿越同理整体重算，请以 `(device_id, gate_id, direction, ts)` 识别。
- 同一设备同一时间戳的轨迹点视为重复（保留首条）。
- 距离计算采用以围栏质心为原点的局部等距圆柱投影近似，围栏尺度（公里级）下误差可忽略。
- 门线方向以**坐标书写顺序**为准（右→左为正向）；同一扇门反向画一遍会交换 forward/reverse 的语义，命名时请注意坐标顺序。
