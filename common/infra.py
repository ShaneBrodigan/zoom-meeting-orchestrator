"""Fixed VPC deployment facts: which VM sits at which private IP.

A single home for the per-subnet client IPs so the orchestrator drivers (the bulk
generator and the live-check) don't each re-hardcode them — if a subnet IP ever changes,
it changes here only. Source of truth is ``handoff_zoom_aws_setup.md``. Pure constants,
no AWS.
"""

from __future__ import annotations

VM1_IP = "10.0.1.119"       # private1, Zoom client
VM2_IP = "10.0.2.67"        # private2, Zoom client
VM3_IP = "10.0.3.53"        # private3, Zoom client
VM5_NOISE_IP = "10.0.4.16"  # private4, noise generator (never joins Zoom)

# The subnets that can join a meeting (one bot per subnet, so Zoom uses its relay).
CLIENT_IPS = [VM1_IP, VM2_IP, VM3_IP]
