"""Memory-layout validation for assembled firmware images.

A layout is a list of regions — non-overlapping half-open intervals
``[start, end)`` — describing where firmware bytes and the entry address
are allowed to live.  Each contiguous firmware segment is split at region
boundaries; every piece must be covered by a region with
``allow_data=True``, and the entry address (if any) must land in a region
with ``allow_entry=True``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .hex_parser import Segment


def _fmt(addr: int) -> str:
    return f"0x{addr:04X}"


@dataclass
class Region:
    """Half-open address range ``[start, end)`` with usage permissions."""

    name: str
    start: int  # inclusive
    end: int  # exclusive
    allow_data: bool = True
    allow_entry: bool = False

    @property
    def size(self) -> int:
        return self.end - self.start


@dataclass
class Violation:
    """A single layout problem. ``start``/``end`` are a half-open range."""

    kind: str  # uncovered | data_not_allowed | entry_not_allowed | region_overlap | region_invalid
    message: str
    start: int | None = None
    end: int | None = None  # exclusive
    region: str | None = None


@dataclass
class RegionSummary:
    name: str
    start: int
    end: int
    size: int
    allow_data: bool
    allow_entry: bool
    occupied_bytes: int
    utilization: float  # occupied_bytes / size


@dataclass
class LayoutResult:
    regions_valid: bool  # False when region definitions overlap or are empty intervals
    summaries: list[RegionSummary]
    violations: list[Violation]

    @property
    def ok(self) -> bool:
        return not self.violations


def validate_layout(
    segments: list[Segment],
    entry_address: int | None,
    regions: list[Region],
) -> LayoutResult:
    """Check an assembled firmware image against a memory layout.

    Region definitions are validated first; overlapping or empty
    (``start >= end``) regions make the layout ambiguous, so byte-level
    checks are skipped and only the definition violations are reported.
    """
    violations: list[Violation] = []

    for r in regions:
        if r.start >= r.end:
            violations.append(Violation(
                kind="region_invalid",
                message=(
                    f"region '{r.name}': start {_fmt(r.start)} must be below "
                    f"end {_fmt(r.end)} (half-open interval)"
                ),
                start=r.start,
                end=r.end,
                region=r.name,
            ))

    ordered = sorted(regions, key=lambda r: (r.start, r.end))
    anchor: Region | None = None  # valid region with the largest end so far
    for r in ordered:
        if r.start >= r.end:
            continue
        if anchor is not None and r.start < anchor.end:
            violations.append(Violation(
                kind="region_overlap",
                message=(
                    f"region '{r.name}' overlaps '{anchor.name}' over "
                    f"[{_fmt(r.start)}, {_fmt(min(anchor.end, r.end))})"
                ),
                start=r.start,
                end=min(anchor.end, r.end),
                region=r.name,
            ))
        if anchor is None or r.end > anchor.end:
            anchor = r

    if violations:
        return LayoutResult(
            regions_valid=False,
            summaries=_summaries(ordered, [0] * len(ordered)),
            violations=violations,
        )

    # Split each contiguous segment at region boundaries. ``ordered`` is
    # sorted and non-overlapping, so every piece maps to at most one region.
    occupied = [0] * len(ordered)
    for seg in segments:
        pos = seg.start
        for i, r in enumerate(ordered):
            if r.end <= pos:
                continue
            if r.start >= seg.end:
                break
            if r.start > pos:
                violations.append(_uncovered(pos, r.start))
                pos = r.start
            piece_end = min(r.end, seg.end)
            if r.allow_data:
                occupied[i] += piece_end - pos
            else:
                violations.append(Violation(
                    kind="data_not_allowed",
                    message=(
                        f"{piece_end - pos} byte(s) at [{_fmt(pos)}, {_fmt(piece_end)}) "
                        f"fall in region '{r.name}' (allow_data=false)"
                    ),
                    start=pos,
                    end=piece_end,
                    region=r.name,
                ))
            pos = piece_end
        if pos < seg.end:
            violations.append(_uncovered(pos, seg.end))

    if entry_address is not None:
        hit = next((r for r in ordered if r.start <= entry_address < r.end), None)
        if hit is None:
            violations.append(Violation(
                kind="entry_not_allowed",
                message=f"entry address {_fmt(entry_address)} is not covered by any region",
                start=entry_address,
                end=entry_address + 1,
            ))
        elif not hit.allow_entry:
            violations.append(Violation(
                kind="entry_not_allowed",
                message=(
                    f"entry address {_fmt(entry_address)} lies in region "
                    f"'{hit.name}' with allow_entry=false"
                ),
                start=entry_address,
                end=entry_address + 1,
                region=hit.name,
            ))

    return LayoutResult(
        regions_valid=True,
        summaries=_summaries(ordered, occupied),
        violations=violations,
    )


def _uncovered(start: int, end: int) -> Violation:
    return Violation(
        kind="uncovered",
        message=f"{end - start} byte(s) at [{_fmt(start)}, {_fmt(end)}) are not covered by any region",
        start=start,
        end=end,
    )


def _summaries(regions: list[Region], occupied: list[int]) -> list[RegionSummary]:
    return [
        RegionSummary(
            name=r.name,
            start=r.start,
            end=r.end,
            size=r.size,
            allow_data=r.allow_data,
            allow_entry=r.allow_entry,
            occupied_bytes=occ,
            utilization=occ / r.size if r.size > 0 else 0.0,
        )
        for r, occ in zip(regions, occupied)
    ]
