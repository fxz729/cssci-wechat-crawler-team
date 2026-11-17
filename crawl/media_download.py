"""Download direct article media without resolving players or changing client settings.

Only requests and the standard library are used. No browser/capture integration,
credential inspection, URL-signature expansion, transcoding, or manifest sidecar.
The caller persists the returned rows and joins local_path to article_dir when
exporting tables. Each call owns its temporary files; use one session per worker.
"""

from collections.abc import Iterable, Mapping
from contextlib import suppress
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import socket
import tempfile
import time
from urllib.parse import unquote, urldefrag, urljoin, urlsplit, urlunsplit

import requests

from crawl.request_governor import TrafficStopped


_FOLDERS = {"image": "images", "audio": "audio", "video": "video"}
_CHUNK_SIZE = 64 * 1024
_PREFIX_SIZE = 8192
_MAX_REDIRECTS = 5
_PLAYLIST_TYPES = {
    "application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl",
    "audio/x-mpegurl", "application/dash+xml",
}
_BINARY_TYPES = {"", "application/octet-stream", "binary/octet-stream", "application/binary", "application/download"}


class _MediaError(Exception):
    def __init__(self, code: str, status: str = "error"):
        super().__init__(code)
        self.code = code
        self.status = status


def download_media_assets(
    media_assets: Iterable[Mapping],
    article_dir: str | Path,
    *,
    session=None,
    timeout: float | tuple[float, float] = (10, 60),
    max_bytes: int = 512 * 1024 * 1024,
) -> list[dict]:
    """Return one manifest row per parser asset, preserving input order.

    Rows contain kind, source_url, local_path (article-relative POSIX path), bytes,
    sha256, status and error_code. Status is downloaded, skipped, needs_resolution
    or error. Unavailable files have empty path/hash and zero bytes; error_code
    contains only a fixed code, never request/exception text. source_url retains
    only the origin and an opaque URL digest, not paths, queries or credentials.

    Filenames contain a 96-bit URL digest and full content SHA-256. Existing files
    are streamed and checked against that digest and media signature before skip,
    including on subsequent calls without a previous manifest. Invalid cache
    entries remain untouched until a complete replacement is ready. The directory
    is caller-owned; do not concurrently replace its directories with symlinks.

    HTTP(S) targets and every redirect are checked against public DNS/IP results;
    automatic redirects are disabled. A caller session is never forwarded across
    origins (cross_origin_redirect); plain requests may follow public redirects.
    HTTPS downgrades are refused. DNS and HTTP transport remain those of the
    caller/requests: this does not reconfigure proxies, TLS, auth or DNS pinning.
    No network is performed at import time. Timeout/size are per resource; media
    requiring player, voice-ID, HLS or DASH resolution is not expanded here.
    """
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    client = requests if session is None else session
    rows = []
    for asset in media_assets:
        row = {"kind": "", "source_url": "", "local_path": "", "bytes": 0,
               "sha256": "", "status": "error", "error_code": ""}
        try:
            if not isinstance(asset, Mapping):
                raise _MediaError("invalid_asset")
            kind = asset.get("kind")
            if not isinstance(kind, str) or kind not in _FOLDERS:
                raise _MediaError("invalid_kind")
            row["kind"] = kind
            raw_url = asset.get("url", "")
            if raw_url is None or raw_url == "":
                raise _MediaError("missing_direct_url", "needs_resolution")
            url = _checked_url(raw_url)
            url_key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            row["source_url"] = _redacted_url(url, url_key)
            _check_resolution(url)
            root = Path(article_dir).resolve()
            folder = _safe_path(root, root / _FOLDERS[kind])
            cached = _verified_cache(root, folder, url_key, kind, max_bytes)
            if cached is not None:
                row.update(cached, status="skipped")
            else:
                response = _open_media(client, url, timeout)
                try:
                    row.update(_save_media(response, root, folder, url_key, kind, max_bytes), status="downloaded")
                except BaseException:
                    finish = getattr(response, "finish_media", None)
                    if callable(finish):
                        finish(False)
                    raise
                else:
                    finish = getattr(response, "finish_media", None)
                    if callable(finish):
                        finish(True)
                finally:
                    with suppress(Exception):
                        response.close()
        except TrafficStopped:
            raise
        except _MediaError as exc:
            row.update(status=exc.status, error_code=exc.code)
        except requests.RequestException:
            row["error_code"] = "network_error"
        except OSError:
            row["error_code"] = "io_error"
        except Exception:
            # A custom client or malformed row must not discard earlier results.
            row["error_code"] = "download_error"
        rows.append(row)
    return rows


def _checked_url(value: str) -> str:
    if not isinstance(value, str) or "\\" in value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise _MediaError("invalid_url")
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or "%" in parts.hostname or parts.port == 0):
            raise ValueError
    except ValueError:
        raise _MediaError("invalid_url") from None
    # Fragments are not sent to HTTP servers; query bytes/order are preserved.
    return urldefrag(value)[0]


def _redacted_url(url: str, url_key: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/redacted/" + url_key, "", ""))


def _check_resolution(url: str) -> None:
    path = unquote(urlsplit(url).path).lower()
    if path.endswith((".m3u8", ".m3u", ".mpd")):
        raise _MediaError("playlist_requires_resolution", "needs_resolution")
    if (path.endswith((".html", ".htm")) or path.rstrip("/") == "/mp/readtemplate"
            or re.search(r"/(?:iframe|player)(?:[/._-]|$)", path)):
        raise _MediaError("player_url", "needs_resolution")


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not ip.is_global or ip.is_multicast or ip.is_reserved:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _is_public(str(ip.ipv4_mapped))
        if ip.sixtofour is not None:
            return _is_public(str(ip.sixtofour))
        if ip.teredo is not None:
            return all(_is_public(str(part)) for part in ip.teredo)
    return True


def _check_public_address(url: str) -> None:
    parts = urlsplit(url)
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise _MediaError("blocked_address")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError:
            raise _MediaError("dns_error") from None
        if not addresses:
            raise _MediaError("dns_error")
        if not all(_is_public(item[4][0]) for item in addresses):
            raise _MediaError("blocked_address")
    else:
        if not _is_public(host):
            raise _MediaError("blocked_address")


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)


def _open_media(client, url: str, timeout):
    initial_origin = _origin(url)
    previous_scheme = initial_origin[0]
    for hop in range(_MAX_REDIRECTS + 1):
        url = _checked_url(url)
        _check_resolution(url)
        _check_public_address(url)
        origin = _origin(url)
        if previous_scheme == "https" and origin[0] == "http":
            raise _MediaError("insecure_redirect")
        if client is not requests and origin != initial_origin:
            # Manual redirects must not leak a session's default credentials.
            raise _MediaError("cross_origin_redirect")
        previous_scheme = origin[0]
        response = client.get(url, stream=True, allow_redirects=False, timeout=timeout)
        keep_open = False
        try:
            status = response.status_code
            if status == 200:
                keep_open = True
                return response
            if status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                if not location:
                    raise _MediaError("invalid_redirect")
                if hop == _MAX_REDIRECTS:
                    raise _MediaError("too_many_redirects")
                if not isinstance(location, str) or any(ord(c) <= 32 or ord(c) == 127 for c in location):
                    raise _MediaError("invalid_url")
                url = urljoin(url, location)
            else:
                raise _MediaError(f"http_{status}" if isinstance(status, int) else "http_error")
        finally:
            if not keep_open:
                with suppress(Exception):
                    response.close()
    raise _MediaError("too_many_redirects")


def _safe_path(root: Path, path: Path) -> Path:
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise _MediaError("unsafe_path")
    return path


def _consume(chunks, max_bytes: int, output=None) -> tuple[int, str, bytes]:
    digest = hashlib.sha256()
    size = 0
    prefix = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        size += len(chunk)
        if size > max_bytes:
            raise _MediaError("size_limit")
        prefix.extend(chunk[:max(0, _PREFIX_SIZE - len(prefix))])
        digest.update(chunk)
        if output is not None:
            output.write(chunk)
    if not size:
        raise _MediaError("empty_download")
    return size, digest.hexdigest(), bytes(prefix)


def _media_extension(prefix: bytes, mime: str, kind: str) -> str:
    head = prefix.lstrip(b"\xef\xbb\xbf \t\r\n")
    if mime in _PLAYLIST_TYPES or head.startswith(b"#EXTM3U"):
        raise _MediaError("playlist_requires_resolution", "needs_resolution")
    # SVG is an original image format; it is text-based, so check it before the
    # generic HTML/XML rejection rather than treating every '<' as a web page.
    if kind == "image" and (mime == "image/svg+xml" or re.match(rb"^(?:<\?xml[^>]*>\s*)?<svg(?:\s|>)", head, re.IGNORECASE)):
        return ".svg"
    if mime in {"text/html", "application/xhtml+xml"} or head.startswith(b"<"):
        raise _MediaError("not_media", "needs_resolution" if kind in {"audio", "video"} else "error")

    detected, extension = "", ""
    for signature, ext in ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"),
                           (b"GIF87a", ".gif"), (b"GIF89a", ".gif"),
                           (b"II*\x00", ".tif"), (b"MM\x00*", ".tif"), (b"BM", ".bmp")):
        if prefix.startswith(signature):
            detected, extension = "image", ext
            break
    if prefix.startswith(b"RIFF") and len(prefix) >= 12:
        detected, extension = {b"WEBP": ("image", ".webp"), b"WAVE": ("audio", ".wav"),
                               b"AVI ": ("video", ".avi")}.get(prefix[8:12], ("", ""))
    elif prefix.startswith((b"ID3", b"#!AMR", b"fLaC", b"#!SILK_V3", b"\x02#!SILK_V3")):
        if prefix.startswith(b"ID3"):
            extension = ".mp3"
        elif prefix.startswith(b"#!AMR"):
            extension = ".amr"
        elif prefix.startswith(b"fLaC"):
            extension = ".flac"
        else:
            extension = ".silk"
        detected = "audio"
    elif len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0 and not extension:
        detected, extension = "audio", ".aac" if prefix[1] & 0xF6 == 0xF0 else ".mp3"
    elif prefix.startswith(b"OggS"):
        detected = "video" if b"theora" in prefix or mime.startswith("video/") else "audio"
        extension = ".ogv" if detected == "video" else ".ogg"
    elif len(prefix) >= 16 and prefix[4:8] == b"ftyp":
        brand = prefix[8:12].lower()
        if brand in {b"avif", b"avis", b"heic", b"heix", b"mif1", b"msf1"}:
            detected, extension = "image", ".avif" if brand in {b"avif", b"avis"} else ".heic"
        elif brand in {b"m4a ", b"m4b "} or mime.startswith("audio/") or (mime in _BINARY_TYPES and kind == "audio"):
            detected, extension = "audio", ".m4a"
        else:
            detected, extension = "video", ".mp4"
    elif prefix.startswith(b"\x1a\x45\xdf\xa3"):
        detected = "audio" if mime.startswith("audio/") or (mime in _BINARY_TYPES and kind == "audio") else "video"
        extension = ".webm"

    if not detected:
        raise _MediaError("unsupported_media")
    if detected != kind:
        raise _MediaError("media_kind_mismatch")
    if mime not in _BINARY_TYPES and mime != "application/ogg" and not mime.startswith(kind + "/"):
        raise _MediaError("content_type_mismatch")
    return extension


def _file_record(root: Path, path: Path, size: int, digest: str) -> dict:
    return {"local_path": path.relative_to(root).as_posix(), "bytes": size, "sha256": digest}


def _verified_cache(root: Path, folder: Path, url_key: str, kind: str, max_bytes: int) -> dict | None:
    if not folder.exists():
        return None
    pattern = re.compile(rf"{url_key}-([0-9a-f]{{64}})(\.[a-z0-9]{{1,5}})")
    for path in sorted(folder.glob(url_key + "-*")):
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        _safe_path(root, path)
        if not path.is_file():
            continue
        try:
            with path.open("rb") as source:
                size, digest, prefix = _consume(iter(lambda: source.read(_CHUNK_SIZE), b""), max_bytes)
            if digest == match[1] and _media_extension(prefix, "", kind) == match[2]:
                return _file_record(root, path, size, digest)
        except (OSError, _MediaError):
            continue  # Unverified cache entries are neither trusted nor removed.
    return None


def _save_media(response, root: Path, folder: Path, url_key: str, kind: str, max_bytes: int) -> dict:
    length = response.headers.get("Content-Length")
    if length is not None:
        if not isinstance(length, str) or not length.isascii() or not length.isdecimal():
            raise _MediaError("invalid_content_length")
        length = int(length)
    encoded = response.headers.get("Content-Encoding", "").strip().lower() not in {"", "identity"}
    if length is not None and not encoded and length > max_bytes:
        raise _MediaError("size_limit")
    mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    _safe_path(root, folder)
    folder.mkdir(parents=True, exist_ok=True)
    _safe_path(root, folder)
    part = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=folder, prefix="." + url_key + "-", suffix=".part", delete=False) as output:
            part = Path(output.name)
            size, digest, prefix = _consume(response.iter_content(chunk_size=_CHUNK_SIZE), max_bytes, output)
            if length is not None and not encoded and size != length:
                raise _MediaError("incomplete_download")
            extension = _media_extension(prefix, mime, kind)
            target = _safe_path(root, folder / f"{url_key}-{digest}{extension}")
            output.flush()
            os.fsync(output.fileno())
        _safe_path(root, folder)
        _safe_path(root, target)
        for attempt in range(5):
            try:
                os.replace(part, target)
                return _file_record(root, target, size, digest)
            except PermissionError:
                # Two workers may finish the same URL simultaneously. On
                # Windows the winning replacement can briefly lock the final
                # path; accept it only after verifying its hash/signature.
                cached = _verified_cache(root, folder, url_key, kind, max_bytes)
                if cached is not None:
                    return cached
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if part is not None:
            with suppress(OSError, _MediaError):
                _safe_path(root, part).unlink(missing_ok=True)

