from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class FenceConfigSchema(BaseModel):
    """围栏判定参数。enter_buffer_m 必须 <= exit_buffer_m 才能构成有效迟滞带。"""

    enter_buffer_m: float = Field(0.0, ge=0, description="进入缓冲距离（米）")
    exit_buffer_m: float = Field(0.0, ge=0, description="离开缓冲距离（米）")
    enter_dwell_s: float = Field(0.0, ge=0, description="进入确认停留时长（秒）")
    exit_dwell_s: float = Field(0.0, ge=0, description="离开确认停留时长（秒）")
    stay_min_s: float = Field(300.0, ge=0, description="产生 stay 事件的最小连续停留时长（秒）")

    @model_validator(mode="after")
    def check_buffers(self):
        if self.enter_buffer_m > self.exit_buffer_m:
            raise ValueError("enter_buffer_m 必须小于等于 exit_buffer_m")
        return self


class FenceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    geometry: dict = Field(description="GeoJSON Polygon，coordinates[0] 为外环，其余为孔洞")
    config: FenceConfigSchema = FenceConfigSchema()


class FenceOut(BaseModel):
    id: int
    name: str
    geometry: dict
    config: FenceConfigSchema
    created_at: datetime


class PointIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=100)
    ts: datetime = Field(description="ISO 8601 时间戳，缺省时区按 UTC 处理")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class PointBatch(BaseModel):
    points: list[PointIn] = Field(min_length=1, max_length=10000)


class IngestResult(BaseModel):
    accepted: int
    duplicates: int
    devices_updated: int


class EventOut(BaseModel):
    id: int
    device_id: str
    fence_id: int
    type: str
    ts: datetime


class FenceStateOut(BaseModel):
    fence_id: int
    fence_name: str
    state: Literal["inside", "outside"]
    since: datetime | None


class DeviceStatusOut(BaseModel):
    device_id: str
    fences: list[FenceStateOut]


class CapacityRuleCreate(BaseModel):
    effective_from: datetime = Field(description="规则生效时刻（ISO 8601，含该时刻）")
    max_occupancy: int = Field(gt=0, description="容量上限（正整数）")


class CapacityRuleOut(BaseModel):
    id: int
    fence_id: int
    effective_from: datetime
    max_occupancy: int
    created_at: datetime


class OccupancySegmentOut(BaseModel):
    start: datetime
    end: datetime
    count: int = Field(description="分段内在场设备数")
    limit: int | None = Field(description="分段内生效的容量上限；无规则时为 null")
    exceeded: bool = Field(description="是否超限（count > limit）")
    excess: int | None = Field(description="超额人数，仅超限分段有值")
    devices: list[str] | None = Field(description="在场设备（按 ID 排序），仅超限分段有值")


class OccupancyOut(BaseModel):
    fence_id: int
    start: datetime
    end: datetime
    segments: list[OccupancySegmentOut]


class GateLineCreate(BaseModel):
    geometry: dict = Field(
        description="GeoJSON LineString，至少两个不同坐标；整条线必须位于围栏有效区域内"
    )
    forward_name: str = Field(min_length=1, max_length=50, description="正向（右->左）名称")
    reverse_name: str = Field(min_length=1, max_length=50, description="反向（左->右）名称")
    min_displacement_m: float = Field(
        0.0, ge=0, description="相邻轨迹点最小位移（米），不足则忽略该次穿越"
    )


class GateLineOut(BaseModel):
    id: int
    fence_id: int
    geometry: dict
    forward_name: str
    reverse_name: str
    min_displacement_m: float
    created_at: datetime


class CrossingOut(BaseModel):
    id: int
    device_id: str
    fence_id: int
    gate_id: int
    direction: Literal["forward", "reverse"]
    direction_name: str = Field(description="方向名称（取门线的正向/反向名称）")
    ts: datetime = Field(description="按交点比例插值得到的穿越时刻")
    lat: float = Field(description="交点纬度")
    lon: float = Field(description="交点经度")
