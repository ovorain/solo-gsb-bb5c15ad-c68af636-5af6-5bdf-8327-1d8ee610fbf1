"""FastAPI firmware image assembly service."""

from __future__ import annotations

import base64

from fastapi import Body, FastAPI
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .hex_parser import ParseResult, parse_intel_hex
from .layout import Region, validate_layout as check_layout

app = FastAPI(
    title="Firmware Image Assembly Service",
    version="1.1.0",
    description=(
        "Accepts Intel HEX text, validates every record (byte count and "
        "two's-complement checksum; record types 00/01/02/03/04/05), assembles "
        "the firmware image and returns contiguous Base64 segments, "
        "inter-segment gaps, the entry address and per-line diagnostics. "
        "POST /api/v1/validate-layout additionally checks the assembled "
        "image against a memory layout of named regions."
    ),
)


class AssembleRequest(BaseModel):
    hex_text: str = Field(..., description="Intel HEX text, one record per line")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "hex_text": (
                        ":020000040800F2\n"
                        ":10000000214601360121470136007EFE09D2190141\n"
                        ":04000005080001C12D\n"
                        ":00000001FF\n"
                    )
                }
            ]
        }
    }


class DiagnosticOut(BaseModel):
    line: int = Field(..., description="1-based line number; 0 marks a file-level issue")
    status: str = Field(..., description="ok | warning | error")
    record_type: str | None = None
    address: int | None = Field(None, description="Absolute address the record refers to, if any")
    byte_count: int | None = None
    message: str


class SegmentOut(BaseModel):
    start: int = Field(..., description="Absolute start address")
    end: int = Field(..., description="Exclusive end address")
    size: int
    data_base64: str = Field(..., description="Base64-encoded contiguous bytes")


class GapOut(BaseModel):
    start: int = Field(..., description="First unwritten address after the previous segment")
    end: int = Field(..., description="Start address of the next segment (exclusive)")
    size: int


class AssembleResponse(BaseModel):
    ok: bool = Field(..., description="True when no error-level diagnostic was raised")
    error_count: int
    warning_count: int
    entry_address: int | None = Field(None, description="From the last 03/05 record, if present")
    total_bytes: int
    segment_count: int
    segments: list[SegmentOut]
    gaps: list[GapOut]
    diagnostics: list[DiagnosticOut]


class RegionIn(BaseModel):
    name: str = Field(..., min_length=1, description="Region label, echoed in summaries and violations")
    start: int = Field(..., ge=0, description="Inclusive start address")
    end: int = Field(..., ge=0, description="Exclusive end address")
    allow_data: bool = Field(True, description="Whether firmware bytes may occupy this region")
    allow_entry: bool = Field(False, description="Whether the entry address may land in this region")


class ValidateLayoutRequest(BaseModel):
    hex_text: str = Field(..., description="Intel HEX text, one record per line")
    regions: list[RegionIn] = Field(
        ...,
        description="Memory regions; must be non-overlapping half-open intervals [start, end)",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "hex_text": (
                        ":020000040800F2\n"
                        ":10000000214601360121470136007EFE09D2190141\n"
                        ":04000005080001C12D\n"
                        ":00000001FF\n"
                    ),
                    "regions": [
                        {
                            "name": "flash",
                            "start": 134217728,
                            "end": 134218752,
                            "allow_data": True,
                            "allow_entry": True,
                        },
                        {
                            "name": "config",
                            "start": 134218752,
                            "end": 134219776,
                            "allow_data": False,
                            "allow_entry": False,
                        },
                    ],
                }
            ]
        }
    }


class ViolationOut(BaseModel):
    kind: str = Field(
        ...,
        description="uncovered | data_not_allowed | entry_not_allowed | region_overlap | region_invalid",
    )
    message: str
    start: int | None = Field(None, description="Inclusive start of the offending byte range, if any")
    end: int | None = Field(None, description="Exclusive end of the offending byte range, if any")
    region: str | None = Field(None, description="Region the violation refers to, if any")


class RegionSummaryOut(BaseModel):
    name: str
    start: int
    end: int
    size: int
    allow_data: bool
    allow_entry: bool
    occupied_bytes: int = Field(..., description="Firmware bytes accepted by this region")
    utilization: float = Field(..., description="occupied_bytes / size")


class ValidateLayoutResponse(BaseModel):
    ok: bool = Field(..., description="True when the HEX parsed cleanly and no layout violation was found")
    hex_ok: bool = Field(..., description="True when no error-level HEX diagnostic was raised")
    layout_evaluated: bool = Field(
        ...,
        description=(
            "True when firmware bytes were checked against the regions; false when the HEX "
            "had errors or the region definitions were invalid, so layout checks were skipped"
        ),
    )
    error_count: int
    warning_count: int
    entry_address: int | None = Field(None, description="From the last 03/05 record, if present")
    total_bytes: int
    regions: list[RegionSummaryOut]
    violations: list[ViolationOut]
    diagnostics: list[DiagnosticOut]


def _build_response(result: ParseResult) -> AssembleResponse:
    return AssembleResponse(
        ok=result.ok,
        error_count=result.error_count,
        warning_count=result.warning_count,
        entry_address=result.entry_address,
        total_bytes=result.total_bytes,
        segment_count=len(result.segments),
        segments=[
            SegmentOut(
                start=s.start,
                end=s.end,
                size=len(s.data),
                data_base64=base64.b64encode(s.data).decode("ascii"),
            )
            for s in result.segments
        ],
        gaps=[GapOut(start=g.start, end=g.end, size=g.size) for g in result.gaps],
        diagnostics=[DiagnosticOut(**vars(d)) for d in result.diagnostics],
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/assemble", response_model=AssembleResponse, tags=["assemble"])
def assemble(request: AssembleRequest) -> AssembleResponse:
    """Assemble a firmware image from Intel HEX text sent as JSON."""
    return _build_response(parse_intel_hex(request.hex_text))


@app.post("/api/v1/assemble/raw", response_model=AssembleResponse, tags=["assemble"])
def assemble_raw(hex_text: str = Body(..., media_type="text/plain")) -> AssembleResponse:
    """Assemble a firmware image from a raw Intel HEX text body (text/plain)."""
    return _build_response(parse_intel_hex(hex_text))


@app.post("/api/v1/validate-layout", response_model=ValidateLayoutResponse, tags=["layout"])
def validate_layout(request: ValidateLayoutRequest) -> ValidateLayoutResponse:
    """Validate the assembled firmware image against a memory layout.

    The HEX text is parsed with the same rules as /api/v1/assemble. When
    parsing fails, only diagnostics are returned and no layout judgment is
    made. Otherwise each contiguous firmware segment is split at region
    boundaries: bytes outside every region or inside an ``allow_data=false``
    region are reported, and the entry address (if any) must land in an
    ``allow_entry=true`` region.
    """
    result = parse_intel_hex(request.hex_text)
    diagnostics = [DiagnosticOut(**vars(d)) for d in result.diagnostics]
    common = {
        "error_count": result.error_count,
        "warning_count": result.warning_count,
        "entry_address": result.entry_address,
        "total_bytes": result.total_bytes,
        "diagnostics": diagnostics,
    }
    if not result.ok:
        return ValidateLayoutResponse(
            ok=False,
            hex_ok=False,
            layout_evaluated=False,
            regions=[],
            violations=[],
            **common,
        )
    layout = check_layout(
        result.segments,
        result.entry_address,
        [Region(**vars(r)) for r in request.regions],
    )
    return ValidateLayoutResponse(
        ok=layout.ok,
        hex_ok=True,
        layout_evaluated=layout.regions_valid,
        regions=[RegionSummaryOut(**vars(s)) for s in layout.summaries],
        violations=[ViolationOut(**vars(v)) for v in layout.violations],
        **common,
    )
