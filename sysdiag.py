#!/usr/bin/env python3
"""sysdiag — a simple cross-platform IT diagnostics CLI."""

from __future__ import annotations

import argparse
import ipaddress
import platform
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

import dns.exception
import dns.resolver
import psutil
from rich.console import Console
from rich.text import Text

console = Console(record=True)  # record=True lets --output export the full report as plain text

BYTES_PER_GB = 1024 ** 3
DISK_WARN_PERCENT = 90
MEMORY_WARN_PERCENT = 90
DEFAULT_PORTS = [22, 53, 80, 443, 3389]  # ssh, dns, http, https, rdp — common L1 checks
PUBLIC_DNS_RESOLVER = "8.8.8.8"


def line(text: str, style: str = "") -> None:
    # Text() never parses its content as markup, so this stays safe even for
    # dynamic content (hostnames, subprocess output, error messages)
    console.print(Text(text, style=style))


def print_header(name: str) -> None:
    line(f"== {name} ==", style="bold cyan")


def get_disk_usage() -> list[dict[str, Any]]:
    """Return usage stats for every mounted partition."""
    results = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            # e.g. an empty CD-ROM drive on Windows — skip it, don't crash the run
            continue
        results.append({
            "device": part.device,
            "mountpoint": part.mountpoint,
            "total_gb": round(usage.total / BYTES_PER_GB, 2),
            "used_gb": round(usage.used / BYTES_PER_GB, 2),
            "free_gb": round(usage.free / BYTES_PER_GB, 2),
            "percent": usage.percent,
        })
    return results


def get_memory_usage() -> dict[str, float]:
    mem = psutil.virtual_memory()
    return {
        "total_gb": round(mem.total / BYTES_PER_GB, 2),
        "used_gb": round(mem.used / BYTES_PER_GB, 2),
        "available_gb": round(mem.available / BYTES_PER_GB, 2),
        "percent": mem.percent,
    }


def get_cpu_usage() -> float:
    # 1s sample gives a real reading; psutil's non-blocking mode would return 0.0 on first call
    return psutil.cpu_percent(interval=1)


def get_battery_status() -> dict[str, Any] | None:
    try:
        battery = psutil.sensors_battery()
    except (AttributeError, NotImplementedError):
        return None  # not available on this platform
    if battery is None:
        return None  # desktop with no battery
    return {"percent": round(battery.percent, 1), "plugged_in": battery.power_plugged}


def get_uptime() -> dict[str, str]:
    boot_dt = datetime.fromtimestamp(psutil.boot_time())
    uptime_delta = datetime.now() - boot_dt
    return {
        "boot_time": boot_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime": str(uptime_delta).split(".")[0],
    }


def get_firewall_status() -> dict[str, Any]:
    is_windows = platform.system() == "Windows"
    # ponytail: Linux only checks ufw; firewalld/iptables/nftables aren't covered.
    # Extend get_firewall_status if you need those.
    cmd = ["netsh", "advfirewall", "show", "allprofiles", "state"] if is_windows else ["ufw", "status"]
    try:
        output = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError) as e:
        return {"available": False, "enabled": None, "detail": str(e)}

    if is_windows:
        states = re.findall(r"State\s+(ON|OFF)", output)
        if not states:
            return {"available": False, "enabled": None, "detail": "could not parse netsh output"}
        enabled = all(s == "ON" for s in states)
        return {"available": True, "enabled": enabled, "detail": f"{states.count('ON')}/{len(states)} profiles active"}

    if not output.strip():
        return {"available": False, "enabled": None, "detail": "empty ufw output"}
    enabled = "Status: active" in output
    return {"available": True, "enabled": enabled, "detail": output.strip().splitlines()[0]}


def run_system_checks() -> dict[str, Any]:
    return {
        "disk": get_disk_usage(),
        "memory": get_memory_usage(),
        "cpu_percent": get_cpu_usage(),
        "uptime": get_uptime(),
        "battery": get_battery_status(),
        "firewall": get_firewall_status(),
    }


def print_system_report(system: dict[str, Any]) -> None:
    print_header("System Health")
    line(f"CPU usage: {system['cpu_percent']}%")

    mem = system["memory"]
    line(f"Memory: {mem['used_gb']}GB / {mem['total_gb']}GB used ({mem['percent']}%), "
         f"{mem['available_gb']}GB available")

    uptime = system["uptime"]
    line(f"Boot time: {uptime['boot_time']} (uptime: {uptime['uptime']})")

    firewall = system.get("firewall")
    if firewall and firewall["available"]:
        status = "enabled" if firewall["enabled"] else "DISABLED"
        line(f"Firewall: {status} ({firewall['detail']})", style="green" if firewall["enabled"] else "bold red")

    battery = system.get("battery")
    if battery:
        status = "plugged in" if battery["plugged_in"] else "on battery"
        line(f"Battery: {battery['percent']}% ({status})")

    line("Disk usage:")
    for disk in system["disk"]:
        line(f"  {disk['device']} ({disk['mountpoint']}): "
             f"{disk['used_gb']}GB / {disk['total_gb']}GB used ({disk['percent']}%)")
    console.print()


def ping_host(host: str, count: int = 4) -> dict[str, Any]:
    is_windows = platform.system() == "Windows"
    cmd = ["ping", "-n" if is_windows else "-c", str(count), host]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=count * 2 + 5)
    except (subprocess.SubprocessError, OSError) as e:
        return {"reachable": False, "avg_ms": None, "error": str(e)}

    return {
        "reachable": result.returncode == 0,
        "avg_ms": _parse_ping_avg(result.stdout, is_windows),
    }


def _parse_ping_avg(output: str, is_windows: bool) -> float | None:
    # Windows: "Average = 23ms"; Linux: "rtt min/avg/max/mdev = 0.02/0.03/0.05/0.01 ms"
    pattern = r"Average = (\d+)ms" if is_windows else r"= [\d.]+/([\d.]+)/"
    match = re.search(pattern, output)
    return float(match.group(1)) if match else None


def _is_ip_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def resolve_dns(host: str) -> dict[str, Any]:
    try:
        return {"success": True, "ip": socket.gethostbyname(host)}
    except socket.gaierror as e:
        return {"success": False, "ip": None, "error": str(e)}


def query_dns_server(host: str, resolver_ip: str, timeout: float = 2.0) -> dict[str, Any]:
    if _is_ip_address(host):
        # already an address (e.g. the default host, 8.8.8.8) — nothing to resolve.
        # socket.gethostbyname short-circuits the same way, so mirror it here too.
        return {"success": True, "ip": host, "error": None}

    # queries a specific resolver directly, bypassing the OS-configured one —
    # lets us tell "internet is down" apart from "your DNS resolver is broken"
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = [resolver_ip]
    resolver.timeout = timeout
    resolver.lifetime = timeout
    try:
        answer = resolver.resolve(host, "A")
        return {"success": True, "ip": str(answer[0]), "error": None}
    except dns.exception.DNSException as e:
        return {"success": False, "ip": None, "error": str(e)}


def get_public_ip(timeout: float = 3.0) -> str | None:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=timeout) as resp:
            ip = resp.read().decode().strip()
        return ip or None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def get_local_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # no packet actually sent (UDP), just picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        return None


def get_default_gateway() -> str | None:
    is_windows = platform.system() == "Windows"
    cmd = ["ipconfig"] if is_windows else ["ip", "route", "show", "default"]
    try:
        output = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return None

    if is_windows:
        # ipconfig lists IPv6 and IPv4 default gateways under the same label, IPv4
        # often continuing unlabeled on the next line — prefer the IPv4-looking value
        ipv4_pattern = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
        lines = output.splitlines()
        for i, ln in enumerate(lines):
            if "Default Gateway" in ln:
                value = ln.split(":", 1)[1].strip()
                candidates = [c for c in (value, lines[i + 1].strip() if i + 1 < len(lines) else "") if c]
                for candidate in candidates:
                    if ipv4_pattern.match(candidate):
                        return candidate
                if value:
                    return value
    else:
        match = re.search(r"default via (\S+)", output)
        if match:
            return match.group(1)
    return None


def get_network_interfaces() -> list[dict[str, Any]]:
    stats = psutil.net_if_stats()
    interfaces = []
    for name, addrs in psutil.net_if_addrs().items():
        ipv4 = next((a.address for a in addrs if a.family == socket.AF_INET), None)
        interfaces.append({
            "name": name,
            "ip": ipv4,
            "is_up": stats[name].isup if name in stats else False,
        })
    return interfaces


def traceroute(host: str) -> list[str]:
    is_windows = platform.system() == "Windows"
    # cap per-hop wait so 15 unresponsive hops can't blow past the subprocess timeout
    cmd = (["tracert", "-h", "15", "-w", "1000", host] if is_windows
           else ["traceroute", "-m", "15", "-w", "1", host])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return [ln for ln in result.stdout.splitlines() if ln.strip()]
    except (subprocess.SubprocessError, OSError) as e:
        return [f"traceroute unavailable: {e}"]


def run_network_checks(host: str) -> dict[str, Any]:
    return {
        "host": host,
        "ping": ping_host(host),
        "dns": resolve_dns(host),
        "dns_public": {"resolver": PUBLIC_DNS_RESOLVER, **query_dns_server(host, PUBLIC_DNS_RESOLVER)},
        "local_ip": get_local_ip(),
        "public_ip": get_public_ip(),
        "gateway": get_default_gateway(),
        "interfaces": get_network_interfaces(),
        "traceroute": traceroute(host),
    }


def print_network_report(network: dict[str, Any]) -> None:
    print_header("Network")
    host = network["host"]

    ping = network["ping"]
    if ping["reachable"]:
        line(f"Ping {host}: reachable, avg {ping['avg_ms']}ms", style="green")
    else:
        line(f"Ping {host}: UNREACHABLE", style="bold red")

    dns_result = network["dns"]
    if dns_result["success"]:
        line(f"DNS resolution ({host}): {dns_result['ip']}", style="green")
    else:
        line(f"DNS resolution ({host}): FAILED ({dns_result['error']})", style="bold red")

    dns_public = network["dns_public"]
    if dns_public["success"]:
        line(f"DNS via {dns_public['resolver']}: {dns_public['ip']}", style="green")
    else:
        line(f"DNS via {dns_public['resolver']}: FAILED ({dns_public['error']})", style="bold red")

    line(f"Local IP: {network['local_ip'] or 'unknown'}")
    line(f"Public IP: {network['public_ip'] or 'unknown'}")
    line(f"Default gateway: {network['gateway'] or 'unknown'}")

    line("Interfaces:")
    for iface in network["interfaces"]:
        state = "up" if iface["is_up"] else "down"
        style = "green" if iface["is_up"] else "dim"
        line(f"  {iface['name']}: {iface['ip'] or 'no IPv4'} ({state})", style=style)

    line("Traceroute:")
    for hop in network["traceroute"]:
        line(f"  {hop}")
    console.print()


def get_top_processes(top_n: int = 5) -> dict[str, Any]:
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            p.cpu_percent(None)  # first call always returns 0.0 — this just primes it
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        procs.append(p)

    time.sleep(0.2)  # let cpu_percent measure usage over this interval

    rows = []
    for p in procs:
        try:
            rows.append({
                "pid": p.pid,
                "name": p.name(),
                "cpu_percent": p.cpu_percent(None),
                "memory_percent": round(p.memory_percent(), 2),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return {
        "by_cpu": sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:top_n],
        "by_memory": sorted(rows, key=lambda r: r["memory_percent"], reverse=True)[:top_n],
    }


def print_process_report(processes: dict[str, Any], top_n: int) -> None:
    print_header(f"Top {top_n} Processes")

    line("By CPU:")
    for p in processes["by_cpu"]:
        line(f"  PID {p['pid']:>6}  {p['cpu_percent']:>5.1f}%  {p['name']}")

    line("By Memory:")
    for p in processes["by_memory"]:
        line(f"  PID {p['pid']:>6}  {p['memory_percent']:>5.1f}%  {p['name']}")
    console.print()


def check_port(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_port_checks(host: str, ports: list[int] | None = None) -> dict[str, Any]:
    ports = ports if ports is not None else DEFAULT_PORTS
    return {
        "host": host,
        "ports": [{"port": port, "open": check_port(host, port)} for port in ports],
    }


def print_port_report(port_results: dict[str, Any]) -> None:
    print_header("Ports")
    host = port_results["host"]
    for entry in port_results["ports"]:
        if entry["open"]:
            line(f"  {host}:{entry['port']} - OPEN", style="green")
        else:
            line(f"  {host}:{entry['port']} - closed", style="dim")
    console.print()


def _summarize_windows_event_block(block: str) -> str:
    # wevtutil's /f:text puts each field on its own "Key: value" line, except
    # Description, whose text follows on unlabeled lines after "Description:"
    date = source = event_id = ""
    description_lines = []
    in_description = False
    for raw_line in block.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("Date:"):
            date = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Source:"):
            source = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Event ID:"):
            event_id = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Description:"):
            in_description = True
            remainder = stripped.split(":", 1)[1].strip()
            if remainder:
                description_lines.append(remainder)
        elif in_description and stripped:
            description_lines.append(stripped)
    description = " ".join(description_lines) or "(no description)"
    return f"{date} [{source} / {event_id}] {description}"


def get_recent_system_errors(max_events: int = 5) -> dict[str, Any]:
    is_windows = platform.system() == "Windows"
    cmd = (["wevtutil", "qe", "System", "/q:*[System[(Level=1 or Level=2)]]",
            f"/c:{max_events}", "/rd:true", "/f:text"] if is_windows
           else ["journalctl", "-p", "err", "-n", str(max_events), "--no-pager", "-o", "short"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as e:
        return {"available": False, "events": [], "error": str(e)}

    output = result.stdout.strip()
    if not output:
        return {"available": True, "events": [], "error": None}

    if is_windows:
        events = [_summarize_windows_event_block(b) for b in output.split("\n\n") if b.strip()]
    else:
        events = [ln for ln in output.splitlines() if ln.strip()]
    return {"available": True, "events": events[:max_events], "error": None}


def print_event_report(event_data: dict[str, Any]) -> None:
    print_header("Recent System Errors")
    if not event_data["available"]:
        line(f"Unavailable: {event_data['error']}", style="dim")
    elif not event_data["events"]:
        line("None found.", style="green")
    else:
        for i, event in enumerate(event_data["events"], 1):
            line(f"  [{i}] {event}", style="bold red")
    console.print()


def find_problems(
    results: dict[str, Any],
    disk_threshold: float = DISK_WARN_PERCENT,
    memory_threshold: float = MEMORY_WARN_PERCENT,
) -> list[str]:
    problems = []

    system = results.get("system")
    if system:
        if system["memory"]["percent"] > memory_threshold:
            problems.append(f"Memory usage is high: {system['memory']['percent']}%")
        for disk in system["disk"]:
            if disk["percent"] > disk_threshold:
                problems.append(f"Disk {disk['device']} is almost full: {disk['percent']}%")
        firewall = system.get("firewall")
        if firewall and firewall["available"] and firewall["enabled"] is False:
            problems.append("Firewall is disabled")

    network = results.get("network")
    if network:
        if not network["ping"]["reachable"]:
            problems.append(f"No internet connectivity: ping to {network['host']} failed")
        if not network["dns"]["success"]:
            if network.get("dns_public", {}).get("success"):
                problems.append(f"System DNS resolver is broken (public DNS works) for {network['host']}")
            else:
                problems.append(f"DNS resolution failed for {network['host']}")

    events = results.get("events")
    if events and events["available"] and events["events"]:
        problems.append(f"Found {len(events['events'])} recent system error(s) in the event log")

    return problems


def suggest_fixes(
    results: dict[str, Any],
    disk_threshold: float = DISK_WARN_PERCENT,
    memory_threshold: float = MEMORY_WARN_PERCENT,
) -> list[dict[str, str]]:
    """Suggest-only: describes the fix, never runs anything."""
    is_windows = platform.system() == "Windows"
    suggestions = []

    system = results.get("system")
    if system:
        if system["memory"]["percent"] > memory_threshold:
            processes = results.get("processes")
            top_consumer = (processes or {}).get("by_memory") or None
            if top_consumer:
                top = top_consumer[0]
                suggestion = f"Close high-memory processes, e.g. {top['name']} (using {top['memory_percent']}% RAM)"
            else:
                suggestion = "Close unused applications, or restart the machine to free memory"
            suggestions.append({"problem": "High memory usage", "suggestion": suggestion})

        for disk in system["disk"]:
            if disk["percent"] > disk_threshold:
                cleanup = "Disk Cleanup (cleanmgr.exe)" if is_windows else "apt clean / journalctl --vacuum-size=200M"
                suggestions.append({
                    "problem": f"Disk {disk['device']} almost full",
                    "suggestion": f"Free up space on {disk['device']}: run {cleanup}, or delete unused files",
                })

        firewall = system.get("firewall")
        if firewall and firewall["available"] and firewall["enabled"] is False:
            enable_cmd = "netsh advfirewall set allprofiles state on" if is_windows else "sudo ufw enable"
            suggestions.append({"problem": "Firewall is disabled", "suggestion": f"Enable it: {enable_cmd}"})

    events = results.get("events")
    if events and events["available"] and events["events"]:
        review_cmd = "Open Event Viewer (eventvwr.msc)" if is_windows else "journalctl -p err -n 20"
        suggestions.append({
            "problem": "Recent system errors found",
            "suggestion": f"Review the details: {review_cmd}",
        })

    network = results.get("network")
    if network:
        if not network["ping"]["reachable"]:
            suggestions.append({
                "problem": "No internet connectivity",
                "suggestion": "Check the physical network cable / Wi-Fi connection, or restart the router/modem",
            })
        elif not network["dns"]["success"]:
            if network.get("dns_public", {}).get("success"):
                flush_cmd = "ipconfig /flushdns" if is_windows else "sudo resolvectl flush-caches"
                suggestions.append({
                    "problem": "System DNS resolver is broken",
                    "suggestion": f"Flush the DNS cache: {flush_cmd}",
                })
            else:
                suggestions.append({
                    "problem": "DNS resolution failed",
                    "suggestion": "Check firewall/VPN settings blocking DNS (port 53), or try a different DNS server",
                })

    return suggestions


def print_problems(problems: list[str]) -> None:
    print_header("Problems")
    if not problems:
        line("None detected.", style="green")
    else:
        for problem in problems:
            line(f"  [!] {problem}", style="bold red")
    console.print()


def print_suggested_fixes(fixes: list[dict[str, str]]) -> None:
    if not fixes:
        return
    print_header("Suggested Fixes")
    for fix in fixes:
        line(f"  {fix['problem']}:", style="bold")
        line(f"    {fix['suggestion']}", style="cyan")
    console.print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sysdiag",
        description="Run L1 help-desk diagnostics: network, system health, and processes.",
    )
    parser.add_argument("--network", action="store_true", help="run only network checks")
    parser.add_argument("--system", action="store_true", help="run only system health checks")
    parser.add_argument("--processes", action="store_true", help="run only process checks")
    parser.add_argument("--ports", action="store_true", help="run only port checks (opt-in, not part of the default run)")
    parser.add_argument("--port-list", default=None,
                         help=f"comma-separated ports to check (default: {','.join(map(str, DEFAULT_PORTS))})")
    parser.add_argument("--events", action="store_true",
                         help="check for recent system errors in the event log / journal (opt-in, not part of the default run)")
    parser.add_argument("--event-count", type=int, default=5, help="number of recent error events to show (default: 5)")
    parser.add_argument("--host", default="8.8.8.8", help="host to ping/resolve/port-check (default: 8.8.8.8)")
    parser.add_argument("--json", action="store_true", help="output results as JSON")
    parser.add_argument("--top", type=int, default=5, help="number of top processes to show (default: 5)")
    parser.add_argument("--quiet", "-q", action="store_true", help="only show the problems summary")
    parser.add_argument("--disk-threshold", type=float, default=DISK_WARN_PERCENT,
                         help=f"flag a disk as a problem above this usage percent (default: {DISK_WARN_PERCENT})")
    parser.add_argument("--mem-threshold", type=float, default=MEMORY_WARN_PERCENT,
                         help=f"flag memory as a problem above this usage percent (default: {MEMORY_WARN_PERCENT})")
    parser.add_argument("--output", default=None, help="also write the report to this file")
    return parser


def write_output_file(path: str, content: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        print(f"Warning: could not write output file {path}: {e}", file=sys.stderr)


def parse_port_list(raw: str | None, parser: argparse.ArgumentParser) -> list[int] | None:
    if not raw:
        return None
    try:
        return [int(p.strip()) for p in raw.split(",")]
    except ValueError:
        parser.error(f"--port-list must be comma-separated integers, got: {raw!r}")
        return None  # unreachable: parser.error() exits, but keeps mypy happy


def run_checks(args: argparse.Namespace, port_list: list[int] | None) -> dict[str, Any]:
    # no group flags set -> run everything (ports/events are opt-in only, see their --help)
    run_all = not (args.network or args.system or args.processes or args.ports or args.events)

    results: dict[str, Any] = {}
    if run_all or args.network:
        results["network"] = run_network_checks(args.host)
    if run_all or args.system:
        results["system"] = run_system_checks()
    if run_all or args.processes:
        results["processes"] = get_top_processes(args.top)
    if args.ports:
        results["ports"] = run_port_checks(args.host, port_list)
    if args.events:
        results["events"] = get_recent_system_errors(args.event_count)

    results["problems"] = find_problems(results, args.disk_threshold, args.mem_threshold)
    results["suggested_fixes"] = suggest_fixes(results, args.disk_threshold, args.mem_threshold)
    return results


def print_report(results: dict[str, Any], args: argparse.Namespace) -> None:
    if not args.quiet:
        if "network" in results:
            print_network_report(results["network"])
        if "system" in results:
            print_system_report(results["system"])
        if "processes" in results:
            print_process_report(results["processes"], args.top)
        if "ports" in results:
            print_port_report(results["ports"])
        if "events" in results:
            print_event_report(results["events"])
    print_problems(results["problems"])
    print_suggested_fixes(results["suggested_fixes"])


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    port_list = parse_port_list(args.port_list, parser)

    results = run_checks(args, port_list)

    if args.json:
        import json
        if args.quiet:
            output = {"problems": results["problems"], "suggested_fixes": results["suggested_fixes"]}
        else:
            output = results
        json_str = json.dumps(output, indent=2)
        print(json_str)
        if args.output:
            write_output_file(args.output, json_str)
    else:
        print_report(results, args)
        if args.output:
            is_html = args.output.lower().endswith((".html", ".htm"))
            write_output_file(args.output, console.export_html() if is_html else console.export_text())

    return 1 if results["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
