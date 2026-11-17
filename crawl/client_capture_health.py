"""Read-only health checks for the WeChat/Fiddler capture boundary."""

from __future__ import annotations


def evaluate_health(
    *,
    window_ok: bool,
    fiddler_process_ok: bool,
    listener_ok: bool,
    proxy_ok: bool,
    rules_owned: bool,
    capture_control: dict,
) -> dict:
    """Evaluate already-observed health facts without changing system state."""
    control = capture_control if isinstance(capture_control, dict) else {}
    checks = [
        _check("wechat_window", bool(window_ok)),
        _check("fiddler_process", bool(fiddler_process_ok)),
        _check("fiddler_listener", bool(listener_ok)),
        _check("fiddler_system_proxy", bool(proxy_ok)),
        _check("rules_owned", bool(rules_owned)),
        _check(
            "capture_disabled",
            control.get("enabled") is False and control.get("diagnostic_enabled") is False,
        ),
    ]
    return {"ok": all(item["status"] == "pass" for item in checks), "checks": checks}


def _check(name: str, passed: bool) -> dict:
    return {"name": name, "status": "pass" if passed else "fail"}


def _proxy_routes_to_listener(proxy_enable, proxy_server, host: str, port: int) -> bool:
    if proxy_enable != 1 or not isinstance(proxy_server, str):
        return False
    accepted = {f"{host}:{int(port)}".lower()}
    if host in {"127.0.0.1", "localhost"}:
        accepted.update({f"127.0.0.1:{int(port)}", f"localhost:{int(port)}"})
    parts = [part.strip().lower() for part in proxy_server.split(";") if part.strip()]
    if not parts:
        return False
    if all("=" not in part for part in parts):
        return len(parts) == 1 and parts[0] in accepted
    routed = {}
    for part in parts:
        if "=" not in part:
            continue
        scheme, endpoint = part.split("=", 1)
        routed[scheme.strip()] = endpoint.strip()
    return routed.get("http") in accepted and routed.get("https") in accepted


def check_capture_health(
    *,
    hwnd: int,
    fiddler_exe,
    active_rules,
    activation_metadata,
    control_path,
    listen_host: str = "127.0.0.1",
    listen_port: int = 8899,
) -> dict:
    """Perform host checks used by the CLI; no proxy or credential changes."""
    import hashlib
    import json
    from pathlib import Path
    import socket
    import subprocess

    from crawl.desktop_wechat import DesktopWeChatDriver

    window_ok = True
    window_detail = ""
    try:
        driver = DesktopWeChatDriver(int(hwnd))
        window_detail = f"pid={driver.pid}"
    except Exception as exc:
        window_ok = False
        window_detail = type(exc).__name__

    fiddler_exe = Path(fiddler_exe)
    process_ok = False
    try:
        output = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Fiddler.exe"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        process_ok = "fiddler.exe" in output.lower()
    except (OSError, subprocess.SubprocessError):
        process_ok = False
    process_ok = process_ok and fiddler_exe.exists()

    listener_ok = False
    try:
        with socket.create_connection((listen_host, int(listen_port)), timeout=1):
            listener_ok = True
    except OSError:
        listener_ok = False

    proxy_enable = None
    proxy_server = None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            proxy_enable = winreg.QueryValueEx(key, "ProxyEnable")[0]
            proxy_server = winreg.QueryValueEx(key, "ProxyServer")[0]
    except (OSError, ImportError):
        pass
    proxy_ok = _proxy_routes_to_listener(proxy_enable, proxy_server, listen_host, int(listen_port))

    active_rules = Path(active_rules)
    activation_metadata = Path(activation_metadata)
    rules_owned = False
    try:
        metadata = json.loads(activation_metadata.read_text(encoding="utf-8"))
        expected = str(metadata.get("installed_sha256") or "")
        rules_owned = (
            active_rules.exists()
            and str(active_rules) == str(metadata.get("active_path") or "")
            and bool(expected)
            and hashlib.sha256(active_rules.read_bytes()).hexdigest() == expected
        )
    except (OSError, ValueError, TypeError):
        rules_owned = False

    try:
        control = json.loads(Path(control_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        control = {}
    result = evaluate_health(
        window_ok=window_ok,
        fiddler_process_ok=process_ok,
        listener_ok=listener_ok,
        proxy_ok=proxy_ok,
        rules_owned=rules_owned,
        capture_control=control,
    )
    result["details"] = {
        "wechat_window": window_detail,
        "fiddler_exe_exists": fiddler_exe.exists(),
        "listen_host": listen_host,
        "listen_port": int(listen_port),
        "system_proxy_enabled": proxy_enable == 1,
        "system_proxy_server": proxy_server,
        "control_enabled": control.get("enabled"),
        "diagnostic_enabled": control.get("diagnostic_enabled"),
    }
    return result
