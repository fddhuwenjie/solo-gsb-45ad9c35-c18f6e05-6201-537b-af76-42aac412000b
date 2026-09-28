from sqlalchemy import Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Fence(Base):
    __tablename__ = "fences"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    geometry: Mapped[str] = mapped_column(Text)  # GeoJSON Polygon
    enter_buffer_m: Mapped[float] = mapped_column(Float, default=0.0)
    exit_buffer_m: Mapped[float] = mapped_column(Float, default=0.0)
    enter_dwell_s: Mapped[float] = mapped_column(Float, default=0.0)
    exit_dwell_s: Mapped[float] = mapped_column(Float, default=0.0)
    stay_min_s: Mapped[float] = mapped_column(Float, default=300.0)
    created_at: Mapped[float] = mapped_column(Float)


class TrackPointRow(Base):
    __tablename__ = "points"
    # (device_id, ts) 唯一：同一设备同一时刻的重复提交被幂等忽略
    __table_args__ = (UniqueConstraint("device_id", "ts", name="uq_device_ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(100), index=True)
    ts: Mapped[float] = mapped_column(Float)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(100), index=True)
    fence_id: Mapped[int] = mapped_column(Integer, index=True)
    type: Mapped[str] = mapped_column(String(10))  # enter | leave | stay
    ts: Mapped[float] = mapped_column(Float, index=True)


class DeviceState(Base):
    __tablename__ = "device_state"

    device_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    fence_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(10))  # inside | outside
    since: Mapped[float | None] = mapped_column(Float, nullable=True)


class CapacityRule(Base):
    __tablename__ = "capacity_rules"
    # (fence_id, effective_from) 唯一：同一围栏同一生效时刻只允许一条容量规则
    __table_args__ = (UniqueConstraint("fence_id", "effective_from", name="uq_fence_effective"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fence_id: Mapped[int] = mapped_column(Integer, index=True)
    effective_from: Mapped[float] = mapped_column(Float)  # epoch 秒，规则生效时刻（含）
    max_occupancy: Mapped[int] = mapped_column(Integer)   # 容量上限，正整数
    created_at: Mapped[float] = mapped_column(Float)


class GateLineRow(Base):
    __tablename__ = "gate_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    fence_id: Mapped[int] = mapped_column(Integer, index=True)
    geometry: Mapped[str] = mapped_column(Text)       # GeoJSON LineString（简单折线）
    forward_name: Mapped[str] = mapped_column(String(50))   # 右->左（正向）名称
    reverse_name: Mapped[str] = mapped_column(String(50))   # 左->右（反向）名称
    min_displacement_m: Mapped[float] = mapped_column(Float, default=0.0)  # 最小位移（米）
    created_at: Mapped[float] = mapped_column(Float)


class Crossing(Base):
    __tablename__ = "crossings"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(100), index=True)
    fence_id: Mapped[int] = mapped_column(Integer, index=True)
    gate_id: Mapped[int] = mapped_column(Integer, index=True)
    direction: Mapped[str] = mapped_column(String(10))     # forward | reverse
    ts: Mapped[float] = mapped_column(Float, index=True)   # 插值得到的穿越时刻（epoch 秒）
    lat: Mapped[float] = mapped_column(Float)              # 交点纬度
    lon: Mapped[float] = mapped_column(Float)              # 交点经度
