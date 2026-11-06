from __future__ import annotations

import time
import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["error", "warning", "info"]
Case = Literal["minimum", "normal", "maximum", "design", "single", "other"]
Kind = Literal["condition", "design", "calculated"]


def new_id() -> str:
    return uuid.uuid4().hex[:8]


class Flag(BaseModel):
    severity: Severity
    code: str
    message: str


class Cell(BaseModel):
    id: str = Field(default_factory=new_id)
    section: Optional[str] = None
    row_no: Optional[int] = None
    parameter: str
    canonical: str = "other"
    kind: Kind = "condition"
    case: Case
    value: Optional[str] = None
    unit: Optional[str] = None
    page: int = 1
    bbox: Optional[list[float]] = None      # [x0, y0, x1, y1] in original page pixels (Phase 2+)
    flags: list[Flag] = []
    source: str = "model"
    original_value: Optional[str] = None    # value before the first human edit
    original_unit: Optional[str] = None
    edited: bool = False
    reviewed: bool = False                  # a human accepted or corrected this cell


class Sheet(BaseModel):
    id: str = Field(default_factory=new_id)
    file_idx: int
    file_name: str
    title: Optional[str] = None
    tag: Optional[str] = None
    fluid: Optional[str] = None
    service: Optional[str] = None
    pages: list[int] = []
    has_values: bool = True
    info: dict[str, str] = {}
    cells: list[Cell] = []
    notes: list[Flag] = []                  # sheet-level flags


class FileInfo(BaseModel):
    idx: int
    name: str
    pages: int = 0


class Job(BaseModel):
    id: str
    status: Literal["queued", "processing", "done", "error"] = "queued"
    progress: str = "Queued"
    files: list[FileInfo] = []
    sheets: list[Sheet] = []
    error: Optional[str] = None
    created: float = Field(default_factory=time.time)


class CellPatch(BaseModel):
    value: Optional[str] = None
    unit: Optional[str] = None
    parameter: Optional[str] = None
    reviewed: Optional[bool] = None


class NewCell(BaseModel):
    parameter: str
    case: Case
    value: Optional[str] = None
    unit: Optional[str] = None
    section: Optional[str] = None
    kind: Kind = "condition"
