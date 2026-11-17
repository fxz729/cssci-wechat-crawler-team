"""Synthetic-only contract tests; never arm a production directory."""
import importlib
import json
import os
from pathlib import Path
import uuid
from urllib.parse import unquote, urlsplit

import pytest

from crawl.profile_session_probe import HomeRequest, read_home_request

BIZ = "TEST_BIZ=="
URL = "https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=TEST_BIZ%3D%3D&key=TEST_KEY&uin=TEST_UIN&pass_ticket=TEST_TICKET"
RAW = f"GET {URL} HTTP/1.1\r\nUser-Agent: TEST_UA\r\nCookie: wap_sid2=TEST_COOKIE\r\n\r\n"


def module():
    try:
        return importlib.import_module("crawl.journal_session_capture")
    except ModuleNotFoundError:
        pytest.fail("journal_session_capture module is not implemented")


@pytest.fixture
def rig(tmp_path):
    now = [1000.0]
    checked = []
    parsed = []

    def verify(path):
        checked.append(Path(path))
        return True

    def parser(text, *, expected_biz):
        parsed.append(text)
        result = read_home_request(text)
        assert result.biz == expected_biz
        return result

    broker = module().SessionCaptureBroker(tmp_path, clock=lambda: now[0],
        permission_verifier=verify, request_parser=parser)
    return broker, tmp_path, now, checked, parsed


def event(round_, **changes):
    value = dict(schema=1, event_id=uuid.uuid4().hex, kind="home",
        run_id=round_.run_id, generation=round_.generation, target_biz=round_.target_biz,
        started_at=1001.0, captured_at=1002.0, process_name="wechatappex", process_id=123,
        session_id="1", request_text=RAW, referer="", sessionparams="", response_set_cookie=[])
    value.update(changes)
    if "sessionparams" not in changes:
        url = value["request_text"].split(" ")[1] if " " in value["request_text"] else ""
        value["sessionparams"] = {unquote(k): unquote(v) for part in urlsplit(url).query.split("&")
            for k, sep, v in [part.partition("=")] if sep and k in {"uin", "key", "pass_ticket", "appmsg_token", "wxtoken", "sessionparams"}}
    return value


def drop(root, value):
    path = root / (value["event_id"] + ".event.json")
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_single_home_returns_immediately_with_real_headers_and_no_second_event(rig, capsys):
    broker, root, now, checked, parsed = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    path = drop(root, event(round_))
    result = broker.consume()
    assert isinstance(result, HomeRequest)
    assert result.headers["User-Agent"] == "TEST_UA"
    assert result.headers["Cookie"] == "wap_sid2=TEST_COOKIE"
    assert parsed == [RAW]
    assert path in checked
    assert broker.consume() is None
    assert capsys.readouterr() == ("", "")


def test_control_round_lifecycle_has_no_credentials_and_checks_temp_final(rig):
    broker, root, now, checked, _ = rig
    first = broker.arm(BIZ)
    second = broker.arm(BIZ)
    assert second.generation > first.generation and second.run_id != first.run_id
    control = json.loads((root / "control.json").read_text())
    assert set(control) == {"schema", "enabled", "run_id", "generation", "target_biz", "armed_at", "deadline"}
    assert any(p.name.endswith(".tmp") for p in checked)
    assert root / "control.json" in checked
    broker.disarm()
    assert not json.loads((root / "control.json").read_text())["enabled"]
    assert broker.consume() is None


@pytest.mark.parametrize("changes", [
    {"run_id": "0" * 32}, {"generation": 0}, {"target_biz": "OTHER"},
    {"started_at": 999}, {"captured_at": 999}, {"captured_at": 9999},
    {"process_name": "fiddler"}, {"process_name": "wechat"}, {"process_id": 0},
    {"process_id": True}, {"request_text": RAW.replace("TEST_BIZ%3D%3D", "OTHER")},
    {"request_text": RAW.replace("action=home", "action=getmsg")},
    {"request_text": RAW.replace("mp.weixin.qq.com", "mp.weixin.qq.com.evil")},
    {"request_text": RAW.replace("User-Agent: TEST_UA\r\n", "")},
    {"request_text": RAW.replace("Cookie: wap_sid2=TEST_COOKIE\r\n", "")},
    {"request_text": RAW.replace("Cookie:", "Proxy-Authorization: TEST\r\nCookie:")},
    {"request_text": "<html>not JSON</html>"}, {"kind": "urlcheck"},
])
def test_rejects_unrelated_stale_background_and_incomplete_requests(rig, changes):
    broker, root, now, _, parsed = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, event(round_, **changes))
    assert broker.consume() is None
    assert not parsed


def test_expiry_and_disarm_reject_late_arrivals(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    drop(root, event(round_))
    now[0] = round_.deadline
    assert broker.consume() is None
    broker.disarm()
    assert broker.consume() is None


def test_duplicate_session_and_event_are_never_delivered_twice(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    one = event(round_)
    drop(root, one)
    assert broker.consume()
    drop(root, one)
    drop(root, event(round_))
    assert broker.consume() is None


def test_home_html_is_ignored_not_decoded_as_json(rig):
    broker, root, now, _, parsed = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, event(round_, response_body="<html>TEST_SECRET</html>"))
    assert broker.consume() is not None
    assert parsed == [RAW]


def test_parser_failures_are_redacted(rig, capsys):
    broker, root, now, _, _ = rig
    def fail(*args, **kwargs):
        raise ValueError("TEST_SECRET")
    broker.request_parser = fail
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, event(round_))
    assert broker.consume() is None
    assert capsys.readouterr() == ("", "")


def test_permission_failure_on_temp_prevents_sensitive_write(tmp_path):
    m = module()
    checked = []
    def verify(path):
        checked.append(path)
        return not path.name.endswith(".tmp")
    broker = m.SessionCaptureBroker(tmp_path, permission_verifier=verify)
    with pytest.raises(m.CaptureError, match="^permission_denied$"):
        broker.arm(BIZ)
    assert not (tmp_path / "control.json").exists()
    assert all(not p.read_bytes() for p in tmp_path.glob("*.tmp"))


def test_permission_failure_event_not_parsed(rig):
    broker, root, now, _, parsed = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    path = drop(root, event(round_))
    broker.permission_verifier = lambda p: p != path
    with pytest.raises(module().CaptureError, match="^permission_denied$"):
        broker.consume()
    assert not parsed


def test_missing_root_is_not_created(tmp_path):
    m = module()
    root = tmp_path / "missing"
    with pytest.raises(m.CaptureError, match="^private_root_missing$"):
        m.SessionCaptureBroker(root, permission_verifier=lambda p: True)
    assert not root.exists()


def test_non_windows_default_fails_closed(tmp_path, monkeypatch):
    m = module()
    monkeypatch.setattr(m, "_WINDOWS", False)
    with pytest.raises(m.CaptureError, match="^acl_unavailable$"):
        m.SessionCaptureBroker(tmp_path)


def test_symlink_rejected_even_with_injected_permissions(tmp_path):
    m = module()
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(m.CaptureError, match="^reparse_path$"):
        m.SessionCaptureBroker(link, permission_verifier=lambda p: True)


def test_candidate_is_only_text_and_never_installs(tmp_path):
    script = module().prepare_candidate(tmp_path / "not-created")
    assert "class CssciSessionCapture" in script
    assert "CssciSessionCapture.Record(s)" in script
    assert "__CSSCI_SESSION_ROOT__" not in script
    assert not list(tmp_path.iterdir())


def test_fiddler_source_is_data_only_and_checks_acl_before_and_after_publish():
    script = (Path(__file__).parents[1] / "capture" / "fiddler_session.js")
    assert script.exists(), "Fiddler session helper is not implemented"
    text = script.read_text(encoding="utf-8")
    for required in ("class CssciSessionCapture", "Record(s: Session)", "ClientBeginRequest",
                     "GetAccessControl", "ReparsePoint", "CreateNew", "File.Move", "Set-Cookie",
                     "User-Agent", "Cookie", "urlcheck", "wechatappex", "generation", "deadline"):
        assert required in text
    for forbidden in ("FiddlerApplication.Log", "Console.Write", "getResponseBodyAsString", "oResponse.body",
                      "Process.Start", "SetAccessControl", "CustomRules.js"):
        assert forbidden not in text

def test_native_fiddler_helper_compiles_without_loading_or_installing_rules(tmp_path):
    import subprocess
    m = module()
    compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework/v4.0.30319/jsc.exe"
    fiddler = Path(__file__).parents[2] / "Fiddler/Fiddler.exe"
    if not compiler.exists() or not fiddler.exists():
        pytest.skip("local JScript.NET compiler/Fiddler reference unavailable")
    source = Path(__file__).parents[1] / "capture/fiddler_session.js"
    result = subprocess.run([str(compiler), "/nologo", "/target:library", "/utf8output",
        f"/out:{tmp_path / 'session.dll'}", f"/reference:{fiddler}", str(source)],
        capture_output=True, timeout=30)
    assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", errors="replace")
    assert b"warning JS" not in result.stdout, result.stdout.decode("utf-8", errors="replace")
    assert (tmp_path / "session.dll").exists()


def test_actual_parser_contract_when_available(rig):
    pytest.importorskip("config.journal_request_input")
    broker, root, now, _, _ = rig
    broker.request_parser = module()._default_parser
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, event(round_))
    result = broker.consume()
    assert isinstance(result, HomeRequest)
    assert result.headers["User-Agent"] == "TEST_UA"
    assert result.headers["Cookie"] == "wap_sid2=TEST_COOKIE"


def urlcheck(round_, **changes):
    url = "https://mp.weixin.qq.com/mp/urlcheck?scene=1"
    text = f"GET {url} HTTP/1.1\r\nUser-Agent: TEST_UA\r\nCookie: wap_sid2=TEST_COOKIE\r\nReferer: {URL}\r\n\r\n"
    return event(round_, kind="urlcheck", request_text=text, referer=URL, session_id="2",
                 response_set_cookie=["wap_sid2=TEST_UPDATED; Path=/; Secure; HttpOnly"], **changes)


def test_urlcheck_without_biz_matches_same_process_referer_only_if_already_available(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, event(round_))
    drop(root, urlcheck(round_))
    assert broker.consume().headers["Cookie"] == "wap_sid2=TEST_UPDATED"


@pytest.mark.parametrize("variant", ["other_process", "wrong_ref", "ambiguous", "early", "wrong_biz"])
def test_optional_urlcheck_is_dropped_without_blocking_home(rig, variant):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    check = urlcheck(round_)
    if variant == "other_process":
        check["process_id"] += 1
    elif variant == "wrong_ref":
        check["referer"] = URL.replace("TEST_BIZ", "OTHER")
        check["request_text"] = check["request_text"].replace(URL, check["referer"])
    elif variant == "wrong_biz":
        check["request_text"] = check["request_text"].replace("scene=1", "__biz=OTHER")
    elif variant == "ambiguous":
        drop(root, urlcheck(round_))
    if variant == "early":
        drop(root, check)
        assert broker.consume() is None
    else:
        drop(root, check)
    drop(root, event(round_))
    expected = "wap_sid2=TEST_UPDATED" if variant == "early" else "wap_sid2=TEST_COOKIE"
    assert broker.consume().headers["Cookie"] == expected


def test_cookie_updates_scoped_allowlisted_and_original_event_unchanged(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    row = event(round_, response_set_cookie=[
        "wap_sid2=TEST_UPDATED; Path=/; Domain=.weixin.qq.com; Secure",
        "appmsg_token=TEST_BAD; Domain=evil.example", "tracking=TEST_NO; Path=/",
        "wxuin=TEST_BAD; Path=/other", "wxtokenkey=TEST_INJECT\r\nEvil: 1"])
    path = drop(root, row)
    assert broker.consume().headers["Cookie"] == "wap_sid2=TEST_UPDATED"
    assert row["request_text"] == RAW
    assert not path.exists()


def test_control_conflict_cannot_disarm_another_owner_round(rig):
    broker, root, now, _, _ = rig
    broker.arm(BIZ)
    other = module().SessionCaptureBroker(root, clock=lambda: now[0], permission_verifier=lambda p: True)
    active = other.arm(BIZ)
    with pytest.raises(module().CaptureError, match="^control_conflict$"):
        broker.disarm()
    assert json.loads((root / "control.json").read_text())["run_id"] == active.run_id


def test_states_waiting_rejected_expired_idle(rig):
    broker, root, now, _, _ = rig
    assert broker.status == "idle"
    round_ = broker.arm(BIZ)
    assert broker.consume() is None and broker.status == "waiting"
    drop(root, event(round_, target_biz="OTHER"))
    now[0] = 1003
    assert broker.consume() is None and broker.last_error == "event_rejected"
    now[0] = round_.deadline
    assert broker.consume() is None and broker.status == "expired"
    broker.disarm()
    broker.disarm()
    assert broker.status == "idle" and broker.last_error is None


def test_permission_exception_text_cannot_escape(tmp_path, capsys):
    def broken(path):
        raise RuntimeError("TEST_SECRET")
    with pytest.raises(module().CaptureError, match="^permission_denied$") as caught:
        module().SessionCaptureBroker(tmp_path, permission_verifier=broken)
    assert caught.value.__context__ is None
    assert capsys.readouterr() == ("", "")


def test_status_error_on_acl_failure(rig):
    broker, root, now, _, _ = rig
    broker.arm(BIZ)
    broker.permission_verifier = lambda p: False
    with pytest.raises(module().CaptureError):
        broker.consume()
    assert broker.status == "error" and broker.last_error == "permission_denied"


def test_request_literal_plus_agrees_with_parser(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    home = event(round_, request_text=RAW.replace(" HTTP/1.1", "&sessionparams=TEST+TOKEN HTTP/1.1"), sessionparams={"uin": "TEST_UIN", "key": "TEST_KEY", "pass_ticket": "TEST_TICKET", "sessionparams": "TEST+TOKEN"})
    drop(root, home)
    assert broker.consume() is not None


def test_reparse_root_checked_before_missing_target_error(tmp_path, monkeypatch):
    m = module()
    root = tmp_path / "ordinary"
    root.mkdir()
    original = Path.lstat
    class ReparseInfo:
        st_mode = 0o040700
        st_file_attributes = 0x400
    monkeypatch.setattr(Path, "lstat", lambda p: ReparseInfo() if p == root else original(p))
    with pytest.raises(m.CaptureError, match="^reparse_path$"):
        m.SessionCaptureBroker(root, permission_verifier=lambda p: True)

@pytest.mark.parametrize("changes", [{"schema": True}, {"generation": True}, {"response_set_cookie": None}])
def test_malformed_event_types_are_rejected_with_codes_not_exceptions(rig, changes):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, event(round_, **changes))
    assert broker.consume() is None
    assert broker.last_error == "event_rejected"


def test_duplicate_session_with_conflicting_content_is_ambiguous(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, event(round_))
    drop(root, event(round_, request_text=RAW.replace("TEST_COOKIE", "TEST_OTHER")))
    assert broker.consume() is None


def test_urlcheck_session_fields_without_referer_can_correlate(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, event(round_))
    check = urlcheck(round_)
    check["referer"] = ""
    check["request_text"] = check["request_text"].replace(f"Referer: {URL}\r\n", "").replace("scene=1", "uin=TEST_UIN&key=TEST_KEY")
    check["sessionparams"] = {"uin": "TEST_UIN", "key": "TEST_KEY"}
    drop(root, check)
    assert broker.consume().headers["Cookie"] == "wap_sid2=TEST_UPDATED"


def test_urlcheck_conflicting_session_fields_override_matching_referer(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, event(round_))
    check = urlcheck(round_)
    check["request_text"] = check["request_text"].replace("scene=1", "uin=TEST_OTHER&key=TEST_KEY")
    check["sessionparams"] = {"uin": "TEST_OTHER", "key": "TEST_KEY"}
    drop(root, check)
    assert broker.consume().headers["Cookie"] == "wap_sid2=TEST_COOKIE"


def test_urlcheck_conflicting_request_headers_dropped(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, event(round_))
    check = urlcheck(round_)
    check["request_text"] = check["request_text"].replace("TEST_COOKIE", "TEST_OTHER")
    drop(root, check)
    assert broker.consume().headers["Cookie"] == "wap_sid2=TEST_COOKIE"

@pytest.mark.parametrize("case,allowed", [
    ("owner_only", True), ("system_admin", True), ("ordinary_read", False),
    ("ordinary_write", False), ("null_dacl", False), ("untrusted_owner", False),
    ("parent_delete_child", False), ("parent_create_only", True), ("reparse", False),
    ("ancestor_inherit_only", True), ("leaf_inherit_only", False),
    ("ancestor_trustedinstaller", True), ("leaf_trustedinstaller", False),
])
def test_windows_acl_semantics_with_in_memory_descriptors_only(tmp_path, case, allowed):
    """No Set-Acl/filesystem ACL changes; .NET security descriptors are in memory."""
    import base64
    import subprocess
    m = module()
    if os.name != "nt":
        pytest.skip("Windows ACL runtime unavailable")
    # Fake metadata accessors, real .NET SID/DACL parser and production policy.
    setup = r'''
$case='__CASE__'
$script:aclCalls=0
function Get-Item { param($LiteralPath,[switch]$Force)
  $attr=[IO.FileAttributes]::Directory
  if($case -eq 'reparse') { $attr=$attr -bor [IO.FileAttributes]::ReparsePoint }
  return [pscustomobject]@{Attributes=$attr}
}
function Get-Acl { param($LiteralPath)
  $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  $owner=$sid
  if($case -eq 'untrusted_owner') { $owner='S-1-5-32-545' }
  if($case -eq 'ancestor_trustedinstaller' -and $script:aclCalls -gt 0 -or $case -eq 'leaf_trustedinstaller') { $owner='S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464' }
  $dacl="D:P(A;;FA;;;$sid)"
  if($case -eq 'ancestor_inherit_only' -and $script:aclCalls -gt 0 -or $case -eq 'leaf_inherit_only') { $dacl+='(A;OICIIO;SDGXGWGR;;;AU)' }
  if($case -eq 'system_admin') { $dacl+='(A;;FA;;;SY)(A;;FA;;;BA)' }
  if($case -eq 'ordinary_read') { $dacl+='(A;;FR;;;BU)' }
  if($case -eq 'ordinary_write') { $dacl+='(A;;FW;;;BU)' }
  if($case -eq 'parent_delete_child' -and $script:aclCalls -gt 0) { $dacl+='(A;;0x40;;;BU)' }
  if($case -eq 'parent_create_only' -and $script:aclCalls -gt 0) { $dacl+='(A;;0x4;;;BU)' }
  if($case -eq 'null_dacl') { $dacl='D:NO_ACCESS_CONTROL' }
  $script:aclCalls++
  $acl=New-Object Security.AccessControl.DirectorySecurity
  $acl.SetSecurityDescriptorSddlForm("O:${owner}G:${sid}${dacl}")
  return $acl
}
'''.replace("__CASE__", case)
    body = m._ACL_SCRIPT.replace("__PATH__", base64.b64encode(str(tmp_path).encode()).decode())
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
        base64.b64encode((setup + body).encode("utf-16le")).decode()],
        capture_output=True, timeout=20, creationflags=0x08000000)
    assert result.returncode == (0 if allowed else 2), "ACL policy or native descriptor inspection failed"
    assert (result.stdout == b"ACL_OK") == allowed
    assert not result.stderr


def test_atomic_event_publish_checks_empty_temp_and_final_acl_in_order():
    text = (Path(__file__).parents[1] / "capture/fiddler_session.js").read_text(encoding="utf-8")
    block = text.split("static function Publish", 1)[1].split("static function Record", 1)[0]
    assert block.index("Check(temporary)") < block.index("stream.Write")
    assert block.index("stream.Flush(true)") < block.index("File.Move") < block.index("Check(destination)")


def test_replaced_control_generation_is_rejected_before_parser(rig):
    broker, root, now, _, parsed = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, event(round_))
    control = json.loads(broker.control_path.read_text())
    control["generation"] += 1
    broker.control_path.write_text(json.dumps(control))
    with pytest.raises(module().CaptureError, match="^control_conflict$"):
        broker.consume()
    assert not parsed

@pytest.mark.parametrize("root", [r"\\server\share\session", "relative/session", None])
def test_invalid_root_rejected_before_filesystem_access(root, monkeypatch):
    m = module()
    def forbidden(path):
        pytest.fail("invalid/private-network path must not be probed")
    monkeypatch.setattr(Path, "is_dir", forbidden)
    with pytest.raises(m.CaptureError, match="^invalid_private_path$"):
        m.SessionCaptureBroker(root, permission_verifier=lambda p: True)


def test_slow_local_scan_stops_at_deadline_without_parsing(rig):
    broker, root, now, _, parsed = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, event(round_))
    def slow(path):
        if path.name.endswith(".event.json"):
            now[0] = round_.deadline
        return True
    broker.permission_verifier = slow
    assert broker.consume() is None
    assert broker.status == "expired"
    assert not parsed

# Cleanup/ack regression tests: every path below belongs to a pytest sandbox.
def test_consume_ack_removes_only_current_verified_events(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    home = drop(root, event(round_))
    check = drop(root, urlcheck(round_))
    foreign = drop(root, event(round_, run_id="f" * 32))
    wrong = drop(root, event(round_, target_biz="OTHER"))
    unknown = root / "operator-notes.txt"
    unknown.write_text("TEST_PRIVATE_NOTE")
    temporary = root / "unknown.tmp"
    temporary.write_text("TEST_UNKNOWN")
    before = {p: p.read_bytes() for p in (foreign, wrong, unknown, temporary)}
    assert broker.consume() is not None
    assert not home.exists() and not check.exists()
    assert all(p.read_bytes() == data for p, data in before.items())


def test_disarm_discovers_and_cleans_current_unconsumed_records_after_expiry(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    own = drop(root, event(round_))
    foreign = drop(root, event(round_, generation=round_.generation + 1))
    now[0] = round_.deadline + 1
    broker.disarm()
    assert not own.exists() and foreign.exists()
    broker.disarm()
    assert broker.status == "idle"


def test_300_rounds_ack_and_disarm_do_not_fill_inbox(rig):
    broker, root, now, _, _ = rig
    for n in range(300):
        round_ = broker.arm(BIZ)
        now[0] += 3
        drop(root, event(round_, started_at=round_.armed_at + 1, captured_at=round_.armed_at + 2))
        drop(root, urlcheck(round_, started_at=round_.armed_at + 1, captured_at=round_.armed_at + 2))
        if n % 2 == 0:
            assert broker.consume() is not None
        broker.disarm()
        assert not list(root.glob("*.event.json")), f"round {n} left credentials"


def test_rearm_cleans_previous_owned_round_before_new_control(rig):
    broker, root, now, _, _ = rig
    first = broker.arm(BIZ)
    now[0] = 1003
    own = drop(root, event(first))
    second = broker.arm(BIZ)
    assert second.generation > first.generation
    assert not own.exists()


def test_early_valid_urlcheck_is_buffered_across_polls_then_acknowledged(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    check = drop(root, urlcheck(round_))
    assert broker.consume() is None
    assert broker.consume() is None
    assert broker.status == "waiting"
    home = drop(root, event(round_))
    assert broker.consume().headers["Cookie"] == "wap_sid2=TEST_UPDATED"
    assert not home.exists() and not check.exists()


def test_buffer_is_not_carried_to_next_round(rig):
    broker, root, now, _, _ = rig
    old = broker.arm(BIZ)
    now[0] = 1003
    drop(root, urlcheck(old))
    assert broker.consume() is None
    broker.disarm()
    active = broker.arm(BIZ)
    now[0] = 1006
    drop(root, event(active, started_at=1004, captured_at=1005))
    assert broker.consume().headers["Cookie"] == "wap_sid2=TEST_COOKIE"
    assert not list(root.glob("*.event.json"))


@pytest.mark.parametrize("action", ["consume", "disarm"])
def test_ack_delete_failure_is_code_only_and_stops_rearming(rig, monkeypatch, action):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    own = drop(root, event(round_))
    def cannot_delete(path, *args, **kwargs):
        raise PermissionError("TEST_SECRET_PATH")
    monkeypatch.setattr(Path, "unlink", cannot_delete)
    with pytest.raises(module().CaptureError, match="^cleanup_failed$") as caught:
        getattr(broker, action)()
    assert caught.value.__context__ is None
    assert broker.status == "error" and broker.last_error == "cleanup_failed"
    assert own.exists()
    assert json.loads(broker.control_path.read_text())["enabled"] is False
    with pytest.raises(module().CaptureError, match="^cleanup_failed$"):
        broker.arm(BIZ)
    assert broker.consume() is None


def test_cleanup_refuses_changed_validated_file(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    row = urlcheck(round_)
    path = drop(root, row)
    assert broker.consume() is None
    row["run_id"] = "e" * 32
    path.write_text(json.dumps(row))
    with pytest.raises(module().CaptureError, match="^cleanup_identity_changed$"):
        broker.disarm()
    assert json.loads(path.read_text())["run_id"] == "e" * 32


def test_cleanup_refuses_same_bytes_replacement(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    path = drop(root, urlcheck(round_))
    assert broker.consume() is None
    replacement = root / "replacement.txt"
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)
    with pytest.raises(module().CaptureError, match="^cleanup_identity_changed$"):
        broker.disarm()
    assert path.exists()


def test_cleanup_rechecks_acl_and_reparse_before_exact_delete(rig, monkeypatch):
    broker, root, now, checked, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    path = drop(root, urlcheck(round_))
    assert broker.consume() is None
    original = Path.lstat
    class ReparseInfo:
        st_mode = 0o100600
        st_file_attributes = 0x400
    monkeypatch.setattr(Path, "lstat", lambda p: ReparseInfo() if p == path else original(p))
    with pytest.raises(module().CaptureError, match="^reparse_path$"):
        broker.disarm()
    assert path.exists()


@pytest.mark.parametrize("name", ["unknown.event.json", "unfinished.tmp"])
def test_restart_retains_unconfirmed_events_and_requires_explicit_review(rig, name):
    broker, root, now, _, _ = rig
    broker.arm(BIZ)
    orphan = root / name
    orphan.write_text("TEST_UNCONFIRMED")
    control = broker.control_path.read_bytes()
    restarted = module().SessionCaptureBroker(root, clock=lambda: now[0], permission_verifier=lambda p: True)
    with pytest.raises(module().CaptureError, match="^orphaned_capture_files$"):
        restarted.arm(BIZ)
    assert orphan.read_text() == "TEST_UNCONFIRMED"
    assert broker.control_path.read_bytes() == control


def test_buffer_has_fixed_bound_and_stops_safely(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    for n in range(65):
        row = urlcheck(round_)
        row["session_id"] = str(n + 2)
        drop(root, row)
    with pytest.raises(module().CaptureError, match="^capture_buffer_full$"):
        broker.consume()
    assert broker.status == "error"
    broker.disarm()
    assert not list(root.glob("*.event.json"))


def test_cleanup_permission_change_is_not_swallowed(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    path = drop(root, urlcheck(round_))
    assert broker.consume() is None
    broker.permission_verifier = lambda p: p != path
    with pytest.raises(module().CaptureError, match="^permission_denied$"):
        broker.disarm()
    assert path.exists()
    assert broker.status == "error"
    assert json.loads(broker.control_path.read_text())["enabled"] is False
    with pytest.raises(module().CaptureError, match="^permission_denied$"):
        broker.arm(BIZ)


def test_ack_delete_failure_can_be_explicitly_retried_by_disarm(rig, monkeypatch):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    own = drop(root, event(round_))
    def fail(*args, **kwargs):
        raise OSError("TEST_SECRET")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        with pytest.raises(module().CaptureError, match="^cleanup_failed$"):
            broker.consume()
    broker.disarm()
    assert not own.exists()
    assert broker.status == "idle"
    assert broker.arm(BIZ).generation > round_.generation


def test_restart_must_not_adopt_even_a_well_formed_previous_event(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    orphan = drop(root, event(round_))
    expected = orphan.read_bytes()
    restarted = module().SessionCaptureBroker(root, clock=lambda: now[0], permission_verifier=lambda p: True)
    restarted.disarm()  # no in-memory round, no cleanup authority
    with pytest.raises(module().CaptureError, match="^orphaned_capture_files$"):
        restarted.arm(BIZ)
    assert orphan.read_bytes() == expected


def test_multiple_early_checks_remain_ambiguous_and_are_cleaned(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    drop(root, urlcheck(round_))
    assert broker.consume() is None
    check = urlcheck(round_)
    check["session_id"] = "3"
    drop(root, check)
    assert broker.consume() is None
    drop(root, event(round_))
    assert broker.consume().headers["Cookie"] == "wap_sid2=TEST_COOKIE"
    assert not list(root.glob("*.event.json"))


def test_cleanup_unknown_invalid_and_foreign_files_remain_byte_exact(rig):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    paths = [drop(root, event(round_, generation=99)), drop(root, event(round_, request_text="bad input"))]
    invalid = root / "not-an-owned-id.event.json"
    invalid.write_text("TEST_PRIVATE_INVALID")
    paths.append(invalid)
    original = {p: p.read_bytes() for p in paths}
    own = drop(root, event(round_))
    assert broker.consume()
    broker.disarm()
    assert not own.exists()
    assert all(p.read_bytes() == data for p, data in original.items())


def test_inbox_enumeration_is_bounded_without_deleting_overflow(rig, monkeypatch):
    broker, root, now, _, _ = rig
    broker.arm(BIZ)
    count = [0]
    class Entry:
        def __init__(self, n):
            self.name = f"{n:032x}.event.json"
    class Entries:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def __iter__(self):
            for n in range(1000000):
                count[0] += 1
                assert count[0] <= 257, "must not materialize an unbounded inbox"
                yield Entry(n)
    monkeypatch.setattr(module().os, "scandir", lambda p: Entries())
    with pytest.raises(module().CaptureError, match="^inbox_full$"):
        broker.consume()
    assert count[0] == 257


def test_default_private_root_avoids_mutable_profile_ancestors(monkeypatch):
    m = module()
    monkeypatch.setattr(m, "_WINDOWS", True)
    monkeypatch.setenv("SystemDrive", "C:")
    assert m.default_private_root() == Path("C:/CssciWechatPrivate/session")


@pytest.mark.parametrize("path", ["/mp/urlcheck?scene=1", "/mp/urlcheck?action=urlcheck&scene=1",
                                   "/mp/profile_ext?action=urlcheck&scene=1"])
def test_urlcheck_path_variants_update_only_paired_home(rig, path):
    broker, root, now, _, parsed = rig
    broker.request_parser = module()._default_parser
    round_ = broker.arm(BIZ)
    now[0] = 1003
    check = urlcheck(round_)
    check["request_text"] = check["request_text"].replace("/mp/urlcheck?scene=1", path)
    drop(root, event(round_))
    drop(root, check)
    result = broker.consume()
    assert result.headers["Cookie"] == "wap_sid2=TEST_UPDATED"
    assert result.biz == BIZ and result.params["action"] == "home"


@pytest.mark.parametrize("change", ["process", "referer", "biz", "session", "header", "kind", "alone"])
def test_profile_urlcheck_pairing_is_not_weakened(rig, change):
    broker, root, now, _, _ = rig
    round_ = broker.arm(BIZ)
    now[0] = 1003
    check = urlcheck(round_)
    check["request_text"] = check["request_text"].replace("/mp/urlcheck?scene=1", "/mp/profile_ext?action=urlcheck&scene=1")
    if change == "process": check["process_id"] += 1
    if change == "referer":
        check["referer"] = URL.replace("TEST_BIZ", "OTHER")
        check["request_text"] = check["request_text"].replace(URL, check["referer"])
    if change == "biz": check["request_text"] = check["request_text"].replace("scene=1", "__biz=OTHER")
    if change == "session":
        check["request_text"] = check["request_text"].replace("scene=1", "uin=TEST_OTHER&key=TEST_KEY")
        check["sessionparams"] = {"uin": "TEST_OTHER", "key": "TEST_KEY"}
    if change == "header": check["request_text"] = check["request_text"].replace("TEST_COOKIE", "OTHER_COOKIE")
    if change == "kind": check["kind"] = "home"
    if change != "alone": drop(root, event(round_))
    drop(root, check)
    result = broker.consume()
    if change == "alone": assert result is None
    else: assert result.headers["Cookie"] == "wap_sid2=TEST_COOKIE"


# The helper classification block is pure logic. Execute just that block in
# Node with in-memory Uri/Hashtable stand-ins; never load Fiddler or RecordCore.
ENDPOINT_CASES = [
    ("/mp/profile_ext", "home", "home"),
    ("/mp/profile_ext", "urlcheck", "urlcheck"),
    ("/mp/urlcheck", None, "urlcheck"),
    ("/mp/urlcheck", "urlcheck", "urlcheck"),
    ("/mp/profile_ext", None, ""),
    ("/mp/profile_ext", "getmsg", ""),
    ("/mp/profile_ext", "", ""),
    ("/mp/profile_ext", "URLCHECK", ""),
    ("/mp/urlcheck", "home", ""),
    ("/mp/urlcheck", "getmsg", ""),
    ("/mp/urlcheck", "", ""),
    ("/mp/urlcheck", "URLCHECK", ""),
    ("/mp/other", "urlcheck", ""),
]


@pytest.mark.parametrize("path,action,expected", ENDPOINT_CASES)
def test_broker_request_kind_matches_endpoint_action(path, action, expected):
    query = "scene=1" if action is None else "action=" + action
    text = RAW.replace(URL, f"https://mp.weixin.qq.com{path}?{query}")
    for kind in ("home", "urlcheck"):
        assert (module()._request(text, kind) is not None) == (kind == expected)


def test_fiddler_endpoint_classification_without_recording_or_ui():
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable for isolated helper expression test")
    source = (Path(__file__).parents[1] / "capture/fiddler_session.js").read_text(encoding="utf-8-sig")
    block = re.search(r'        var kind: String = "";.*?(?=        var headers:)', source, re.S).group()
    block = block.replace("var kind: String", "var kind")
    script = """
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function classify(path, action) {
  const u = {AbsolutePath:path};
  const q = {ContainsKey(k) {return Object.prototype.hasOwnProperty.call(this,k)}};
  if (action !== null) q.action = action;
  function Text(v) {return v == null ? '' : String(v)}
""" + block + """
  return kind;
}
process.stdout.write(JSON.stringify(cases.map(([p,a]) => classify(p,a) || '')));
"""
    result = subprocess.run([node, "-e", script], input=json.dumps(ENDPOINT_CASES),
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [case[2] for case in ENDPOINT_CASES]


def test_event_arriving_during_slow_acl_read_uses_validation_time(rig,monkeypatch):
    broker,root,now,_,_=rig
    round_=broker.arm(BIZ)
    now[0]=1001.0
    path=drop(root,event(round_))
    original=broker._read
    def delayed_read(p,**kw):
        result=original(p,**kw)
        if p==path:now[0]=1003.0
        return result
    monkeypatch.setattr(broker,'_read',delayed_read)
    assert isinstance(broker.consume(),HomeRequest)
    assert not path.exists()


@pytest.mark.parametrize('status,payload,accepted',[(200,{'version':'cssci-session-health-v1'},True),(200,{'version':'OTHER'},False),(502,{'version':'cssci-session-health-v1'},False)])
def test_fiddler_preflight_uses_only_local_proxy_and_fixed_contract(monkeypatch,status,payload,accepted):
    import requests
    from types import SimpleNamespace
    calls=[]
    class Session:
        trust_env=True
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**kw):
            assert self.trust_env is False
            calls.append((url,kw))
            return SimpleNamespace(status_code=status,json=lambda:payload)
    monkeypatch.setattr(requests,'Session',Session)
    if accepted:assert module().verify_fiddler_hook() is True
    else:
        with pytest.raises(module().CaptureError,match='^fiddler_hook_not_ready$'):module().verify_fiddler_hook()
    assert calls==[('http://cssci-session.invalid/health',{'proxies':{'http':'http://127.0.0.1:8899'},'timeout':(2,3),'allow_redirects':False})]
