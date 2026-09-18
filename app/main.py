"""FastAPI firmware image assembly service."""

from __future__ import annotations

import base64

from fastapi import Body, FastAPI
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .hex_parser import ParseResult, parse_intel_hex

app = FastAPI(
    title="Firmware Image Assembly Service",
    version="1.0.0",
    description=(
        "Accepts Intel HEX text, validates every record (byte count and "
        "two's-complement checksum; record types 00/01/02/03/04/05), assembles "
        "the firmware image and returns contiguous Base64 segments, "
        "inter-segment gaps, the entry address and per-line diagnostics."
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
