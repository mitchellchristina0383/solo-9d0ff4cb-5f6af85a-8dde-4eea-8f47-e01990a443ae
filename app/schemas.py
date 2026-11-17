"""Pydantic request/response models."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["fault", "recover"]


class ServiceIn(BaseModel):
    name: str = Field(..., min_length=1, description="Unique service name")
    dependencies: list[str] = Field(
        default_factory=list,
        description="Names of upstream services this service depends on",
    )


class GraphPayload(BaseModel):
    version: Optional[str] = Field(
        default=None, description="Optional caller-supplied graph version label"
    )
    services: list[ServiceIn] = Field(
        ..., min_length=1, description="Full dependency graph (whole-version replace)"
    )


class ReportPayload(BaseModel):
    service: str = Field(..., min_length=1)
    seq: int = Field(..., gt=0, description="Strictly positive, increasing per service")
    status: Status


class RootCause(BaseModel):
    service: str
    paths: list[list[str]]


class ActionableFault(BaseModel):
    service: str
    last_seq: int


class SuppressedFault(BaseModel):
    service: str
    last_seq: int
    root_causes: list[str]
    paths: list[list[str]]


class PaginationInfo(BaseModel):
    limit: int = Field(..., description="Maximum paths returned on this page")
    offset: int = Field(..., description="Global flat path offset of this page")
    next_offset: int = Field(..., description="Offset the next_cursor continues from")
    returned_paths: int = Field(..., description="Number of paths included on this page")
    total_paths: int = Field(
        ...,
        description=(
            "Total number of all-faulty suppression paths across all pages. "
            "An arbitrarily large Python integer on highly branched graphs."
        ),
    )
    path_counts: dict[str, int] = Field(
        ..., description="Total all-faulty path count per suppressed service"
    )
    complete: bool = Field(..., description="True when this is the final page")
    truncated: bool = Field(..., description="True when further pages exist")
    next_cursor: Optional[str] = Field(
        ..., description="Opaque token for the next page; null when complete"
    )
    hint: Optional[str] = None


class AlertsResponse(BaseModel):
    graph_revision: int
    graph_version: Optional[str]
    services: list[str]
    actionable_faults: list[ActionableFault]
    suppressed_faults: list[SuppressedFault]
    down_services: list[str]
    unreported_services: list[str]
    healthy_services: list[str]
    # Absent on legacy (parameter-less) responses whose paths fit one page.
    pagination: Optional[PaginationInfo] = None


class GraphResponse(BaseModel):
    revision: int
    version: Optional[str]
    committed_at: Optional[str]
    services: list[ServiceIn]


class ReportResponse(BaseModel):
    applied: bool
    duplicate: bool
    service: str
    seq: int
    status: Status
    current_status: Literal["up", "down"]
    last_seq: int


class ReplaceResponse(BaseModel):
    accepted: bool
    revision: int
    version: Optional[str]
    service_count: int
    edge_count: int
    preserved_state: list[str]
