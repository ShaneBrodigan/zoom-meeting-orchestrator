"""Checkpoint tests for the VM5 noise generator (client/noise.py + common/noise_config.py).

Pure, local, no AWS / iperf / network / real sleeping: the two outside-world edges
(running iperf, sleeping the gap) are injected as recording fakes, so the whole seeded
scheduling brain is exercised in memory. These prove:

* the config round-trips through JSON and maps to the recorded ``NoiseBlock`` (decision 10),
* the same seed yields the same burst/gap sequence (reproducible) and different seeds differ,
* every drawn parameter stays inside the configured ranges/sets,
* the iperf command line is built correctly for TCP / UDP / reverse(download), and
* one run cycle fires exactly one iperf then sleeps exactly one drawn gap.

Run with:  pytest tests/test_noise.py
"""

import pytest

from common.noise_config import NoiseConfig
from common.schema import NoiseBlock
from client.noise import IperfBurst, NoiseGenerator, build_iperf_command


def make_config(**overrides):
    base = dict(
        target="203.0.113.10",
        ports=[5201, 5202, 5203],
        protocols=["tcp", "udp"],
        rate_mbps=(1.0, 50.0),
        burst_s=(2, 10),
        gap_s=(0.5, 5.0),
        reverse_prob=0.5,
        seed=4711,
    )
    base.update(overrides)
    return NoiseConfig(**base)


# --- config shape ---------------------------------------------------------- #

def test_config_dict_round_trip():
    cfg = make_config()
    assert NoiseConfig.from_dict(cfg.to_dict()) == cfg


def test_to_noise_block_records_anchor():
    block = make_config().to_noise_block()
    assert isinstance(block, NoiseBlock)
    assert block.enabled is True
    assert block.profile == "iperf"
    assert block.target == "203.0.113.10"     # the labeler's destination-IP anchor
    assert block.ports == "5201,5202,5203"
    assert block.intensity == "1-50Mbps"


def test_config_rejects_bad_protocol():
    with pytest.raises(ValueError):
        make_config(protocols=["tcp", "sctp"])


def test_config_rejects_inverted_range():
    with pytest.raises(ValueError):
        make_config(rate_mbps=(50.0, 1.0))


def test_config_rejects_no_ports():
    with pytest.raises(ValueError):
        make_config(ports=[])


def test_config_rejects_bad_reverse_prob():
    with pytest.raises(ValueError):
        make_config(reverse_prob=1.5)


def test_config_rejects_rate_floor_that_rounds_to_zero():
    # A sub-0.05 Mbps floor would round to 0 -> "iperf3 -b 0M" = unlimited (a flood).
    with pytest.raises(ValueError):
        make_config(rate_mbps=(0.04, 5.0))


# --- seeded scheduling ----------------------------------------------------- #

def _drain(cfg, n):
    """Draw n (burst, gap) pairs in run-cycle order from a fresh generator."""
    gen = NoiseGenerator(cfg, run_iperf=lambda argv: None, sleep=lambda s: None)
    return [(gen.draw_burst(), gen.draw_gap()) for _ in range(n)]


def test_same_seed_is_reproducible():
    assert _drain(make_config(), 20) == _drain(make_config(), 20)


def test_different_seeds_differ():
    assert _drain(make_config(seed=1), 20) != _drain(make_config(seed=2), 20)


def test_drawn_params_stay_in_range():
    cfg = make_config()
    for burst, gap in _drain(cfg, 500):
        assert cfg.burst_s[0] <= burst.duration_s <= cfg.burst_s[1]
        assert cfg.rate_mbps[0] <= burst.rate_mbps <= cfg.rate_mbps[1]
        assert burst.protocol in cfg.protocols
        assert burst.port in cfg.ports
        assert isinstance(burst.reverse, bool)
        assert cfg.gap_s[0] <= gap <= cfg.gap_s[1]


def test_both_directions_drawn_when_reverse_prob_is_mid():
    directions = {burst.reverse for burst, _ in _drain(make_config(), 200)}
    assert directions == {True, False}   # both upload and download appear


def test_reverse_prob_zero_is_upload_only():
    assert all(not burst.reverse for burst, _ in _drain(make_config(reverse_prob=0.0), 100))


# --- iperf command line ---------------------------------------------------- #

def test_command_tcp_upload():
    cmd = build_iperf_command(
        IperfBurst(duration_s=5, rate_mbps=10.0, protocol="tcp", port=5201, reverse=False),
        "203.0.113.10",
    )
    assert cmd == ["iperf3", "-c", "203.0.113.10", "-p", "5201", "-t", "5", "-b", "10M"]


def test_command_udp_has_u_flag():
    cmd = build_iperf_command(
        IperfBurst(duration_s=3, rate_mbps=12.5, protocol="udp", port=5202, reverse=False),
        "203.0.113.10",
    )
    assert "-u" in cmd
    assert "-b" in cmd and cmd[cmd.index("-b") + 1] == "12.5M"
    assert "-R" not in cmd


def test_command_reverse_has_r_flag():
    cmd = build_iperf_command(
        IperfBurst(duration_s=8, rate_mbps=20.0, protocol="tcp", port=5203, reverse=True),
        "203.0.113.10",
    )
    assert "-R" in cmd
    assert "-u" not in cmd


# --- one cycle drives the injected edges ----------------------------------- #

def test_run_cycle_fires_one_iperf_then_sleeps_one_gap():
    cfg = make_config()
    ran, slept = [], []
    gen = NoiseGenerator(cfg, run_iperf=ran.append, sleep=slept.append)

    # Predict the first cycle's burst + gap from a parallel same-seed generator.
    predictor = NoiseGenerator(cfg, run_iperf=lambda a: None, sleep=lambda s: None)
    expected_cmd = build_iperf_command(predictor.draw_burst(), cfg.target)
    expected_gap = predictor.draw_gap()

    gen.run_cycle()

    assert ran == [expected_cmd]
    assert slept == [expected_gap]