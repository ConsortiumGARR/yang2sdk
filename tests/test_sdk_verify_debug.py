"""Offline contract for `sdk-verify --debug` (no device, no docker).

--debug must stream every endpoint row live while changing nothing about the
verdict: same statuses, same details, same exit logic. Timing populates the
previously-always-None `Result.seconds`; tracebacks appear only for FAILs
recorded inside an `except` block and only on stderr.
"""

import io
import json
from contextlib import redirect_stderr

from yang2sdk.cli.sdk_verify import FAIL, PASS, Report, Verifier, build_parser


class _Client:
    """RESTCONF-shaped stub: no `has_nmda`, so the RESTCONF branch is taken."""


def _verifier(debug: bool = True) -> tuple[Verifier, Report]:
    report: Report = Report(device="d", protocol="restconf")
    return Verifier(_Client(), report, debug=debug, depth=2), report


def test_debug_flag_defaults_off_and_parses() -> None:
    assert build_parser().parse_args(["--device", "d"]).debug is False
    assert build_parser().parse_args(["--device", "d", "--debug"]).debug is True


def test_debug_off_is_silent() -> None:
    verifier, _ = _verifier(debug=False)
    buf = io.StringIO()
    with redirect_stderr(buf):
        verifier._record("/a", "container", "retrieve", PASS, "model Foo")
        verifier._skip("/b", "container", "retrieve", "some reason")
    assert buf.getvalue() == ""


def test_debug_streams_every_status_with_counter_and_timing() -> None:
    verifier, report = _verifier()
    buf = io.StringIO()
    with redirect_stderr(buf):
        verifier._record("/a", "container", "retrieve", PASS, "model Foo", seconds=0.42)
        verifier._skip("/b", "container", "retrieve", "some reason")
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            verifier._record("/c", "list", "retrieve", FAIL, "RuntimeError: kaboom")
    lines = [line for line in buf.getvalue().splitlines() if line.startswith("[debug]")]
    outcomes = [line for line in lines if "->" in line]
    assert len(outcomes) == 3
    assert outcomes[0].startswith("[debug] #1 /a") and "0.42s" in outcomes[0]
    assert outcomes[1].startswith("[debug] #2 /b") and "skip" in outcomes[1]
    assert outcomes[2].startswith("[debug] #3 /c") and "fail" in outcomes[2]
    # The FAIL was recorded inside `except`: a traceback follows its line.
    trace = [line for line in lines if line.startswith("[debug]   | ")]
    assert any("RuntimeError: kaboom" in line for line in trace)
    # Verdict unchanged: timing stored, detail has no request-shape leakage.
    assert [r.seconds for r in report.results] == [0.42, None, None]
    assert all(
        set(r.keys()) == {"node", "kind", "method", "status", "detail", "seconds"}
        for r in json.loads(report.to_json())["results"]
    )


def test_debug_fail_outside_except_has_no_traceback() -> None:
    verifier, _ = _verifier()
    buf = io.StringIO()
    with redirect_stderr(buf):
        # Digest-mismatch style FAIL: no active exception.
        verifier._record("-", "crud", "verify-restore", FAIL, "RESTORE NOT PROVEN")
    lines = buf.getvalue().splitlines()
    assert len(lines) == 1 and "RESTORE NOT PROVEN" in lines[0]


def test_check_retrieve_times_both_outcomes_and_shows_request_shape() -> None:
    class OkNav:
        def retrieve(self, **kwargs: object) -> object:
            return object()

    class BoomNav:
        def retrieve(self, **kwargs: object) -> None:
            raise RuntimeError("kaboom")

    verifier, report = _verifier()
    buf = io.StringIO()
    with redirect_stderr(buf):
        verifier._check_retrieve(OkNav(), "/ok")
        verifier._check_retrieve(BoomNav(), "/bad")
    out = buf.getvalue()
    assert "/ok" in out and "content=config depth=2" in out
    assert "/bad" in out and "RuntimeError" in out
    assert all(r.seconds is not None for r in report.results)
    # Request shape is console-only: stored details carry no `source=` text.
    assert all("content=config" not in r.detail for r in report.results)
