# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] - 2026-08-17

### Added

- Core CLI (`sysdiag`) with `argparse`-based flags, running network, system
  health, and process checks by default (`--network`, `--system`,
  `--processes` to run a single group).
- Network checks: ping reachability + latency, DNS resolution via both the
  system resolver and a public resolver directly (distinguishes "the internet
  is down" from "your DNS resolver is broken"), local/public IP, default
  gateway, per-interface up/down status, traceroute.
- System health checks: disk usage per partition, memory, CPU, uptime,
  battery, firewall status (Windows profiles / `ufw` on Linux).
- Process listing: top N by CPU and by memory.
- Port checks (`--ports`, opt-in): TCP connect check against common ports or
  a custom `--port-list`.
- Recent system errors (`--events`, opt-in): last N Error/Critical events from
  the Windows Event Log or `journalctl`.
- Problem flagging with configurable thresholds (`--disk-threshold`,
  `--mem-threshold`) and a process exit code (`1` if problems found) for use
  in cron/CI/monitoring.
- Suggested Fixes: plain-English, OS-aware fix suggestions for each flagged
  problem — suggest-only, never executes anything.
- `--json` output, `--quiet` (problems-only) mode, `--output <file>` to also
  save the report as plain text, JSON, or self-contained HTML (by extension).
- Colorized report output via `rich`; colors auto-suppress when piped.
- Packaging via `pyproject.toml` (`pip install -e .` exposes a `sysdiag`
  command); documented standalone-executable build via PyInstaller.
- Full type hints, clean `mypy` pass.
- Test suite (`pytest`, mocked system calls) with coverage reporting; CI
  (GitHub Actions) running tests + coverage + mypy on Ubuntu and Windows.
