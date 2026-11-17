"""V1.2 private, one-shot session provider. No installation or ACL mutation.

The owner SID, SYSTEM and Administrators are trusted. Other principals may not
access the private root/files or replace ancestors. Same-owner malicious code is
outside this boundary (it can already read this owner's credentials). The caller
must provision the directory explicitly; no constructor creates it. Event files
are acknowledged only after current-round validation. Cleanup rechecks exact
file identity, content digest, ACL and non-reparse status. Unknown/old events
and unconfirmed .tmp files are never deleted: arm refuses them with
orphaned_capture_files for explicit operator review. Crashes never transfer
cleanup authority to a new broker. Deletion is not secure disk erasure.

Fiddler writes one <event_id>.event.json atomically. Schema 1 includes round
identity, request start/capture Unix timestamps, process identity, session id,
raw request headers, and an allowlisted response_set_cookie list. No body.
Only a home produces a HomeRequest; optional urlcheck is never a prerequisite.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from http.cookies import SimpleCookie
import json
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from urllib.parse import unquote, urlsplit
import uuid

from crawl.profile_session_probe import HomeRequest

_WINDOWS = os.name == "nt"
_MAX_BYTES = 131072
_SESSION_FIELDS = frozenset({"uin", "key", "pass_ticket", "appmsg_token", "wxtoken", "sessionparams"})
_COOKIE_NAMES = frozenset({"wap_sid2", "wxuin", "wxtokenkey", "appmsg_token", "pass_ticket"})


class CaptureError(RuntimeError):
    """Only fixed codes; never wrap an exception containing captured data."""


@dataclass(frozen=True)
class CaptureRound:
    run_id: str
    generation: int
    target_biz: str
    armed_at: float
    deadline: float


def verify_fiddler_hook() -> bool:
    """Check the loaded local hook before using UI; never send session material."""
    import requests
    ready = False
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get("http://cssci-session.invalid/health",
                proxies={"http": "http://127.0.0.1:8899"}, timeout=(2, 3), allow_redirects=False)
            payload = response.json() if response.status_code == 200 else None
            ready = isinstance(payload, dict) and payload.get("version") == "cssci-session-health-v1"
    except (requests.RequestException, ValueError):
        pass
    if not ready:
        raise CaptureError("fiddler_hook_not_ready") from None
    return True


def default_private_root() -> Path:
    drive = os.environ.get("SystemDrive")
    if not _WINDOWS or not drive:
        raise CaptureError("acl_unavailable")
    return Path(drive + "/") / "CssciWechatPrivate" / "session"


def _path_chain(path: Path) -> None:
    # lstat, not resolve(): resolving first would hide a junction/symlink.
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise CaptureError("invalid_private_path")
    if any(part in (".", "..") or ":" in part for part in path.parts[1:]):
        raise CaptureError("invalid_private_path")
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise CaptureError("reparse_path")
    if path.is_file() and path.stat().st_nlink != 1:
        raise CaptureError("hardlink_path")


# Read-only .NET ACL inspection. Unknown allow ACEs fail closed; deny ACEs never
# justify a broad allow. Parent create-child rights alone cannot replace an
# existing protected child, so ancestors use delete/change-security rights only.
_ACL_SCRIPT = r"""
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
try {
  $p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PATH__'))
  $trusted=@([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18','S-1-5-32-544')
  $installer='S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
  $first=$true
  while ($p) {
    $item=Get-Item -LiteralPath $p -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { exit 2 }
    $acl=Get-Acl -LiteralPath $p
    $owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($trusted -notcontains $owner -and ($first -or $owner -ne $installer)) { exit 2 }
    $raw=New-Object Security.AccessControl.RawSecurityDescriptor($acl.GetSecurityDescriptorBinaryForm(),0)
    if ($null -eq $raw.DiscretionaryAcl -or $raw.DiscretionaryAcl.Count -eq 0) { exit 2 }
    foreach ($ace in $raw.DiscretionaryAcl) {
      if ($ace.AceType -ne [Security.AccessControl.AceType]::AccessAllowed -and
          $ace.AceType -ne [Security.AccessControl.AceType]::AccessDenied) { exit 2 }
    }
    foreach ($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
      # Ancestor inherit-only ACEs do not grant replacement rights on that ancestor.
      if (!$first -and ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0) { continue }
      if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
          $trusted -notcontains $rule.IdentityReference.Value) {
        $rights=[int64]$rule.FileSystemRights
        if ($first -or ($rights -band 0x500D0040) -ne 0) { exit 2 }
      }
    }
    $first=$false
    $p=[IO.Path]::GetDirectoryName($p.TrimEnd('\'))
  }
  [Console]::Out.Write('ACL_OK')
} catch { exit 3 }
"""


def verify_private_path(path: Path) -> bool:
    """Read-only Windows DACL/owner/path-chain verification; no ACL repair."""
    if not _WINDOWS:
        raise CaptureError("acl_unavailable")
    code = None
    try:
        path = Path(path)
        _path_chain(path)
        encoded_path = base64.b64encode(str(path).encode()).decode("ascii")
        script = _ACL_SCRIPT.replace("__PATH__", encoded_path)
        executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(script.encode("utf-16le")).decode("ascii")],
            capture_output=True, timeout=10, creationflags=0x08000000,
        )
        if result.returncode != 0 or result.stdout.strip() != b"ACL_OK":
            code = "permission_denied"
    except CaptureError as exc:
        code = str(exc)
    except Exception:
        code = "acl_unavailable"
    if code:
        raise CaptureError(code)
    return True


def _default_parser(text, *, expected_biz):
    missing = False
    try:
        from config.journal_request_input import read_request_input
    except ImportError:
        missing = True
    if missing:
        raise CaptureError("parser_unavailable")
    return read_request_input(text, expected_biz=expected_biz)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _request(text, kind):
    """Validate transport origin and header uniqueness before invoking parser."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_BYTES or "\x00" in text:
        return None
    lines = text.replace("\r\n", "\n").split("\n")
    match = re.fullmatch(r"GET (\S+) HTTP/1\.[01]", lines[0])
    if not match:
        return None
    u = urlsplit(match[1])
    if u.scheme != "https" or u.netloc != "mp.weixin.qq.com" or u.fragment:
        return None
    q = {}
    for part in u.query.split("&"):
        key, sep, value = part.partition("=")
        if not sep or re.search(r"%(?![0-9a-fA-F]{2})", part):
            return None
        key, value = unquote(key, errors="strict"), unquote(value, errors="strict")
        if key in q or not re.fullmatch(r"[A-Za-z0-9_]+", key) or re.search(r"[\x00-\x20\x7f]", value):
            return None
        q[key] = value
    if kind == "home":
        if u.path != "/mp/profile_ext" or q.get("action") != "home":
            return None
    elif kind == "urlcheck":
        if not ((u.path == "/mp/profile_ext" and q.get("action") == "urlcheck")
                or (u.path == "/mp/urlcheck" and q.get("action", "urlcheck") == "urlcheck")):
            return None
    else:
        return None
    headers = {}
    for line in lines[1:]:
        if not line:
            break
        if line[0].isspace() or ":" not in line:
            return None
        key, value = line.split(":", 1)
        key, value = key.lower(), value.strip()
        if not re.fullmatch(r"[a-z0-9-]+", key) or key in headers or any(ord(c) < 32 for c in value):
            return None
        if key in {"proxy-authorization", "proxy-connection", "forwarded", "x-forwarded-for", "via"}:
            return None
        headers[key] = value
    if not headers.get("user-agent") or not headers.get("cookie"):
        return None
    if "host" in headers and headers["host"].lower() != "mp.weixin.qq.com":
        return None
    return match[1], q, headers


class SessionCaptureBroker:
    def __init__(self, root, *, clock=time.time, timeout_seconds=60,
                 permission_verifier=None, request_parser=None):
        self.status = "idle"
        self.last_error = None
        self.clock = clock
        self.permission_verifier = permission_verifier or verify_private_path
        self.request_parser = request_parser or _default_parser
        invalid = False
        try:
            self.root = Path(root)
            invalid = (not self.root.is_absolute() or str(self.root).startswith(("\\\\", "//"))
                       or any(part in (".", "..") or ":" in part for part in self.root.parts[1:]))
        except (TypeError, ValueError):
            invalid = True
        if invalid:
            self._fail("invalid_private_path")
        self.control_path = self.root / "control.json"
        self._round = None
        self._generation = 0
        self._seen = set()
        self._owned = {}  # exact Path -> stat identity + SHA256, no credentials
        self._buffer = {}  # at most 64 validated current-round urlcheck records
        self._cleanup_blocked = False
        if type(timeout_seconds) not in (int, float) or not 1 <= timeout_seconds <= 300:
            self._fail("invalid_timeout")
        self.timeout_seconds = timeout_seconds
        if not self.root.is_dir():
            self._fail("private_root_missing")
        self._check(self.root)

    def _fail(self, code):
        self.status, self.last_error = "error", code
        if hasattr(self, "_buffer"):
            self._buffer.clear()
        raise CaptureError(code)

    def _check(self, path):
        code = None
        try:
            _path_chain(path)
            if self.permission_verifier(path) is not True:
                code = "permission_denied"
        except CaptureError as exc:
            code = str(exc) if str(exc) in {"permission_denied", "acl_unavailable", "reparse_path",
                                           "hardlink_path", "invalid_private_path"} else "permission_denied"
        except Exception:
            code = "permission_denied"
        if code:
            self._fail(code)

    def _now(self):
        code = False
        try:
            now = self.clock()
            code = type(now) not in (float, int) or not math.isfinite(now)
        except Exception:
            code = True
        if code:
            self._fail("clock_invalid")
        return float(now)

    @staticmethod
    def _identity(info):
        # Windows Python versions differ: fstat ctime is ChangeTime while
        # stat ctime may still be CreationTime. BirthTime agrees after rename.
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                getattr(info, "st_birthtime_ns", info.st_ctime_ns))

    def _read(self, path, *, identity=False):
        self._check(self.root)
        self._check(path)
        with path.open("rb") as stream:
            before = path.stat()
            opened = os.fstat(stream.fileno())
            if (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev):
                self._fail("path_changed")
            data = stream.read(_MAX_BYTES + 1)
        self._check(path)
        if len(data) > _MAX_BYTES:
            raise ValueError()
        after = path.stat()
        if self._identity(opened) != self._identity(after) or not stat.S_ISREG(after.st_mode):
            self._fail("path_changed")
        row = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_object)
        return (row, (self._identity(after), hashlib.sha256(data).digest())) if identity else row

    def _write_control(self, value):
        # Validate the empty temporary file before any bytes enter it. The root
        # is already private; inherited ACLs must also pass after atomic rename.
        self._check(self.root)
        if self.control_path.exists():
            self._check(self.control_path)
        temporary = self.root / (uuid.uuid4().hex + ".tmp")
        failed = False
        try:
            with temporary.open("xb") as stream:
                self._check(temporary)
                stream.write(json.dumps(value, allow_nan=False).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            self._check(temporary)
            self._check(self.root)
            if self.control_path.exists():
                self._check(self.control_path)
            os.replace(temporary, self.control_path)
            self._check(self.control_path)
        except CaptureError:
            raise
        except Exception:
            failed = True
        if failed:
            self._fail("capture_io")

    def _control(self, enabled):
        return {"schema": 1, "enabled": enabled, **asdict(self._round)}

    def arm(self, target_biz):
        if self._cleanup_blocked:
            self._fail(self.last_error)
        if not isinstance(target_biz, str) or not re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", target_biz):
            self._fail("invalid_target_biz")
        if self._round is not None:
            self.disarm()
        self._check(self.root)
        # Do not read/adopt stale credentials after restart or previous errors.
        if self._inbox_paths(include_temporary=True):
            self._fail("orphaned_capture_files")
        failed = False
        try:
            if self.control_path.exists():
                old = self._read(self.control_path)
                if not isinstance(old, dict) or type(old.get("generation")) is not int or old["generation"] < 1:
                    raise ValueError()
                self._generation = max(self._generation, old["generation"])
        except CaptureError:
            raise
        except Exception:
            failed = True
        if failed:
            self._fail("control_invalid")
        now = self._now()
        self._generation += 1
        self._round = CaptureRound(uuid.uuid4().hex, self._generation, target_biz, now, now + self.timeout_seconds)
        self._write_control(self._control(True))
        self._seen.clear()
        self._buffer.clear()
        self.status, self.last_error = "waiting", None
        return self._round

    def disarm(self):
        if self._round is not None:
            self._finish_round()
        self._round = None
        self._seen.clear()
        self._buffer.clear()
        self.status, self.last_error = "idle", None

    def _inbox_paths(self, *, include_temporary=False):
        # Bound enumeration too, not just the resulting list. Unknown entries
        # count toward the bound but are never read or removed for convenience.
        paths = []
        failure = None
        try:
            with os.scandir(self.root) as entries:
                for number, entry in enumerate(entries):
                    if number >= 1024:
                        failure = "inbox_full"
                        break
                    if entry.name.endswith(".event.json") or (include_temporary and entry.name.endswith(".tmp")):
                        paths.append(self.root / entry.name)
                        if include_temporary:
                            break  # presence alone requires review; no credential read
                        if len(paths) > 256:
                            failure = "inbox_full"
                            break
        except Exception:
            failure = "capture_io"
        if failure:
            self._fail(failure)
        return sorted(paths)

    def _scan(self, now, *, collecting):
        homes = []
        failed = False
        try:
            for path in self._inbox_paths():
                if collecting and not self._round.armed_at <= self._now() < self._round.deadline:
                    self.status = "expired"
                    self._buffer.clear()
                    break
                if path in self._owned or path.name in self._seen:
                    continue
                try:
                    row, identity = self._read(path, identity=True)
                    # ACL/file reads can outlast a new event arriving. Validate against
                    # the clock after the read, not the beginning of this polling cycle.
                    request = self._eligible(row, path, self._now())
                except (ValueError, TypeError, KeyError):
                    request = None
                self._seen.add(path.name)  # only after the read/validation attempt
                if request is None:
                    self.last_error = "event_rejected"
                    continue
                if len(self._owned) >= 256:
                    self._fail("inbox_full")
                self._owned[path] = identity
                if collecting:
                    if row["kind"] == "urlcheck":
                        if len(self._buffer) >= 64:
                            self._fail("capture_buffer_full")
                        self._buffer[path] = (row, request)
                    else:
                        homes.append((row, request))
        except CaptureError:
            raise
        except Exception:
            failed = True
        if failed:
            self._fail("capture_io")
        return homes

    def _finish_round(self):
        # Revoke capture before ack. Never report captured/idle until cleanup
        # succeeds; keep the ownership registry on failure for a safe retry.
        self._ensure_control()
        self._write_control(self._control(False))
        failure = None
        try:
            self._scan(self._now(), collecting=False)
            for path, identity in list(self._owned.items()):
                if self._ensure_control():
                    self._fail("control_conflict")
                row, current = self._read(path, identity=True)
                if current != identity or self._eligible(row, path, self._now()) is None:
                    self._fail("cleanup_identity_changed")
                self._check(self.root)
                self._check(path)
                if self._identity(path.lstat()) != identity[0]:
                    self._fail("cleanup_identity_changed")
                path.unlink()  # exact registered file, never a glob/recursive delete
                if os.path.lexists(path):
                    self._fail("cleanup_failed")
                del self._owned[path]
        except CaptureError as exc:
            failure = str(exc)
        except Exception:
            failure = "cleanup_failed"
        finally:
            self._buffer.clear()
        if failure:
            self._cleanup_blocked = True
            self._fail(failure)
        self._cleanup_blocked = False

    def _ensure_control(self):
        failed = False
        try:
            actual = self._read(self.control_path)
            expected = self._control(True)
            if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in expected.items() if k != "enabled"):
                failed = True
            if type(actual.get("enabled")) is not bool:
                failed = True
        except CaptureError:
            raise
        except Exception:
            failed = True
        if failed:
            self._fail("control_conflict")
        return actual["enabled"]

    def _eligible(self, row, path, now):
        r = self._round
        if not isinstance(row, dict) or type(row.get("schema")) is not int or row["schema"] != 1:
            return None
        eid = row.get("event_id")
        if not isinstance(eid, str) or not re.fullmatch(r"[0-9a-f]{32}", eid) or path.name != eid + ".event.json":
            return None
        if type(row.get("generation")) is not int or any(row.get(k) != getattr(r, k) for k in ("run_id", "generation", "target_biz")):
            return None
        start, captured = row.get("started_at"), row.get("captured_at")
        if (type(start) not in (float, int) or type(captured) not in (float, int)
                or not r.armed_at <= start <= captured <= now or captured >= r.deadline):
            return None
        if (row.get("process_name") != "wechatappex" or type(row.get("process_id")) is not int
                or row["process_id"] <= 0 or not re.fullmatch(r"[1-9][0-9]{0,19}", str(row.get("session_id", "")))):
            return None
        parsed = _request(row.get("request_text"), row.get("kind"))
        if parsed is None:
            return None
        _, params, headers = parsed
        if row["kind"] == "home" and params.get("__biz") != r.target_biz:
            return None
        if row["kind"] == "urlcheck" and params.get("__biz", r.target_biz) != r.target_biz:
            return None
        if row.get("referer", "") != headers.get("referer", "") or row.get("sessionparams", {}) != {k: v for k, v in params.items() if k in _SESSION_FIELDS}:
            return None
        cookies = row.get("response_set_cookie", [])
        if not isinstance(cookies, list) or len(cookies) > 32 or any(not isinstance(c, str) for c in cookies):
            return None
        return parsed

    def consume(self) -> HomeRequest | None:
        if self._round is None or self.status in {"captured", "expired", "error"}:
            return None
        now = self._now()
        if now < self._round.armed_at or now >= self._round.deadline:
            self.status = "expired"
            self._buffer.clear()
            return None
        if not self._ensure_control():
            self.status = "idle"
            self._buffer.clear()
            return None
        self._check(self.root)
        homes = self._scan(now, collecting=True)
        rows = homes + list(self._buffer.values())
        if not self._round.armed_at <= self._now() < self._round.deadline:
            self.status = "expired"
            self._buffer.clear()
            return None
        # Different homes in a single scan are ambiguous, even within one biz.
        identities = {(row["process_id"], row["session_id"]) for row, _ in homes}
        if len(identities) != 1 or len({row["request_text"] for row, _ in homes}) != 1:
            if rows:
                self.last_error = "event_rejected"
            return None
        home, request = homes[0]
        cookies = list(home.get("response_set_cookie", []))
        checks = []
        for row, req in rows:
            if row["kind"] != "urlcheck" or row["process_id"] != home["process_id"]:
                continue
            referer, params = row.get("referer", ""), row.get("sessionparams", {})
            shared = params.keys() & home.get("sessionparams", {}).keys()
            agrees = all(params[k] == home["sessionparams"][k] for k in shared)
            # A matching referer OR uin plus a credential (or explicit blob).
            # Any overlapping contradictory session/header evidence vetoes it.
            by_ref = bool(referer) and referer == request[0]
            by_params = agrees and ("sessionparams" in shared or ("uin" in shared and bool(shared & {"key", "pass_ticket"})))
            if ((by_ref or by_params) and (not referer or by_ref) and agrees
                    and all(req[2][k] == request[2][k] for k in ("user-agent", "cookie"))
                    and row["started_at"] >= home["started_at"]):
                checks.append(row)
        if len(checks) == 1:
            cookies.extend(checks[0].get("response_set_cookie", []))
        failure = None
        result = None
        try:
            result = self.request_parser(home["request_text"], expected_biz=self._round.target_biz)
            if not isinstance(result, HomeRequest) or result.biz != self._round.target_biz:
                raise ValueError()
            # Preserve original captured UA/Cookie in the event; replay may use
            # scoped, allowlisted response cookie updates, never response UA.
            result.headers = {**{k: v for k, v in result.headers.items() if k.lower() not in {"user-agent", "cookie"}},
                              "User-Agent": request[2]["user-agent"], "Cookie": request[2]["cookie"]}
            result.headers["Cookie"] = _merge_cookies(result.headers["Cookie"], cookies)
        except CaptureError as exc:
            failure = "parser_unavailable" if str(exc) == "parser_unavailable" else "request_rejected"
        except Exception:
            failure = "request_rejected"
        if failure == "parser_unavailable":
            self._fail(failure)
        if failure:
            self.last_error = failure
            return None
        if not self._ensure_control() or not self._round.armed_at <= self._now() < self._round.deadline:
            self.status = "expired"
            return None
        self._finish_round()
        self.status, self.last_error = "captured", None
        return result


def _merge_cookies(original, updates):
    if not isinstance(updates, list) or len(updates) > 32:
        raise ValueError()
    # No updates means preserve the exact request Cookie bytes/value.
    if not updates:
        return original
    replacements = {}
    for text in updates:
        if not isinstance(text, str) or len(text) > 8192 or "\r" in text or "\n" in text:
            continue
        jar = SimpleCookie()
        jar.load(text)
        if len(jar) != 1:
            continue
        name, value = next(iter(jar.items()))
        if name not in _COOKIE_NAMES or value["domain"].lstrip(".").lower() not in ("", "mp.weixin.qq.com", "weixin.qq.com"):
            continue
        if value["path"] not in ("", "/", "/mp", "/mp/") or value["max-age"] == "0" or value["expires"]:
            continue
        replacements[name] = value.coded_value
    parts = [p.strip() for p in original.split(";") if p.strip()]
    parts = [p for p in parts if p.split("=", 1)[0].strip() not in replacements]
    parts.extend(f"{k}={v}" for k, v in replacements.items())
    return "; ".join(parts)


def prepare_candidate(root: Path) -> str:
    """Return reviewable text only. Does not read/modify active Fiddler rules."""
    root = Path(root)
    if not root.is_absolute() or str(root).startswith(("\\\\", "//")) or any(c in str(root) for c in "\r\n"):
        raise CaptureError("invalid_private_path")
    failed = False
    try:
        source = (Path(__file__).parents[1] / "capture/fiddler_session.js").read_text(encoding="utf-8-sig")
    except Exception:
        failed = True
    if failed:
        raise CaptureError("candidate_unavailable")
    return source.replace('"__CSSCI_SESSION_ROOT__"', json.dumps(str(root), ensure_ascii=True))
