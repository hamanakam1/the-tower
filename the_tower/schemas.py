"""Core data contracts for The Tower.

Defines the hardware/airspace data contracts flowing through the ATC:
Node -> Telemetry -> Flight -> Clearance -> Kill.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, ConfigDict, field_validator


# ============================================================================
# ENUMS
# ============================================================================

class NodeStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    OVERLOADED = "overloaded"
    REDLINE = "redline"
    UNKNOWN = "unknown"


class ThermalState(str, Enum):
    NOMINAL = "nominal"
    FAIR = "fair"
    SERIOUS = "serious"
    CRITICAL = "critical"


class FlightStatus(str, Enum):
    QUEUED = "queued"
    CLEARED = "cleared"
    IN_FLIGHT = "in_flight"
    LANDED = "landed"
    SHOT_DOWN = "shot_down"
    CRASHED = "crashed"
    DENIED = "denied"


class ClearanceDecision(str, Enum):
    CLEARED = "cleared"
    DENIED = "denied"
    QUEUED = "queued"
    SHOT_DOWN = "shot_down"


class FlightPriority(int, Enum):
    P0 = 0
    P1 = 1
    P2 = 2
    P3 = 3


class EventKind(str, Enum):
    RADAR_SWEEP = "radar_sweep"
    CLEARANCE_REQUEST = "clearance_request"
    CLEARANCE_GRANTED = "clearance_granted"
    CLEARANCE_DENIED = "clearance_denied"
    TAKEOFF = "takeoff"
    LANDING = "landing"
    KILL = "kill"
    SHOT_DOWN = "shot_down"
    ANOMALY = "anomaly"
    NODE_ONLINE = "node_online"
    NODE_OFFLINE = "node_offline"
    REDLINE = "redline"


# ============================================================================
# RESOURCE / TELEMETRY
# ============================================================================

class ResourceWeight(BaseModel):
    """Resource demand of a compute flight."""
    model_config = ConfigDict(extra="forbid")

    cpu_cores: float = Field(default=1.0, ge=0.1, description="CPU cores required")
    memory_gb: float = Field(default=1.0, ge=0.1, description="RAM required (GB)")
    gpu_units: float = Field(default=0.0, ge=0.0, description="GPU units required")
    duration_est_s: float = Field(default=60.0, ge=1.0, description="Estimated runtime seconds")
    network_mbps: float = Field(default=1.0, ge=0.0, description="Sustained bandwidth (Mbps)")
    thermal_budget_c: float = Field(default=5.0, ge=0.0, description="Allowed temp rise (C)")


class Telemetry(BaseModel):
    """A single telemetry sample for a node."""
    model_config = ConfigDict(extra="forbid")

    node_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    cpu_percent: float = Field(ge=0.0, le=100.0)
    memory_percent: float = Field(ge=0.0, le=100.0)
    memory_used_gb: float = Field(ge=0.0)
    memory_total_gb: float = Field(gt=0.0)
    temperature_c: float
    power_watts: float = Field(ge=0.0)
    bandwidth_mbps: float = Field(ge=0.0)
    load_avg: float = Field(ge=0.0)
    gpu_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    thermal_state: ThermalState = ThermalState.NOMINAL
    battery_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    solar_input_watts: Optional[float] = Field(default=None, ge=0.0)
    source: str = Field(default="simulated",
                        description="Where the sample came from: 'measured', 'simulated', or 'ingested'")
    source_note: Optional[str] = Field(default=None,
                                       description="Caveat about the sample, e.g. 'temp estimated from cpu load'")

    @field_validator("thermal_state", mode="before")
    @classmethod
    def _coerce_thermal(cls, v):
        if isinstance(v, str):
            return ThermalState(v)
        return v


class NodeSpec(BaseModel):
    """Static specification of a mesh node."""
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    role: str
    specs: str
    constraints: str
    cpu_cores: float = Field(gt=0.0)
    memory_gb: float = Field(gt=0.0)
    gpu_units: float = Field(default=0.0, ge=0.0)
    redline_temp_c: float = Field(gt=0.0)
    max_power_watts: float = Field(gt=0.0)
    network_capacity_mbps: float = Field(gt=0.0)
    thermal_mass: float = Field(default=1.0, gt=0.0, description="Thermal inertia factor")
    location: str = "local"
    simulate: bool = Field(default=True, description="Whether radar simulates telemetry for this node")
    staleness_budget_s: int = Field(default=30, gt=0, description="Seconds before telemetry is considered stale")


class Node(BaseModel):
    """Live state of a mesh node."""
    model_config = ConfigDict(extra="forbid")

    spec: NodeSpec
    status: NodeStatus = NodeStatus.ONLINE
    telemetry: Telemetry
    active_flights: List[str] = Field(default_factory=list)
    active_weights: Dict[str, "ResourceWeight"] = Field(default_factory=dict)
    queued_flights: List[str] = Field(default_factory=list)
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    history: List[Telemetry] = Field(default_factory=list, description="Recent telemetry samples")

    @property
    def id(self) -> str:
        return self.spec.id

    def reserved(self) -> Dict[str, float]:
        cpu = sum(w.cpu_cores for w in self.active_weights.values())
        mem = sum(w.memory_gb for w in self.active_weights.values())
        gpu = sum(w.gpu_units for w in self.active_weights.values())
        return {"cpu": cpu, "memory_gb": mem, "gpu": gpu}

    def capacity_free(self) -> Dict[str, float]:
        t = self.telemetry
        mem_free = self.spec.memory_gb * (1 - t.memory_percent / 100.0)
        cpu_free = self.spec.cpu_cores * (1 - t.cpu_percent / 100.0)
        gpu_free = self.spec.gpu_units * (1 - (t.gpu_percent or 0.0) / 100.0)
        r = self.reserved()
        return {
            "cpu": max(0.0, cpu_free - r["cpu"]),
            "memory_gb": max(0.0, mem_free - r["memory_gb"]),
            "gpu": max(0.0, gpu_free - r["gpu"]),
        }

    def can_accept(self, weight: ResourceWeight) -> bool:
        if self.status in (NodeStatus.OFFLINE, NodeStatus.REDLINE, NodeStatus.UNKNOWN):
            return False
        if self.telemetry.thermal_state in (ThermalState.CRITICAL,):
            return False
        if self.telemetry.temperature_c >= self.spec.redline_temp_c:
            return False
        free = self.capacity_free()
        if weight.cpu_cores > free["cpu"] + 1e-6:
            return False
        if weight.memory_gb > free["memory_gb"] + 1e-6:
            return False
        if weight.gpu_units > free["gpu"] + 1e-6:
            return False
        # power headroom
        projected_power = self.telemetry.power_watts + weight.cpu_cores * 8.0
        if projected_power > self.spec.max_power_watts * 1.05:
            return False
        return True


# ============================================================================
# FLIGHTS / CLEARANCE
# ============================================================================

class Flight(BaseModel):
    """A compute task seeking clearance."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"flt-{uuid4().hex[:10]}")
    callsign: str = Field(default="", description="Human-readable callsign")
    task: str = Field(..., min_length=1, description="Task description / true goal")
    requester: str = Field(default="el-hidratador", description="Who requested clearance")
    priority: FlightPriority = FlightPriority.P2
    weight: ResourceWeight = Field(default_factory=ResourceWeight)
    container_id: Optional[str] = None
    preferred_node: Optional[str] = None
    scope: List[str] = Field(default_factory=list, description="Resource keys for conflict detection")
    status: FlightStatus = FlightStatus.QUEUED
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    cleared_at: Optional[datetime] = None
    takeoff_at: Optional[datetime] = None
    landed_at: Optional[datetime] = None
    shot_down_at: Optional[datetime] = None
    assigned_node: Optional[str] = None
    reason: str = ""
    rogue: bool = False
    retries: int = 0
    max_retries: int = 2
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context):
        if not self.callsign:
            self.callsign = f"FLT-{self.id[-6:].upper()}"


class ClearanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flight: Flight
    dag: Optional[Dict[str, Any]] = None
    force: bool = False


class ClearanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ClearanceDecision
    flight_id: str
    assigned_node: Optional[str] = None
    reason: str
    estimated_wait_s: Optional[float] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class KillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_id: str
    reason: str = "manual kill"
    requester: str = "operator"
    force: bool = False


class KillResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    container_id: str
    flight_id: Optional[str] = None
    node_id: Optional[str] = None
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ============================================================================
# EVENTS / AIRSPACE
# ============================================================================

class TowerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"evt-{uuid4().hex[:10]}")
    kind: EventKind
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    node_id: Optional[str] = None
    flight_id: Optional[str] = None
    container_id: Optional[str] = None
    message: str
    severity: str = "info"  # info, warn, critical
    data: Dict[str, Any] = Field(default_factory=dict)


class AirspaceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    nodes: List[Node]
    flights: List[Flight]
    events: List[TowerEvent]
    stats: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# DEFAULT NODES
# ============================================================================

def default_nodes() -> List[NodeSpec]:
    return [
        NodeSpec(
            id="m3max",
            name="M3 Max",
            role="Heavy Bomber",
            specs="128GB Unified Memory, Apple Silicon",
            constraints="Thermal throttling under sustained load",
            cpu_cores=16.0,
            memory_gb=128.0,
            gpu_units=40.0,
            redline_temp_c=95.0,
            max_power_watts=100.0,
            network_capacity_mbps=10000.0,
            thermal_mass=4.0,
            location="local",
        ),
        NodeSpec(
            id="pi-cluster",
            name="Raspberry Pi 5 Cluster",
            role="Drone Swarm",
            specs="Multiple nodes, 8GB each",
            constraints="80C redline, limited RAM/power",
            cpu_cores=16.0,
            memory_gb=32.0,
            gpu_units=0.0,
            redline_temp_c=80.0,
            max_power_watts=60.0,
            network_capacity_mbps=1000.0,
            thermal_mass=1.2,
            location="edge",
        ),
        NodeSpec(
            id="trailer-hpc",
            name="Mobile HPC Trailer",
            role="Off-grid Compute",
            specs="Solar + battery banks",
            constraints="Constrained by SoC/battery discharge",
            cpu_cores=8.0,
            memory_gb=64.0,
            gpu_units=8.0,
            redline_temp_c=85.0,
            max_power_watts=300.0,
            network_capacity_mbps=500.0,
            thermal_mass=3.0,
            location="off-grid",
        ),
    ]
