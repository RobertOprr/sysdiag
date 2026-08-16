#!/usr/bin/env python3
"""Regenerate assets/demo.svg for the README from fixed sample data.

Uses fabricated (non-real) values so the committed screenshot never leaks the
machine's actual public IP, gateway, etc. Run after changing report formatting:

    python scripts/generate_demo_svg.py
"""

import argparse
from pathlib import Path

import sysdiag

DEMO_RESULTS = {
    "network": {
        "host": "example.com",
        "ping": {"reachable": True, "avg_ms": 14.0},
        "dns": {"success": True, "ip": "93.184.216.34"},
        "dns_public": {"resolver": "8.8.8.8", "success": True, "ip": "93.184.216.34", "error": None},
        "local_ip": "192.168.1.42",
        "public_ip": "203.0.113.7",
        "gateway": "192.168.1.1",
        "interfaces": [
            {"name": "Wi-Fi", "ip": "192.168.1.42", "is_up": True},
            {"name": "Ethernet", "ip": None, "is_up": False},
        ],
        "traceroute": [
            "Tracing route to example.com [93.184.216.34]",
            "  1     1 ms     1 ms     1 ms  192.168.1.1",
            "  2     8 ms     7 ms     8 ms  93.184.216.34",
            "Trace complete.",
        ],
    },
    "system": {
        "cpu_percent": 4.2,
        "memory": {"total_gb": 16.0, "used_gb": 9.76, "available_gb": 6.24, "percent": 61.0},
        "uptime": {"boot_time": "2026-08-16 08:03:12", "uptime": "1 day, 3:12:04"},
        "firewall": {"available": True, "enabled": True, "detail": "3/3 profiles active"},
        "battery": {"percent": 78.0, "plugged_in": True},
        "disk": [
            {"device": "C:\\", "mountpoint": "C:\\", "total_gb": 500.0,
             "used_gb": 465.0, "free_gb": 35.0, "percent": 93.0},
        ],
    },
    "processes": {
        "by_cpu": [
            {"pid": 4521, "name": "python.exe", "cpu_percent": 18.4, "memory_percent": 1.2},
            {"pid": 812, "name": "chrome.exe", "cpu_percent": 9.1, "memory_percent": 4.6},
        ],
        "by_memory": [
            {"pid": 812, "name": "chrome.exe", "cpu_percent": 9.1, "memory_percent": 4.6},
            {"pid": 4521, "name": "python.exe", "cpu_percent": 18.4, "memory_percent": 1.2},
        ],
    },
}
DEMO_RESULTS["problems"] = sysdiag.find_problems(DEMO_RESULTS)
DEMO_RESULTS["suggested_fixes"] = sysdiag.suggest_fixes(DEMO_RESULTS)


def main() -> None:
    args = argparse.Namespace(quiet=False, top=5)
    sysdiag.print_report(DEMO_RESULTS, args)

    out_path = Path(__file__).resolve().parent.parent / "assets" / "demo.svg"
    sysdiag.console.save_svg(str(out_path), title="sysdiag")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
