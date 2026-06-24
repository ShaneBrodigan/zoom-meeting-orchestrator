"""Checkpoint tests for the VM5 noise generator (client/noise.py + common/noise_config.py).

Pure, local, no AWS / iperf / curl / ffmpeg / network / real sleeping: the two
outside-world edges (running a traffic command, sleeping the gap) are injected as
recording fakes, so the whole seeded scheduling brain is exercised in memory. These prove:

* the multi-profile config round-trips through JSON and maps to the recorded ``NoiseBlock``,
* the same seed yields the same burst/gap sequence (reproducible) and different seeds differ,
* every drawn parameter stays inside the configured ranges/sets, per profile,
* profile selection respects the configured weights,
* the iperf / curl / ffmpeg command lines are built correctly, and
* one run cycle fires exactly one command then sleeps exactly one drawn gap.

Run with:  pytest tests/test_noise.py
"""

import pytest

from common.noise_config import (
    DownloadProfile,
    IperfProfile,
    NoiseConfig,
    VideoProfile,
)
from common.schema import NoiseBlock
from client.noise import (
    DownloadBurst,
    IperfBurst,
    NoiseGenerator,
    VideoBurst,
    build_curl_command,
    build_ffmpeg_command,
    build_iperf_command,
)


def make_iperf(**overrides):
    base = dict(
        weight=0.25,
        target="203.0.113.10",
        ports=[5201, 5202, 5203],
        protocols=["tcp", "udp"],
        rate_mbps=(0.5, 8.0),
        burst_s=(2, 10),
        reverse_prob=0.5,
    )
    base.update(overrides)
    return IperfProfile(**base)


def make_download(**overrides):
    base = dict(
        weight=0.45,
        urls=["https://host.example/a.bin", "https://host.example/b.bin"],
        rate_mbps=(0.5, 10.0),
        max_time_s=(5, 30),
    )
    base.update(overrides)
    return DownloadProfile(**base)


def make_video(**overrides):
    base = dict(
        weight=0.30,
        streams=["https://host.example/master.m3u8"],
        duration_s=(15, 90),
    )
    base.update(overrides)
    return VideoProfile(**base)


def make_config(**overrides):
    base = dict(
        seed=4711,
        gap_s=(0.5, 5.0),
        iperf=make_iperf(),
        download=make_download(),
        video=make_video(),
    )
    base.update(overrides)
    return NoiseConfig(**base)


# --- config shape ---------------------------------------------------------- #

def test_config_dict_round_trip():
    cfg = make_config()
    assert NoiseConfig.from_dict(cfg.to_dict()) == cfg


def test_config_round_trip_with_subset_of_profiles():
    cfg = make_config(download=None, video=None)
    restored = NoiseConfig.from_dict(cfg.to_dict())
    assert restored == cfg
    assert restored.download is None and restored.video is None


def test_to_noise_block_summarises_mix_and_keeps_iperf_anchor():
    block = make_config().to_noise_block()
    assert isinstance(block, NoiseBlock)
    assert block.enabled is True
    assert block.profile == "iperf+download+video"
    assert block.target == "203.0.113.10"     # the iperf anchor (future concurrent-VoIP)
    assert block.ports == "5201,5202,5203"
    assert "iperf 0.5-8Mbps" in block.intensity
    assert "video 15-90s" in block.intensity


def test_to_noise_block_without_iperf_has_no_anchor():
    block = make_config(iperf=None).to_noise_block()
    assert block.profile == "download+video"
    assert block.target is None and block.ports is None


# --- config validation ----------------------------------------------------- #

def test_config_rejects_no_profiles():
    with pytest.raises(ValueError):
        NoiseConfig(seed=1, gap_s=(0.5, 5.0))


def test_config_rejects_bad_protocol():
    with pytest.raises(ValueError):
        make_iperf(protocols=["tcp", "sctp"])


def test_config_rejects_inverted_range():
    with pytest.raises(ValueError):
        make_iperf(rate_mbps=(8.0, 0.5))


def test_config_rejects_no_ports():
    with pytest.raises(ValueError):
        make_iperf(ports=[])


def test_config_rejects_bad_reverse_prob():
    with pytest.raises(ValueError):
        make_iperf(reverse_prob=1.5)


def test_config_rejects_non_positive_weight():
    with pytest.raises(ValueError):
        make_iperf(weight=0.0)


def test_iperf_rejects_rate_floor_that_rounds_to_zero():
    # A sub-0.05 Mbps floor would round to 0 -> "iperf3 -b 0M" = unlimited (a flood).
    with pytest.raises(ValueError):
        make_iperf(rate_mbps=(0.04, 5.0))


def test_download_rejects_rate_floor_that_rounds_to_zero():
    # curl --limit-rate 0 likewise means "no cap" -> a flood.
    with pytest.raises(ValueError):
        make_download(rate_mbps=(0.04, 5.0))


def test_download_rejects_empty_urls():
    with pytest.raises(ValueError):
        make_download(urls=[])


def test_video_rejects_empty_streams():
    with pytest.raises(ValueError):
        make_video(streams=[])


# --- seeded scheduling ----------------------------------------------------- #

def _drain(cfg, n):
    """Run n cycles on a fresh generator, returning the (command, gap) sequence."""
    commands, gaps = [], []
    gen = NoiseGenerator(cfg, run_command=commands.append, sleep=gaps.append)
    for _ in range(n):
        gen.run_cycle()
    return list(zip(commands, gaps))


def test_same_seed_is_reproducible():
    assert _drain(make_config(), 30) == _drain(make_config(), 30)


def test_different_seeds_differ():
    assert _drain(make_config(seed=1), 30) != _drain(make_config(seed=2), 30)


def test_drawn_iperf_params_stay_in_range():
    cfg = make_iperf()
    from client.noise import IperfRunner
    import random
    rng = random.Random(7)
    for _ in range(500):
        b = IperfRunner(cfg).draw_burst(rng)
        assert cfg.burst_s[0] <= b.duration_s <= cfg.burst_s[1]
        assert cfg.rate_mbps[0] <= b.rate_mbps <= cfg.rate_mbps[1]
        assert b.protocol in cfg.protocols
        assert b.port in cfg.ports
        assert isinstance(b.reverse, bool)


def test_drawn_download_params_stay_in_range():
    cfg = make_download()
    from client.noise import DownloadRunner
    import random
    rng = random.Random(7)
    runner = DownloadRunner(cfg)
    for _ in range(500):
        b = runner.draw_burst(rng)
        assert b.url in cfg.urls
        assert cfg.rate_mbps[0] <= b.rate_mbps <= cfg.rate_mbps[1]
        assert cfg.max_time_s[0] <= b.max_time_s <= cfg.max_time_s[1]


def test_drawn_video_params_stay_in_range():
    cfg = make_video()
    from client.noise import VideoRunner
    import random
    rng = random.Random(7)
    runner = VideoRunner(cfg)
    for _ in range(500):
        b = runner.draw_burst(rng)
        assert b.stream in cfg.streams
        assert cfg.duration_s[0] <= b.duration_s <= cfg.duration_s[1]


def test_gap_stays_in_range():
    cfg = make_config()
    for _, gap in _drain(cfg, 500):
        assert cfg.gap_s[0] <= gap <= cfg.gap_s[1]


# --- weighted profile selection -------------------------------------------- #

def test_single_profile_is_always_picked():
    cfg = make_config(download=None, video=None)
    gen = NoiseGenerator(cfg, run_command=lambda a: None, sleep=lambda s: None)
    assert all(gen.pick_runner().name == "iperf" for _ in range(100))


def test_weights_are_respected():
    # Heavily favour downloads; over many draws it must dominate the mix.
    cfg = make_config(
        iperf=make_iperf(weight=1.0),
        download=make_download(weight=8.0),
        video=make_video(weight=1.0),
    )
    gen = NoiseGenerator(cfg, run_command=lambda a: None, sleep=lambda s: None)
    picks = [gen.pick_runner().name for _ in range(2000)]
    assert picks.count("download") > picks.count("iperf")
    assert picks.count("download") > picks.count("video")
    # All three still appear (no weight is starved out).
    assert {"iperf", "download", "video"} == set(picks)


# --- command lines --------------------------------------------------------- #

def test_iperf_command_tcp_upload():
    cmd = build_iperf_command(
        IperfBurst(duration_s=5, rate_mbps=4.0, protocol="tcp", port=5201, reverse=False),
        "203.0.113.10",
    )
    assert cmd == ["iperf3", "-c", "203.0.113.10", "-p", "5201", "-t", "5", "-b", "4M"]


def test_iperf_command_udp_has_u_flag():
    cmd = build_iperf_command(
        IperfBurst(duration_s=3, rate_mbps=2.5, protocol="udp", port=5202, reverse=False),
        "203.0.113.10",
    )
    assert "-u" in cmd
    assert "-b" in cmd and cmd[cmd.index("-b") + 1] == "2.5M"
    assert "-R" not in cmd


def test_iperf_command_reverse_has_r_flag():
    cmd = build_iperf_command(
        IperfBurst(duration_s=8, rate_mbps=6.0, protocol="tcp", port=5203, reverse=True),
        "203.0.113.10",
    )
    assert "-R" in cmd
    assert "-u" not in cmd


def test_curl_command_caps_rate_in_bytes_per_second():
    cmd = build_curl_command(
        DownloadBurst(url="https://host.example/a.bin", rate_mbps=2.0, max_time_s=20),
    )
    assert cmd == [
        "curl", "-s", "-o", "/dev/null",
        "--max-time", "20",
        "--limit-rate", "250000",   # 2 Mbps = 250,000 bytes/s
        "https://host.example/a.bin",
    ]


def test_ffmpeg_command_streams_real_time_and_discards():
    cmd = build_ffmpeg_command(
        VideoBurst(stream="https://host.example/master.m3u8", duration_s=45),
    )
    assert cmd == [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-re", "-i", "https://host.example/master.m3u8",
        "-t", "45",
        "-f", "null", "-",
    ]


# --- one cycle drives the injected edges ----------------------------------- #

def test_run_cycle_fires_one_command_then_sleeps_one_gap():
    cfg = make_config()
    ran, slept = [], []
    gen = NoiseGenerator(cfg, run_command=ran.append, sleep=slept.append)

    # Predict the first cycle's command + gap from a parallel same-seed generator.
    predictor = NoiseGenerator(cfg, run_command=lambda a: None, sleep=lambda s: None)
    expected_cmd = predictor.pick_runner().draw_command(predictor._rng)
    expected_gap = predictor.draw_gap()

    gen.run_cycle()

    assert ran == [expected_cmd]
    assert slept == [expected_gap]


# --- the real subprocess edge must never wedge or crash the loop ------------ #

def test_burst_runner_bounds_each_burst_with_a_timeout():
    import subprocess
    import client.noise as n

    seen = {}

    def fake_run(argv, check, timeout):
        seen["timeout"] = timeout
        return None

    orig = subprocess.run
    subprocess.run = fake_run
    try:
        n._run_command_subprocess(["iperf3", "-c", "x"])
    finally:
        subprocess.run = orig
    assert seen["timeout"] == n._BURST_TIMEOUT_S


def test_burst_runner_survives_a_hang():
    import subprocess
    import client.noise as n

    def fake_run(argv, check, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    orig = subprocess.run
    subprocess.run = fake_run
    try:
        n._run_command_subprocess(["ffmpeg", "-i", "dead-stream"])  # must not raise
    finally:
        subprocess.run = orig


def test_burst_runner_survives_a_missing_program():
    import subprocess
    import client.noise as n

    def fake_run(argv, check, timeout):
        raise FileNotFoundError("ffmpeg not installed")

    orig = subprocess.run
    subprocess.run = fake_run
    try:
        n._run_command_subprocess(["ffmpeg", "-i", "x"])  # must not raise
    finally:
        subprocess.run = orig
