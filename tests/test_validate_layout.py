"""Tests for the /api/v1/validate-layout endpoint and the layout logic."""

import pytest
from fastapi.testclient import TestClient

from app.hex_parser import parse_intel_hex
from app.layout import Region, validate_layout
from app.main import app

client = TestClient(app)


def rec(rectype: int, addr: int, data: bytes = b"") -> str:
    """Build a valid Intel HEX record line."""
    body = bytes([len(data), (addr >> 8) & 0xFF, addr & 0xFF, rectype]) + bytes(data)
    checksum = (-sum(body)) & 0xFF
    return ":" + (body + bytes([checksum])).hex().upper()


EOF = rec(0x01, 0)


def data_rec(addr: int, data: bytes) -> str:
    return rec(0x00, addr, data)


def post_layout(hex_text: str, regions: list[dict]) -> dict:
    resp = client.post("/api/v1/validate-layout", json={"hex_text": hex_text, "regions": regions})
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# HEX parse failure: diagnostics only, no layout judgment
# ---------------------------------------------------------------------------

def test_invalid_hex_returns_only_diagnostics():
    body = post_layout(
        ":00000001FE\n",  # corrupt EOF checksum
        [{"name": "flash", "start": 0, "end": 0x100}],
    )
    assert body["ok"] is False
    assert body["hex_ok"] is False
    assert body["layout_evaluated"] is False
    assert body["regions"] == []
    assert body["violations"] == []
    assert body["error_count"] >= 1
    assert any("checksum" in d["message"] for d in body["diagnostics"])


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

def test_layout_ok_when_bytes_and_entry_inside_regions():
    text = "\n".join([
        rec(0x04, 0, b"\x08\x00"),                 # base 0x0800_0000
        data_rec(0x0000, bytes(range(16))),
        rec(0x05, 0, b"\x08\x00\x00\x01"),         # entry 0x0800_0001
        EOF,
    ])
    body = post_layout(text, [
        {"name": "flash", "start": 0x08000000, "end": 0x08040000,
         "allow_data": True, "allow_entry": True},
    ])
    assert body["ok"] is True
    assert body["hex_ok"] is True
    assert body["layout_evaluated"] is True
    assert body["violations"] == []
    assert body["total_bytes"] == 16
    summary = body["regions"][0]
    assert summary["name"] == "flash"
    assert summary["size"] == 0x40000
    assert summary["occupied_bytes"] == 16
    assert summary["utilization"] == pytest.approx(16 / 0x40000)


def test_no_entry_record_means_no_entry_check():
    text = "\n".join([data_rec(0x0000, b"\x01\x02"), EOF])
    body = post_layout(text, [{"name": "flash", "start": 0, "end": 0x10}])
    assert body["ok"] is True
    assert body["entry_address"] is None
    assert body["violations"] == []


# ---------------------------------------------------------------------------
# byte-range violations
# ---------------------------------------------------------------------------

def test_uncovered_bytes_split_at_region_boundary():
    # segment [0x100, 0x110); region covers only [0x108, 0x200)
    text = "\n".join([data_rec(0x0100, bytes(range(16))), EOF])
    body = post_layout(text, [{"name": "flash", "start": 0x108, "end": 0x200}])
    assert body["ok"] is False
    assert body["layout_evaluated"] is True
    assert len(body["violations"]) == 1
    v = body["violations"][0]
    assert v["kind"] == "uncovered"
    assert (v["start"], v["end"]) == (0x100, 0x108)
    assert v["region"] is None
    assert body["regions"][0]["occupied_bytes"] == 8
    assert body["regions"][0]["utilization"] == pytest.approx(8 / 0xF8)


def test_bytes_in_no_data_region_flagged():
    text = "\n".join([data_rec(0x0000, b"\x01\x02\x03\x04"), EOF])
    body = post_layout(text, [
        {"name": "boot", "start": 0, "end": 0x10, "allow_data": False},
    ])
    assert body["ok"] is False
    assert len(body["violations"]) == 1
    v = body["violations"][0]
    assert v["kind"] == "data_not_allowed"
    assert (v["start"], v["end"]) == (0, 4)
    assert v["region"] == "boot"
    assert body["regions"][0]["occupied_bytes"] == 0
    assert body["regions"][0]["utilization"] == 0.0


def test_segment_split_across_regions_and_gap():
    # segment [0, 0x30); A covers [0, 0x10), B covers [0x20, 0x30), gap between
    text = "\n".join([data_rec(0x0000, bytes(range(0x30))), EOF])
    body = post_layout(text, [
        {"name": "a", "start": 0x00, "end": 0x10},
        {"name": "b", "start": 0x20, "end": 0x30},
    ])
    assert body["ok"] is False
    assert len(body["violations"]) == 1
    v = body["violations"][0]
    assert v["kind"] == "uncovered"
    assert (v["start"], v["end"]) == (0x10, 0x20)
    by_name = {r["name"]: r for r in body["regions"]}
    assert by_name["a"]["occupied_bytes"] == 0x10
    assert by_name["b"]["occupied_bytes"] == 0x10
    assert by_name["a"]["utilization"] == 1.0


def test_bytes_outside_all_regions_when_region_list_empty():
    text = "\n".join([data_rec(0x0000, b"\xAA\xBB"), EOF])
    body = post_layout(text, [])
    assert body["ok"] is False
    assert [v["kind"] for v in body["violations"]] == ["uncovered"]
    assert (body["violations"][0]["start"], body["violations"][0]["end"]) == (0, 2)
    assert body["regions"] == []


# ---------------------------------------------------------------------------
# entry address
# ---------------------------------------------------------------------------

def test_entry_in_region_without_allow_entry_flagged():
    text = "\n".join([
        rec(0x04, 0, b"\x08\x00"),
        data_rec(0x0000, b"\x00" * 4),
        rec(0x05, 0, b"\x08\x00\x00\x01"),
        EOF,
    ])
    body = post_layout(text, [
        {"name": "flash", "start": 0x08000000, "end": 0x08040000,
         "allow_data": True, "allow_entry": False},
    ])
    assert body["ok"] is False
    entries = [v for v in body["violations"] if v["kind"] == "entry_not_allowed"]
    assert len(entries) == 1
    assert entries[0]["region"] == "flash"
    assert entries[0]["start"] == 0x08000001


def test_entry_outside_all_regions_flagged():
    text = "\n".join([
        data_rec(0x0000, b"\x00" * 4),
        rec(0x05, 0, b"\x00\x00\x10\x00"),  # entry 0x1000, far from the region
        EOF,
    ])
    body = post_layout(text, [
        {"name": "flash", "start": 0, "end": 0x100, "allow_entry": True},
    ])
    assert body["ok"] is False
    entries = [v for v in body["violations"] if v["kind"] == "entry_not_allowed"]
    assert len(entries) == 1
    assert entries[0]["region"] is None
    assert entries[0]["start"] == 0x1000


# ---------------------------------------------------------------------------
# region definition problems
# ---------------------------------------------------------------------------

def test_overlapping_regions_reported_and_layout_skipped():
    text = "\n".join([data_rec(0x0000, b"\x01" * 4), EOF])
    body = post_layout(text, [
        {"name": "a", "start": 0x00, "end": 0x20},
        {"name": "b", "start": 0x10, "end": 0x30},
    ])
    assert body["ok"] is False
    assert body["hex_ok"] is True
    assert body["layout_evaluated"] is False
    assert [v["kind"] for v in body["violations"]] == ["region_overlap"]
    v = body["violations"][0]
    assert (v["start"], v["end"]) == (0x10, 0x20)
    assert v["region"] == "b"
    # byte-level checks skipped: no occupancy counted
    assert all(r["occupied_bytes"] == 0 for r in body["regions"])


def test_region_with_empty_interval_reported():
    text = "\n".join([data_rec(0x0000, b"\x01"), EOF])
    body = post_layout(text, [{"name": "bad", "start": 0x10, "end": 0x10}])
    assert body["ok"] is False
    assert body["layout_evaluated"] is False
    assert [v["kind"] for v in body["violations"]] == ["region_invalid"]
    assert body["violations"][0]["region"] == "bad"


# ---------------------------------------------------------------------------
# pure layout logic
# ---------------------------------------------------------------------------

def test_validate_layout_unit_summaries_sorted_by_address():
    text = "\n".join([data_rec(0x0000, b"\x01" * 8), EOF])
    result = parse_intel_hex(text)
    layout = validate_layout(
        result.segments,
        result.entry_address,
        [
            Region(name="hi", start=0x04, end=0x08),
            Region(name="lo", start=0x00, end=0x04),
        ],
    )
    assert layout.ok
    assert [s.name for s in layout.summaries] == ["lo", "hi"]
    assert [s.occupied_bytes for s in layout.summaries] == [4, 4]
    assert all(s.utilization == 1.0 for s in layout.summaries)


def test_validate_layout_unit_chained_overlap_detected():
    # C overlaps A even though B sits between them in address order
    layout = validate_layout(
        [],
        None,
        [
            Region(name="a", start=0, end=100),
            Region(name="b", start=10, end=20),
            Region(name="c", start=30, end=40),
        ],
    )
    assert not layout.regions_valid
    kinds = [v.kind for v in layout.violations]
    assert kinds == ["region_overlap", "region_overlap"]
    assert {v.region for v in layout.violations} == {"b", "c"}
