from crawl.client_capture_health import evaluate_health


def test_evaluate_health_requires_window_fiddler_listener_and_owned_rules():
    result = evaluate_health(
        window_ok=True,
        fiddler_process_ok=True,
        listener_ok=True,
        proxy_ok=True,
        rules_owned=True,
        capture_control={"enabled": False, "diagnostic_enabled": False},
    )
    assert result["ok"] is True
    assert all(check["status"] == "pass" for check in result["checks"])


def test_evaluate_health_does_not_call_an_enabled_capture_healthy():
    result = evaluate_health(
        window_ok=True,
        fiddler_process_ok=True,
        listener_ok=True,
        proxy_ok=True,
        rules_owned=True,
        capture_control={"enabled": True, "diagnostic_enabled": False},
    )
    assert result["ok"] is False
    assert result["checks"][-1]["name"] == "capture_disabled"


def test_evaluate_health_rejects_listener_when_system_proxy_bypasses_fiddler():
    result = evaluate_health(
        window_ok=True,
        fiddler_process_ok=True,
        listener_ok=True,
        proxy_ok=False,
        rules_owned=True,
        capture_control={"enabled": False, "diagnostic_enabled": False},
    )
    assert result["ok"] is False
    assert {item["name"]: item["status"] for item in result["checks"]}["fiddler_system_proxy"] == "fail"


def test_proxy_route_parser_requires_http_and_https_to_fiddler_listener():
    from crawl.client_capture_health import _proxy_routes_to_listener

    assert _proxy_routes_to_listener(
        1,
        "http=127.0.0.1:8899;https=127.0.0.1:8899;ftp=127.0.0.1:7897",
        "127.0.0.1",
        8899,
    ) is True
    assert _proxy_routes_to_listener(1, "127.0.0.1:8899", "127.0.0.1", 8899) is True
    assert _proxy_routes_to_listener(1, "127.0.0.1:7897", "127.0.0.1", 8899) is False
    assert _proxy_routes_to_listener(
        1,
        "http=127.0.0.1:8899;https=127.0.0.1:7897",
        "127.0.0.1",
        8899,
    ) is False
