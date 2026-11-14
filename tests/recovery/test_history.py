"""Incident memory: the append-only ``incidents.jsonl`` read as complete lines only
(guardrail 24), and the search a repair brief and ``python -m recovery incidents`` use."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recovery.history import append_jsonl, read_jsonl, search, similar
from recovery.model import FailureClass, Incident, IncidentState
from recovery.store import Store


def _incident(incident_id: str, created_at: float, **kw: object) -> Incident:
    base: dict[str, object] = {
        "incident_id": incident_id,
        "signature": "sig-a",
        "failure_class": FailureClass.HARNESS_SUSPECT,
        "summary": "TimeoutExpired in run",
        "workspace": "W",
        "state": IncidentState.CLOSED,
        "created_at": created_at,
        "updated_at": created_at,
        "innermost_scio_frame": "scio/harness/providers/claude_code.py:run",
    }
    base.update(kw)
    return Incident(**base)  # type: ignore[arg-type]


# incidents.jsonl


def test_each_append_is_one_complete_lf_line_that_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "home" / "incidents.jsonl"
    first = _incident("inc-1", 1.0, summary="line one\nline two", issue_number=7)
    append_jsonl(path, first, "detected")
    append_jsonl(path, _incident("inc-1", 1.0, state=IncidentState.ISSUE_OPEN), "issue #7 opened")
    data = path.read_bytes()
    assert b"\r" not in data and data.endswith(b"\n") and data.count(b"\n") == 2
    rows = read_jsonl(path)
    assert [r["event"] for r in rows] == ["detected", "issue #7 opened"]
    assert all(isinstance(r["at"], float) for r in rows)
    assert Incident.from_dict(rows[0]["incident"]) == first
    assert Incident.from_dict(rows[1]["incident"]).state is IncidentState.ISSUE_OPEN


def test_a_missing_file_reads_as_no_lines(tmp_path: Path) -> None:
    assert read_jsonl(tmp_path / "incidents.jsonl") == []


def _two_lines(tmp_path: Path) -> tuple[Path, bytes]:
    path = tmp_path / "incidents.jsonl"
    append_jsonl(path, _incident("inc-1", 1.0), "detected")
    append_jsonl(path, _incident("inc-2", 2.0), "detected")
    return path, path.read_bytes()


@pytest.mark.parametrize(
    "tail",
    [
        b'{"at": 3.0, "event": "det',  # cut mid-record, no newline yet
        '{"at": 3.0, "event": "é'.encode()[:-1],  # cut inside a UTF-8 character
        b'{"at": 3.0}',  # complete JSON, but the newline is not written yet
        b"\x00\x00\x00\x00\n",  # a write cut short and padded by the filesystem
        b'{"at": 3.0, "ev\n',  # the last complete line does not parse
    ],
    ids=["torn", "torn-utf8", "no-newline", "nul-padded", "invalid-last"],
)
def test_a_torn_or_invalid_last_line_is_left_out(tmp_path: Path, tail: bytes) -> None:
    path, data = _two_lines(tmp_path)
    path.write_bytes(data + tail)
    assert [r["incident"]["incident_id"] for r in read_jsonl(path)] == ["inc-1", "inc-2"]


@pytest.mark.parametrize(
    "corrupt", [b'{"at": 1.0, "ev\n', b"\xff\xfe\n", b"[1, 2]\n"], ids=["json", "utf8", "array"]
)
def test_a_corrupt_line_before_the_last_raises(tmp_path: Path, corrupt: bytes) -> None:
    path, data = _two_lines(tmp_path)
    first, second = data.splitlines(keepends=True)
    path.write_bytes(first + corrupt + second)
    with pytest.raises(ValueError, match=r"incidents\.jsonl:2"):
        read_jsonl(path)


def test_blank_lines_are_skipped(tmp_path: Path) -> None:
    path, data = _two_lines(tmp_path)
    path.write_bytes(b"\n" + data.replace(b"\n", b"\n\n"))
    assert len(read_jsonl(path)) == 2


# search


@pytest.fixture
def store() -> Store:
    store = Store(":memory:", clock=lambda: 50.0)
    store.save_incident(_incident("inc-1", 10.0, summary="TimeoutExpired in claude run"))
    store.save_incident(
        _incident(
            "inc-2",
            20.0,
            signature="sig-b",
            summary="KeyError 'thread'",
            innermost_scio_frame="scio/cli.py:_build",
            escalation="health check failed: scio report exited 1",
        )
    )
    store.save_incident(_incident("inc-3", 30.0, state=IncidentState.REPAIRING))
    store.save_incident(
        _incident(
            "inc-4",
            40.0,
            signature="sig-c",
            failure_class=FailureClass.EXTERNAL_AUTH,
            summary="not logged in",
            innermost_scio_frame="",
        )
    )
    store.add_event("inc-1", "repair PR #120 merged; ledger I-180")
    return store


def _ids(incidents: list[Incident]) -> list[str]:
    return [i.incident_id for i in incidents]


def test_search_by_signature_is_exact_and_newest_first(store: Store) -> None:
    assert _ids(search(store, signature="sig-a")) == ["inc-3", "inc-1"]
    assert _ids(search(store, signature="sig")) == []
    assert _ids(search(store)) == ["inc-4", "inc-3", "inc-2", "inc-1"]
    assert _ids(search(store, limit=2)) == ["inc-4", "inc-3"]


def test_search_by_text_reads_the_summary_frame_escalation_and_events(store: Store) -> None:
    assert _ids(search(store, text="TIMEOUTEXPIRED")) == ["inc-3", "inc-1"]
    assert _ids(search(store, text="cli.py:_build")) == ["inc-2"]
    assert _ids(search(store, text="health check")) == ["inc-2"]
    assert _ids(search(store, text="i-180")) == ["inc-1"]  # only in an event
    assert _ids(search(store, text="no such words")) == []


def test_search_by_signature_and_text_needs_both(store: Store) -> None:
    assert _ids(search(store, signature="sig-a", text="pr #120")) == ["inc-1"]
    assert _ids(search(store, signature="sig-b", text="pr #120")) == []


# similar incidents for a repair brief


def test_similar_matches_the_signature_or_the_frame_newest_first(store: Store) -> None:
    frame = "scio/cli.py:_build"
    assert _ids(similar(store, "sig-a", frame)) == ["inc-3", "inc-2", "inc-1"]
    assert _ids(similar(store, "sig-z", frame)) == ["inc-2"]
    assert _ids(similar(store, "sig-c", "scio/none.py:f")) == ["inc-4"]


def test_similar_leaves_out_the_given_incident_and_respects_the_limit(store: Store) -> None:
    frame = "scio/harness/providers/claude_code.py:run"
    assert _ids(similar(store, "sig-a", frame, exclude="inc-3")) == ["inc-1"]
    assert _ids(similar(store, "sig-a", frame, limit=1)) == ["inc-3"]
    assert similar(store, "sig-a", frame, limit=0) == []


def test_an_empty_frame_or_signature_is_like_nothing(store: Store) -> None:
    assert _ids(similar(store, "sig-z", "")) == []  # inc-4 has no frame either
    assert _ids(similar(store, "", "scio/cli.py:_build")) == ["inc-2"]
    assert similar(store, "", "") == []


def test_the_jsonl_export_is_valid_json_per_line(tmp_path: Path) -> None:
    path = tmp_path / "incidents.jsonl"
    append_jsonl(path, _incident("inc-1", 1.0, escalation='a "quoted" reason'), "escalated")
    (line,) = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["incident"]["escalation"] == 'a "quoted" reason'
