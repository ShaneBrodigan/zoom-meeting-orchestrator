"""Checkpoint tests for the tshark capture wrapper (orchestrator/capture.py).

Local, no tshark / no root / no ens5: a tiny fake process stands in for tshark, so
the command-building (interface, the exact pre-NAT filter, output path) and the
start/stop lifecycle (wait-for-ready, terminate, timestamps) are fully exercised
here. The only thing left to verify on VM4 is that real tshark accepts the filter
and captures on ens5.

Run with:  pytest tests/test_capture.py
"""

import pytest

from orchestrator.capture import CaptureError, PacketCapture

# The exact filter documented in REFACTOR_DESIGN.md decision 2 / the AWS setup doc.
EXPECTED_FILTER = (
    "(net 10.0.1.0/24 or net 10.0.2.0/24 or net 10.0.3.0/24 or net 10.0.4.0/24) "
    "and not tcp port 22"
)


class FakeStderr:
    def __init__(self, lines):
        self._lines = list(lines)
        self.exhausted = False

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        self.exhausted = True
        return b""


class FakeProc:
    """Stand-in for a tshark Popen handle."""

    def __init__(self, lines, *, dies_when_silent=False, returncode=0):
        self.stderr = FakeStderr(lines)
        self._dies = dies_when_silent
        self.returncode = returncode
        self.terminated = False
        self.wait_called = False

    def poll(self):
        if self.terminated:
            return self.returncode
        if self._dies and self.stderr.exhausted:
            return self.returncode  # exited (e.g. bad filter) after printing its error
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.wait_called = True
        return self.returncode


class FakeLauncher:
    """Records the argv it was called with and returns a preset process."""

    def __init__(self, proc):
        self.proc = proc
        self.argv = None

    def __call__(self, argv):
        self.argv = argv
        return self.proc


def ready_proc():
    return FakeProc([b"Capturing on 'ens5'\n"])


def counting_clock(start=1000.0, step=1.0):
    state = {"t": start}

    def clock():
        now = state["t"]
        state["t"] += step
        return now

    return clock


# --- filter / command ------------------------------------------------------ #

def test_filter_matches_the_frozen_design_string():
    cap = PacketCapture("/tmp/capture.pcap")
    assert cap.capture_filter == EXPECTED_FILTER


def test_build_argv_has_interface_filter_and_output():
    cap = PacketCapture("/tmp/capture.pcap", interface="ens5")
    argv = cap.build_argv()
    assert argv[0] == "tshark"
    assert argv[argv.index("-i") + 1] == "ens5"
    assert argv[argv.index("-f") + 1] == EXPECTED_FILTER
    assert argv[argv.index("-w") + 1] == "/tmp/capture.pcap"


def test_build_argv_snaplen_defaults_to_256():
    # Snaplen keeps 30-min noise-heavy pcaps from ballooning; 256 B keeps every header
    # (labeler 5-tuples) plus the first payload bytes ET-BERT reads.
    cap = PacketCapture("/tmp/capture.pcap")
    argv = cap.build_argv()
    assert argv[argv.index("-s") + 1] == "256"


def test_build_argv_snaplen_is_overridable():
    cap = PacketCapture("/tmp/capture.pcap", snaplen=128)
    argv = cap.build_argv()
    assert argv[argv.index("-s") + 1] == "128"


# --- start: waits for readiness, records t_start --------------------------- #

def test_start_waits_for_ready_then_records_tstart():
    launcher = FakeLauncher(ready_proc())
    cap = PacketCapture("/tmp/c.pcap", popen=launcher, clock=counting_clock())
    t_start = cap.start()
    assert t_start == cap.t_start
    assert isinstance(t_start, float)
    assert launcher.argv == cap.build_argv()


def test_start_raises_if_tshark_exits_before_capturing():
    proc = FakeProc([b"tshark: Invalid capture filter\n"], dies_when_silent=True, returncode=2)
    cap = PacketCapture("/tmp/c.pcap", popen=FakeLauncher(proc))
    with pytest.raises(CaptureError):
        cap.start()


def test_double_start_raises():
    cap = PacketCapture("/tmp/c.pcap", popen=FakeLauncher(ready_proc()))
    cap.start()
    with pytest.raises(CaptureError):
        cap.start()


# --- stop: terminates, records t_stop -------------------------------------- #

def test_stop_terminates_and_records_tstop():
    proc = ready_proc()
    cap = PacketCapture("/tmp/c.pcap", popen=FakeLauncher(proc), clock=counting_clock())
    cap.start()
    t_stop = cap.stop()
    assert proc.terminated is True
    assert proc.wait_called is True
    assert t_stop == cap.t_stop
    assert cap.t_stop > cap.t_start  # window closes after it opens


def test_stop_without_start_raises():
    cap = PacketCapture("/tmp/c.pcap", popen=FakeLauncher(ready_proc()))
    with pytest.raises(CaptureError):
        cap.stop()


def test_stop_is_idempotent():
    proc = ready_proc()
    cap = PacketCapture("/tmp/c.pcap", popen=FakeLauncher(proc), clock=counting_clock())
    cap.start()
    first = cap.stop()
    assert cap.stop() == first  # second stop returns the same t_stop, no re-terminate


def test_stop_raises_if_tshark_will_not_die():
    class StubbornProc(FakeProc):
        def wait(self, timeout=None):
            raise TimeoutError("still running")

    proc = StubbornProc([b"Capturing on 'ens5'\n"])
    cap = PacketCapture("/tmp/c.pcap", popen=FakeLauncher(proc))
    cap.start()
    with pytest.raises(CaptureError):
        cap.stop()


# --- context manager ------------------------------------------------------- #

def test_context_manager_starts_and_stops():
    proc = ready_proc()
    cap = PacketCapture("/tmp/c.pcap", popen=FakeLauncher(proc), clock=counting_clock())
    with cap as c:
        assert c.t_start is not None
        assert proc.terminated is False
    assert proc.terminated is True
    assert cap.t_stop is not None