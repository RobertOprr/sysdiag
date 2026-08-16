import json
import socket

import sysdiag


# ---- find_problems (threshold flagging) ----

def test_find_problems_flags_high_memory():
    results = {"system": {"memory": {"percent": 95.0}, "disk": []}}
    problems = sysdiag.find_problems(results)
    assert any("Memory" in p for p in problems)


def test_find_problems_flags_high_disk():
    results = {"system": {"memory": {"percent": 10.0}, "disk": [{"device": "C:\\", "percent": 91.0}]}}
    problems = sysdiag.find_problems(results)
    assert any("C:\\" in p for p in problems)


def test_find_problems_no_issues_below_thresholds():
    results = {"system": {"memory": {"percent": 50.0}, "disk": [{"device": "C:\\", "percent": 50.0}]}}
    assert sysdiag.find_problems(results) == []


def test_find_problems_flags_no_internet():
    results = {"network": {"host": "8.8.8.8", "ping": {"reachable": False}, "dns": {"success": True}}}
    problems = sysdiag.find_problems(results)
    assert any("No internet" in p for p in problems)


def test_find_problems_flags_dns_failure():
    results = {"network": {"host": "bad.invalid", "ping": {"reachable": True}, "dns": {"success": False}}}
    problems = sysdiag.find_problems(results)
    assert any("DNS resolution failed" in p for p in problems)


def test_find_problems_empty_results():
    assert sysdiag.find_problems({}) == []


def test_find_problems_respects_custom_thresholds():
    results = {"system": {"memory": {"percent": 60.0}, "disk": []}}
    assert sysdiag.find_problems(results, memory_threshold=90) == []
    assert len(sysdiag.find_problems(results, memory_threshold=50)) == 1


def test_find_problems_distinguishes_broken_resolver_from_no_internet():
    results = {"network": {
        "host": "example.com", "ping": {"reachable": True},
        "dns": {"success": False}, "dns_public": {"success": True},
    }}
    problems = sysdiag.find_problems(results)
    assert any("System DNS resolver is broken" in p for p in problems)


def test_find_problems_dns_totally_down_when_public_also_fails():
    results = {"network": {
        "host": "example.com", "ping": {"reachable": True},
        "dns": {"success": False}, "dns_public": {"success": False},
    }}
    problems = sysdiag.find_problems(results)
    assert any("DNS resolution failed" in p for p in problems)
    assert not any("resolver is broken" in p for p in problems)


# ---- suggest_fixes (suggest-only, never executes anything) ----

def test_suggest_fixes_empty_when_healthy():
    results = {
        "system": {"memory": {"percent": 10.0}, "disk": []},
        "network": {"ping": {"reachable": True}, "dns": {"success": True}},
    }
    assert sysdiag.suggest_fixes(results) == []


def test_suggest_fixes_high_memory_names_top_process():
    results = {
        "system": {"memory": {"percent": 95.0}, "disk": []},
        "processes": {"by_memory": [{"name": "chrome.exe", "memory_percent": 8.3}]},
    }
    fixes = sysdiag.suggest_fixes(results)
    assert len(fixes) == 1
    assert "chrome.exe" in fixes[0]["suggestion"]


def test_suggest_fixes_high_memory_generic_advice_without_process_data():
    results = {"system": {"memory": {"percent": 95.0}, "disk": []}}
    fixes = sysdiag.suggest_fixes(results)
    assert len(fixes) == 1
    assert "chrome" not in fixes[0]["suggestion"].lower()
    assert "restart" in fixes[0]["suggestion"].lower() or "close" in fixes[0]["suggestion"].lower()


def test_suggest_fixes_disk_full(monkeypatch):
    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Windows")
    results = {"system": {"memory": {"percent": 10.0}, "disk": [{"device": "C:\\", "percent": 95.0}]}}
    fixes = sysdiag.suggest_fixes(results)
    assert len(fixes) == 1
    assert "cleanmgr" in fixes[0]["suggestion"]


def test_suggest_fixes_respects_custom_thresholds():
    results = {"system": {"memory": {"percent": 60.0}, "disk": []}}
    assert sysdiag.suggest_fixes(results, memory_threshold=90) == []
    assert len(sysdiag.suggest_fixes(results, memory_threshold=50)) == 1


def test_suggest_fixes_no_internet():
    results = {"network": {"ping": {"reachable": False}, "dns": {"success": True}}}
    fixes = sysdiag.suggest_fixes(results)
    assert len(fixes) == 1
    assert "router" in fixes[0]["suggestion"].lower() or "wi-fi" in fixes[0]["suggestion"].lower()


def test_suggest_fixes_broken_resolver_windows(monkeypatch):
    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Windows")
    results = {"network": {
        "ping": {"reachable": True},
        "dns": {"success": False}, "dns_public": {"success": True},
    }}
    fixes = sysdiag.suggest_fixes(results)
    assert len(fixes) == 1
    assert "ipconfig /flushdns" in fixes[0]["suggestion"]


def test_suggest_fixes_broken_resolver_linux(monkeypatch):
    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Linux")
    results = {"network": {
        "ping": {"reachable": True},
        "dns": {"success": False}, "dns_public": {"success": True},
    }}
    fixes = sysdiag.suggest_fixes(results)
    assert len(fixes) == 1
    assert "resolvectl" in fixes[0]["suggestion"]


def test_suggest_fixes_dns_totally_down_suggests_firewall_check():
    results = {"network": {
        "ping": {"reachable": True},
        "dns": {"success": False}, "dns_public": {"success": False},
    }}
    fixes = sysdiag.suggest_fixes(results)
    assert len(fixes) == 1
    assert "firewall" in fixes[0]["suggestion"].lower()


def test_suggest_fixes_no_internet_suppresses_dns_suggestion():
    # when there's no internet at all, don't also suggest a DNS-specific fix —
    # that would be misleading about the actual root cause
    results = {"network": {"ping": {"reachable": False}, "dns": {"success": False}}}
    fixes = sysdiag.suggest_fixes(results)
    assert len(fixes) == 1
    assert "router" in fixes[0]["suggestion"].lower() or "wi-fi" in fixes[0]["suggestion"].lower()


# ---- suggested fixes wired through main() ----

def test_main_report_shows_suggested_fixes_section(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [{"device": "C:\\", "mountpoint": "C:\\", "total_gb": 100.0,
                   "used_gb": 95.0, "free_gb": 5.0, "percent": 95.0}],
        "memory": {"total_gb": 16.0, "used_gb": 1.6, "available_gb": 14.4, "percent": 10.0},
        "cpu_percent": 1.0, "uptime": {"boot_time": "x", "uptime": "1:00:00"}, "battery": None,
    })

    sysdiag.main(["--system"])
    out = capsys.readouterr().out

    assert "== Suggested Fixes ==" in out
    assert "Disk C:\\ almost full" in out


def test_main_report_hides_suggested_fixes_section_when_healthy(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [{"device": "C:\\", "mountpoint": "C:\\", "total_gb": 100.0,
                   "used_gb": 50.0, "free_gb": 50.0, "percent": 50.0}],
        "memory": {"total_gb": 16.0, "used_gb": 4.0, "available_gb": 12.0, "percent": 25.0},
        "cpu_percent": 1.0, "uptime": {"boot_time": "x", "uptime": "1:00:00"}, "battery": None,
    })

    sysdiag.main(["--system"])
    out = capsys.readouterr().out

    assert "Suggested Fixes" not in out


# ---- battery status (mocked psutil) ----

def test_get_battery_status_no_battery(monkeypatch):
    monkeypatch.setattr(sysdiag.psutil, "sensors_battery", lambda: None)
    assert sysdiag.get_battery_status() is None


def test_get_battery_status_unsupported_platform(monkeypatch):
    def raise_not_implemented():
        raise NotImplementedError

    monkeypatch.setattr(sysdiag.psutil, "sensors_battery", raise_not_implemented)
    assert sysdiag.get_battery_status() is None


def test_get_battery_status_present(monkeypatch):
    fake_battery = type("Battery", (), {"percent": 76.4, "power_plugged": True})()
    monkeypatch.setattr(sysdiag.psutil, "sensors_battery", lambda: fake_battery)
    assert sysdiag.get_battery_status() == {"percent": 76.4, "plugged_in": True}


# ---- port checks (mocked socket) ----

def test_check_port_open(monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(sysdiag.socket, "create_connection", lambda addr, timeout: FakeSocket())
    assert sysdiag.check_port("example.com", 443) is True


def test_check_port_closed(monkeypatch):
    def raise_os_error(addr, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(sysdiag.socket, "create_connection", raise_os_error)
    assert sysdiag.check_port("example.com", 9999) is False


def test_run_port_checks_uses_default_ports(monkeypatch):
    monkeypatch.setattr(sysdiag, "check_port", lambda host, port: port == 443)
    result = sysdiag.run_port_checks("example.com")
    assert [p["port"] for p in result["ports"]] == sysdiag.DEFAULT_PORTS
    assert {p["port"]: p["open"] for p in result["ports"]}[443] is True


def test_run_port_checks_uses_custom_port_list(monkeypatch):
    monkeypatch.setattr(sysdiag, "check_port", lambda host, port: True)
    result = sysdiag.run_port_checks("example.com", [8080, 8443])
    assert [p["port"] for p in result["ports"]] == [8080, 8443]


def test_parse_port_list_valid():
    parser = sysdiag.build_parser()
    assert sysdiag.parse_port_list("80, 443,8080", parser) == [80, 443, 8080]


def test_parse_port_list_empty_returns_none():
    parser = sysdiag.build_parser()
    assert sysdiag.parse_port_list("", parser) is None


def test_parse_port_list_invalid_exits(capsys):
    parser = sysdiag.build_parser()
    try:
        sysdiag.parse_port_list("not-a-port", parser)
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 2
    assert "port-list" in capsys.readouterr().err


# ---- ports opt-in behavior ----

def test_main_default_run_excludes_ports(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "run_network_checks", lambda host: {
        "host": host, "ping": {"reachable": True, "avg_ms": 1.0},
        "dns": {"success": True, "ip": "1.2.3.4"},
        "local_ip": "192.168.1.2", "gateway": "192.168.1.1", "traceroute": [],
    })
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [], "memory": {"percent": 10.0}, "cpu_percent": 1.0,
        "uptime": {"boot_time": "x", "uptime": "1:00:00"}, "battery": None,
    })
    monkeypatch.setattr(sysdiag, "get_top_processes", lambda top_n: {"by_cpu": [], "by_memory": []})

    sysdiag.main(["--json"])
    output = json.loads(capsys.readouterr().out)
    assert "ports" not in output


def test_main_ports_flag_runs_port_checks(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "check_port", lambda host, port: False)
    sysdiag.main(["--ports", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert "ports" in output
    assert all(p["open"] is False for p in output["ports"]["ports"])


# ---- ping output parsing ----

def test_parse_ping_avg_windows():
    output = "Approximate round trip times in milli-seconds:\n" \
             "    Minimum = 10ms, Maximum = 20ms, Average = 15ms\n"
    assert sysdiag._parse_ping_avg(output, is_windows=True) == 15.0


def test_parse_ping_avg_linux():
    output = "rtt min/avg/max/mdev = 10.123/15.456/20.789/2.345 ms"
    assert sysdiag._parse_ping_avg(output, is_windows=False) == 15.456


def test_parse_ping_avg_no_match_returns_none():
    assert sysdiag._parse_ping_avg("", is_windows=True) is None
    assert sysdiag._parse_ping_avg("garbage output", is_windows=False) is None


# ---- default gateway parsing (mocked subprocess) ----

def test_get_default_gateway_windows(monkeypatch):
    ipconfig_output = (
        "Ethernet adapter Ethernet:\n"
        "   Default Gateway . . . . . . . . . : fe80::1%10\n"
        "                                       192.168.1.1\n"
    )
    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        sysdiag.subprocess, "run",
        lambda *a, **k: type("R", (), {"stdout": ipconfig_output})(),
    )
    assert sysdiag.get_default_gateway() == "192.168.1.1"


def test_get_default_gateway_linux(monkeypatch):
    ip_route_output = "default via 10.0.0.1 dev eth0 proto dhcp metric 100\n"
    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        sysdiag.subprocess, "run",
        lambda *a, **k: type("R", (), {"stdout": ip_route_output})(),
    )
    assert sysdiag.get_default_gateway() == "10.0.0.1"


def test_get_default_gateway_handles_command_failure(monkeypatch):
    def raise_error(*a, **k):
        raise FileNotFoundError("no such command")

    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sysdiag.subprocess, "run", raise_error)
    assert sysdiag.get_default_gateway() is None


# ---- DNS resolution (mocked socket) ----

def test_resolve_dns_success(monkeypatch):
    monkeypatch.setattr(sysdiag.socket, "gethostbyname", lambda host: "1.2.3.4")
    result = sysdiag.resolve_dns("example.com")
    assert result == {"success": True, "ip": "1.2.3.4"}


def test_resolve_dns_failure(monkeypatch):
    def raise_gaierror(host):
        raise socket.gaierror("name resolution failed")

    monkeypatch.setattr(sysdiag.socket, "gethostbyname", raise_gaierror)
    result = sysdiag.resolve_dns("bad.invalid")
    assert result["success"] is False
    assert "error" in result


# ---- resolver-targeted DNS query (mocked dnspython) ----

def test_query_dns_server_short_circuits_for_ip_host():
    # 8.8.8.8 (the default --host) is already an address — nothing to resolve,
    # mirrors how socket.gethostbyname behaves for the system-resolver check
    result = sysdiag.query_dns_server("8.8.8.8", "1.1.1.1")
    assert result == {"success": True, "ip": "8.8.8.8", "error": None}


def test_query_dns_server_success(monkeypatch):
    class FakeResolver:
        def __init__(self, configure=False):
            pass

        def resolve(self, host, rtype):
            return ["1.2.3.4"]

    monkeypatch.setattr(sysdiag.dns.resolver, "Resolver", FakeResolver)
    result = sysdiag.query_dns_server("example.com", "8.8.8.8")
    assert result == {"success": True, "ip": "1.2.3.4", "error": None}


def test_query_dns_server_failure(monkeypatch):
    class FakeResolver:
        def __init__(self, configure=False):
            pass

        def resolve(self, host, rtype):
            raise sysdiag.dns.exception.DNSException("no answer")

    monkeypatch.setattr(sysdiag.dns.resolver, "Resolver", FakeResolver)
    result = sysdiag.query_dns_server("bad.invalid", "8.8.8.8")
    assert result["success"] is False
    assert result["error"]


# ---- network interfaces (mocked psutil) ----

def test_get_network_interfaces(monkeypatch):
    def fake_addr(family, address):
        return type("Addr", (), {"family": family, "address": address})()

    def fake_stats(isup):
        return type("Stats", (), {"isup": isup})()

    monkeypatch.setattr(sysdiag.psutil, "net_if_addrs", lambda: {
        "eth0": [fake_addr(socket.AF_INET, "10.0.0.5")],
        "lo": [fake_addr(socket.AF_INET, "127.0.0.1")],
    })
    monkeypatch.setattr(sysdiag.psutil, "net_if_stats", lambda: {
        "eth0": fake_stats(True), "lo": fake_stats(False),
    })

    interfaces = {i["name"]: i for i in sysdiag.get_network_interfaces()}
    assert interfaces["eth0"] == {"name": "eth0", "ip": "10.0.0.5", "is_up": True}
    assert interfaces["lo"]["is_up"] is False


# ---- public IP (mocked urllib) ----

def test_get_public_ip_success(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"203.0.113.5\n"

    monkeypatch.setattr(sysdiag.urllib.request, "urlopen", lambda url, timeout: FakeResponse())
    assert sysdiag.get_public_ip() == "203.0.113.5"


def test_get_public_ip_failure(monkeypatch):
    def raise_error(url, timeout):
        raise sysdiag.urllib.error.URLError("no route to host")

    monkeypatch.setattr(sysdiag.urllib.request, "urlopen", raise_error)
    assert sysdiag.get_public_ip() is None


# ---- --output flag ----

def test_main_output_flag_writes_report_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [{"device": "C:\\", "mountpoint": "C:\\", "total_gb": 100.0,
                   "used_gb": 50.0, "free_gb": 50.0, "percent": 50.0}],
        "memory": {"total_gb": 16.0, "used_gb": 6.4, "available_gb": 9.6, "percent": 40.0},
        "cpu_percent": 5.0,
        "uptime": {"boot_time": "x", "uptime": "1:00:00"}, "battery": None,
    })
    out_file = tmp_path / "report.txt"

    sysdiag.main(["--system", "--output", str(out_file)])
    capsys.readouterr()

    content = out_file.read_text(encoding="utf-8")
    assert "System Health" in content
    assert "CPU usage: 5.0%" in content
    assert "Problems" in content


def test_main_output_flag_writes_json_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [], "memory": {"percent": 10.0}, "cpu_percent": 1.0,
        "uptime": {"boot_time": "x", "uptime": "1:00:00"}, "battery": None,
    })
    out_file = tmp_path / "report.json"

    sysdiag.main(["--system", "--json", "--output", str(out_file)])
    capsys.readouterr()

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert "system" in data


def test_main_output_html_extension_writes_self_contained_html(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [{"device": "C:\\", "mountpoint": "C:\\", "total_gb": 100.0,
                   "used_gb": 50.0, "free_gb": 50.0, "percent": 50.0}],
        "memory": {"total_gb": 16.0, "used_gb": 6.4, "available_gb": 9.6, "percent": 40.0},
        "cpu_percent": 5.0, "uptime": {"boot_time": "x", "uptime": "1:00:00"}, "battery": None,
    })
    out_file = tmp_path / "report.html"

    sysdiag.main(["--system", "--output", str(out_file)])
    capsys.readouterr()

    content = out_file.read_text(encoding="utf-8")
    assert content.startswith("<!DOCTYPE html>")
    assert "System Health" in content
    assert "cdnjs.cloudflare.com" not in content  # self-contained, no external resources


def test_write_output_file_handles_error_gracefully(capsys):
    sysdiag.write_output_file(".", "content")  # a directory path can't be opened for writing
    assert "Warning" in capsys.readouterr().err


# ---- firewall status (mocked subprocess) ----

def test_get_firewall_status_windows_all_enabled(monkeypatch):
    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Windows")
    output = "State                                 ON\n" * 3
    monkeypatch.setattr(sysdiag.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": output})())

    result = sysdiag.get_firewall_status()
    assert result == {"available": True, "enabled": True, "detail": "3/3 profiles active"}


def test_get_firewall_status_windows_one_disabled(monkeypatch):
    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Windows")
    output = "State                                 ON\nState                                 OFF\nState                                 ON\n"
    monkeypatch.setattr(sysdiag.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": output})())

    result = sysdiag.get_firewall_status()
    assert result["enabled"] is False


def test_get_firewall_status_linux_active(monkeypatch):
    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sysdiag.subprocess, "run",
                         lambda *a, **k: type("R", (), {"stdout": "Status: active\n"})())

    result = sysdiag.get_firewall_status()
    assert result == {"available": True, "enabled": True, "detail": "Status: active"}


def test_get_firewall_status_handles_missing_command(monkeypatch):
    def raise_error(*a, **k):
        raise FileNotFoundError("ufw not found")

    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sysdiag.subprocess, "run", raise_error)

    result = sysdiag.get_firewall_status()
    assert result["available"] is False


# ---- recent system errors (mocked subprocess) ----

def test_summarize_windows_event_block():
    block = (
        "  Log Name: System\n"
        "  Source: Service Control Manager\n"
        "  Date: 2026-08-16T10:01:07.0730000Z\n"
        "  Event ID: 7011\n"
        "  Level: Error \n"
        "  Description: \n"
        "A timeout was reached while waiting for the WSearch service.\n"
    )
    summary = sysdiag._summarize_windows_event_block(block)
    assert "Service Control Manager" in summary
    assert "7011" in summary
    assert "WSearch" in summary


def test_get_recent_system_errors_windows(monkeypatch):
    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Windows")
    block = "  Source: Test Source\n  Date: 2026-01-01\n  Event ID: 1\n  Description: \nSomething broke.\n"
    monkeypatch.setattr(sysdiag.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": block})())

    result = sysdiag.get_recent_system_errors(max_events=5)
    assert result["available"] is True
    assert len(result["events"]) == 1
    assert "Something broke" in result["events"][0]


def test_get_recent_system_errors_linux(monkeypatch):
    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Linux")
    output = "Jan 01 00:00:00 host proc[1]: error one\nJan 01 00:00:01 host proc[2]: error two\n"
    monkeypatch.setattr(sysdiag.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": output})())

    result = sysdiag.get_recent_system_errors(max_events=5)
    assert result["available"] is True
    assert len(result["events"]) == 2


def test_get_recent_system_errors_none_found(monkeypatch):
    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sysdiag.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": ""})())

    result = sysdiag.get_recent_system_errors()
    assert result == {"available": True, "events": [], "error": None}


def test_get_recent_system_errors_handles_command_failure(monkeypatch):
    def raise_error(*a, **k):
        raise FileNotFoundError("journalctl not found")

    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sysdiag.subprocess, "run", raise_error)

    result = sysdiag.get_recent_system_errors()
    assert result["available"] is False


# ---- firewall / events wired into find_problems and suggest_fixes ----

def test_find_problems_flags_disabled_firewall():
    results = {"system": {"memory": {"percent": 10.0}, "disk": [],
                           "firewall": {"available": True, "enabled": False, "detail": "x"}}}
    problems = sysdiag.find_problems(results)
    assert any("Firewall is disabled" in p for p in problems)


def test_find_problems_ignores_unavailable_firewall_check():
    results = {"system": {"memory": {"percent": 10.0}, "disk": [],
                           "firewall": {"available": False, "enabled": None, "detail": "x"}}}
    assert sysdiag.find_problems(results) == []


def test_find_problems_flags_recent_events():
    results = {"events": {"available": True, "events": ["err1", "err2"], "error": None}}
    problems = sysdiag.find_problems(results)
    assert any("2 recent system error" in p for p in problems)


def test_suggest_fixes_disabled_firewall_windows(monkeypatch):
    monkeypatch.setattr(sysdiag.platform, "system", lambda: "Windows")
    results = {"system": {"memory": {"percent": 10.0}, "disk": [],
                           "firewall": {"available": True, "enabled": False, "detail": "x"}}}
    fixes = sysdiag.suggest_fixes(results)
    assert len(fixes) == 1
    assert "netsh advfirewall set allprofiles state on" in fixes[0]["suggestion"]


def test_suggest_fixes_recent_events():
    results = {"events": {"available": True, "events": ["err1"], "error": None}}
    fixes = sysdiag.suggest_fixes(results)
    assert len(fixes) == 1
    assert fixes[0]["problem"] == "Recent system errors found"


# ---- events opt-in behavior, and report formatting via main() ----

def test_main_default_run_excludes_events(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [], "memory": {"percent": 10.0}, "cpu_percent": 1.0,
        "uptime": {"boot_time": "x", "uptime": "1:00:00"}, "battery": None,
        "firewall": {"available": True, "enabled": True, "detail": "x"},
    })

    sysdiag.main(["--system", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert "events" not in output


def test_main_events_flag_runs_event_check(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "get_recent_system_errors",
                         lambda max_events: {"available": True, "events": [], "error": None})

    sysdiag.main(["--events", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert "events" in output


def test_print_event_report_via_main_shows_events(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "get_recent_system_errors",
                         lambda max_events: {"available": True, "events": ["disk failure at 10:00"], "error": None})

    sysdiag.main(["--events"])
    out = capsys.readouterr().out
    assert "== Recent System Errors ==" in out
    assert "disk failure at 10:00" in out


# ---- report formatting (via main(), report mode, mocked checks) ----

def test_print_network_report_healthy(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "run_network_checks", lambda host: {
        "host": host,
        "ping": {"reachable": True, "avg_ms": 12.0},
        "dns": {"success": True, "ip": "1.2.3.4"},
        "dns_public": {"resolver": "8.8.8.8", "success": True, "ip": "1.2.3.4", "error": None},
        "local_ip": "192.168.1.2", "public_ip": "203.0.113.9", "gateway": "192.168.1.1",
        "interfaces": [
            {"name": "eth0", "ip": "192.168.1.2", "is_up": True},
            {"name": "eth1", "ip": None, "is_up": False},
        ],
        "traceroute": ["1  1 ms  192.168.1.1"],
    })

    sysdiag.main(["--network"])
    out = capsys.readouterr().out

    assert "reachable, avg 12.0ms" in out
    assert "DNS resolution (" in out and "1.2.3.4" in out
    assert "DNS via 8.8.8.8: 1.2.3.4" in out
    assert "Public IP: 203.0.113.9" in out
    assert "eth0: 192.168.1.2 (up)" in out
    assert "eth1: no IPv4 (down)" in out
    assert "1  1 ms  192.168.1.1" in out


def test_print_network_report_failures(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "run_network_checks", lambda host: {
        "host": host,
        "ping": {"reachable": False, "avg_ms": None},
        "dns": {"success": False, "ip": None, "error": "timed out"},
        "dns_public": {"resolver": "8.8.8.8", "success": False, "ip": None, "error": "timed out"},
        "local_ip": None, "public_ip": None, "gateway": None,
        "interfaces": [], "traceroute": [],
    })

    sysdiag.main(["--network"])
    out = capsys.readouterr().out

    assert "UNREACHABLE" in out
    assert "DNS resolution (" in out and "FAILED" in out
    assert "DNS via 8.8.8.8: FAILED" in out
    assert "Local IP: unknown" in out
    assert "Public IP: unknown" in out


def test_print_process_report_via_main(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "get_top_processes", lambda top_n: {
        "by_cpu": [{"pid": 111, "name": "python.exe", "cpu_percent": 42.5, "memory_percent": 1.0}],
        "by_memory": [{"pid": 222, "name": "chrome.exe", "cpu_percent": 1.0, "memory_percent": 8.3}],
    })

    sysdiag.main(["--processes"])
    out = capsys.readouterr().out

    assert "By CPU:" in out
    assert "111" in out and "python.exe" in out
    assert "By Memory:" in out
    assert "222" in out and "chrome.exe" in out


def test_print_port_report_via_main(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "check_port", lambda host, port: port == 443)

    sysdiag.main(["--ports", "--port-list", "80,443"])
    out = capsys.readouterr().out

    assert "80" in out and "closed" in out
    assert "443" in out and "OPEN" in out


# ---- JSON output end-to-end (checks mocked out, no real system access) ----

def test_main_json_output_is_valid_json(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "run_network_checks", lambda host: {
        "host": host, "ping": {"reachable": True, "avg_ms": 1.0},
        "dns": {"success": True, "ip": "1.2.3.4"},
        "local_ip": "192.168.1.2", "gateway": "192.168.1.1", "traceroute": [],
    })
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [{"device": "C:\\", "percent": 50.0}],
        "memory": {"percent": 40.0}, "cpu_percent": 5.0,
        "uptime": {"boot_time": "x", "uptime": "1:00:00"},
    })
    monkeypatch.setattr(sysdiag, "get_top_processes", lambda top_n: {"by_cpu": [], "by_memory": []})

    exit_code = sysdiag.main(["--json"])
    assert exit_code == 0

    output = json.loads(capsys.readouterr().out)
    assert set(output.keys()) == {"network", "system", "processes", "problems", "suggested_fixes"}
    assert output["problems"] == []


def test_main_json_output_flags_high_disk(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [{"device": "C:\\", "percent": 95.0}],
        "memory": {"percent": 40.0}, "cpu_percent": 5.0,
        "uptime": {"boot_time": "x", "uptime": "1:00:00"},
    })

    exit_code = sysdiag.main(["--system", "--json"])

    output = json.loads(capsys.readouterr().out)
    assert len(output["problems"]) == 1
    assert "C:\\" in output["problems"][0]
    assert exit_code == 1


def test_main_exit_code_zero_when_no_problems(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [{"device": "C:\\", "percent": 50.0}],
        "memory": {"percent": 40.0}, "cpu_percent": 5.0,
        "uptime": {"boot_time": "x", "uptime": "1:00:00"},
    })

    exit_code = sysdiag.main(["--system", "--json"])
    capsys.readouterr()
    assert exit_code == 0


def test_main_quiet_json_only_shows_problems(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [{"device": "C:\\", "percent": 95.0}],
        "memory": {"percent": 40.0}, "cpu_percent": 5.0,
        "uptime": {"boot_time": "x", "uptime": "1:00:00"},
    })

    sysdiag.main(["--system", "--json", "--quiet"])

    output = json.loads(capsys.readouterr().out)
    assert set(output.keys()) == {"problems", "suggested_fixes"}


def test_main_quiet_report_skips_section_headers(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [{"device": "C:\\", "percent": 50.0}],
        "memory": {"percent": 40.0}, "cpu_percent": 5.0,
        "uptime": {"boot_time": "x", "uptime": "1:00:00"},
    })

    sysdiag.main(["--system", "--quiet"])

    out = capsys.readouterr().out
    assert "== System Health ==" not in out
    assert "== Problems ==" in out


def test_main_custom_thresholds_from_cli(monkeypatch, capsys):
    monkeypatch.setattr(sysdiag, "run_system_checks", lambda: {
        "disk": [{"device": "C:\\", "percent": 50.0}],
        "memory": {"percent": 60.0}, "cpu_percent": 5.0,
        "uptime": {"boot_time": "x", "uptime": "1:00:00"},
    })

    sysdiag.main(["--system", "--json", "--mem-threshold", "50"])

    output = json.loads(capsys.readouterr().out)
    assert len(output["problems"]) == 1
    assert "Memory" in output["problems"][0]
