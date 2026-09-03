#!/usr/bin/env python3
"""Local backend for the Omarchy Substack feed plugin.

The shell renders one JSON snapshot. This process owns every remote request,
the full-account session cookie, RSS parsing, deduplication, and notifications.
It deliberately uses only Python's standard library plus commands already
provided by Omarchy.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import email.utils
import fcntl
import hashlib
import html
import http.client
import ipaddress
import json
import math
import os
import re
import secrets
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable


PLUGIN_ID = "0x4a756e65.omarchy-substack"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) OmarchySubstack/0.1"
SUBSTACK_ORIGIN = "https://substack.com"
SUBSCRIPTIONS_ENDPOINTS = (
    "/api/v1/subscriptions/page_v2",
    "/api/v1/subscriptions?tvOnly=false",
    "/api/v1/subscriptions/page",
)
PROFILE_ENDPOINT = "/api/v1/user/profile/self"
MAX_FEED_BYTES = 4_000_000
MAX_JSON_BYTES = 2_000_000
MAX_STATE_BYTES = 2_000_000
MAX_CONFIG_BYTES = 4_096
MAX_ARTICLES = 160
MAX_SEEN_PER_PUBLICATION = 240
MAX_SUBSCRIPTIONS = 512
MAX_PUBLICATIONS = 1_024
MAX_PUBLICATION_USERS = 1_024
SUBSCRIPTION_SYNC_SECONDS = 12 * 60 * 60
EMPTY_SUBSCRIPTION_RECHECK_SECONDS = 15 * 60
MAX_SECRET_BYTES = 64_000
MAX_COOKIE_VALUE_BYTES = 16_384
SUBSTACK_CUSTOM_DOMAIN_SUFFIX = ".substack-custom-domains.com"
AUTH_TOP_LEVEL_HOSTS = frozenset({"substack.com", "www.substack.com"})

HOME = Path(os.environ.get("HOME", str(Path.home())))
STATE_ROOT = Path(
    os.environ.get(
        "OMARCHY_SUBSTACK_STATE_ROOT",
        str(Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "omarchy/substack"),
    )
)
STATE_FILE = STATE_ROOT / "state.json"
CONFIG_FILE = STATE_ROOT / "config.json"
STATE_LOCK = STATE_ROOT / "state.lock"
CONFIG_LOCK = STATE_ROOT / "config.lock"
DAEMON_LOCK = STATE_ROOT / "daemon.lock"
AUTH_LOCK = STATE_ROOT / "auth.lock"
REFRESH_REQUEST = STATE_ROOT / "refresh.request"
SECRET_ATTRIBUTES = ("service", "omarchy-substack", "account", "default")
SCRIPT_PATH = Path(__file__).resolve()


class BackendError(RuntimeError):
    pass


class AuthenticationExpired(BackendError):
    pass


def now_ts() -> float:
    return time.time()


def iso_from_ts(value: float | int | None = None) -> str:
    stamp = now_ts() if value is None else float(value)
    return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def default_state() -> dict[str, Any]:
    return {
        "schema": 1,
        "status": "starting",
        "message": "Starting Substack…",
        "authenticated": False,
        "syncing": False,
        "account": {},
        "subscriptions": [],
        "articles": [],
        "unread_count": 0,
        "last_sync": None,
        "last_subscription_sync": None,
        "subscription_sync_due": 0,
        "empty_subscription_confirmations": 0,
        "last_error": "",
        "updated_at": iso_from_ts(),
    }


def default_config() -> dict[str, Any]:
    return {
        "notify": True,
        # Substack returns publications the signed-in reader administers in
        # the same collection as ordinary subscriptions. A reading queue is
        # less surprising when those are excluded unless explicitly enabled.
        "include_owned": False,
    }


DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
PRIVATE_FILE_FLAGS = os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


def _absolute_state_root() -> str:
    root = os.path.abspath(os.fspath(STATE_ROOT))
    if root == os.path.sep:
        raise BackendError("The private state directory is invalid")
    return root


def _verify_parent_directory(info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise BackendError("A private state path component is not a directory")
    mode = stat.S_IMODE(info.st_mode)
    sticky_root_directory = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
    if info.st_uid not in {0, os.geteuid()} or (mode & 0o022 and not sticky_root_directory):
        raise BackendError("A private state path component has unsafe ownership or permissions")


@contextlib.contextmanager
def state_directory():
    """Open STATE_ROOT without following mutable pathname components."""
    current = os.open(os.path.sep, DIRECTORY_OPEN_FLAGS)
    try:
        _verify_parent_directory(os.fstat(current))
        parts = [part for part in Path(_absolute_state_root()).parts if part != os.path.sep]
        for index, part in enumerate(parts):
            if part in {"", ".", ".."}:
                raise BackendError("The private state directory is invalid")
            try:
                next_descriptor = os.open(part, DIRECTORY_OPEN_FLAGS, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                    os.fsync(current)
                    next_descriptor = os.open(part, DIRECTORY_OPEN_FLAGS, dir_fd=current)
                except OSError as exc:
                    raise BackendError("The private state directory could not be created safely") from exc
            except OSError as exc:
                raise BackendError("The private state directory contains an unsafe link") from exc

            try:
                info = os.fstat(next_descriptor)
                _verify_parent_directory(info)
                if index == len(parts) - 1:
                    if info.st_uid != os.geteuid():
                        raise BackendError("The private state directory is not owned by this user")
                    if stat.S_IMODE(info.st_mode) != 0o700:
                        os.fchmod(next_descriptor, 0o700)
                        info = os.fstat(next_descriptor)
                    if stat.S_IMODE(info.st_mode) != 0o700:
                        raise BackendError("The private state directory permissions are unsafe")
            except Exception:
                os.close(next_descriptor)
                raise
            os.close(current)
            current = next_descriptor
        yield current
    finally:
        os.close(current)


def ensure_dirs() -> None:
    with state_directory():
        pass


def _state_filename(path: Path) -> str:
    root = _absolute_state_root()
    target = os.path.abspath(os.fspath(path))
    if os.path.dirname(target) != root:
        raise BackendError("A private file escaped the state directory")
    name = os.path.basename(target)
    if not name or name in {".", ".."} or os.path.sep in name:
        raise BackendError("A private file name is invalid")
    return name


def _verify_private_file(descriptor: int, *, max_bytes: int | None = None) -> os.stat_result:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise BackendError("A private state file has an unsafe type or owner")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise BackendError("A private state file has unsafe permissions")
    if max_bytes is not None and info.st_size > max_bytes:
        raise BackendError("A private state file exceeds its safety limit")
    return info


def _open_private_file(
    directory: int,
    name: str,
    flags: int,
    *,
    create: bool = False,
    max_bytes: int | None = None,
) -> int:
    open_flags = flags | PRIVATE_FILE_FLAGS
    if create:
        open_flags |= os.O_CREAT
    try:
        descriptor = os.open(name, open_flags, 0o600, dir_fd=directory)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise BackendError("A private state file could not be opened safely") from exc
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.geteuid() or initial.st_nlink != 1:
            raise BackendError("A private state file has an unsafe type or owner")
        if create and stat.S_IMODE(initial.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
        _verify_private_file(descriptor, max_bytes=max_bytes)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_bounded(descriptor: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > max_bytes:
        raise BackendError("A private state file exceeds its safety limit")
    return payload


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise BackendError("A private state file could not be written")
        offset += written


@contextlib.contextmanager
def locked(path: Path, *, blocking: bool = True):
    with state_directory() as directory:
        descriptor = _open_private_file(directory, _state_filename(path), os.O_RDWR, create=True, max_bytes=0)
        try:
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(descriptor, operation)
            yield descriptor
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def read_json(path: Path, fallback: Any, *, max_bytes: int) -> Any:
    try:
        with state_directory() as directory:
            descriptor = _open_private_file(
                directory,
                _state_filename(path),
                os.O_RDONLY,
                max_bytes=max_bytes,
            )
            try:
                payload = _read_bounded(descriptor, max_bytes)
            finally:
                os.close(descriptor)
        return json.loads(payload.decode("utf-8"))
    except FileNotFoundError:
        return fallback
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError):
        return fallback


def atomic_json(path: Path, value: Any, *, max_bytes: int = MAX_STATE_BYTES) -> None:
    payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > max_bytes:
        raise BackendError("The local feed snapshot exceeds its safety limit")
    target = _state_filename(path)
    with state_directory() as directory:
        try:
            existing = _open_private_file(directory, target, os.O_RDONLY, max_bytes=max_bytes)
        except FileNotFoundError:
            existing = -1
        if existing >= 0:
            os.close(existing)

        temporary = f".{target}.{os.getpid()}.{secrets.token_hex(12)}"
        descriptor = -1
        try:
            descriptor = _open_private_file(
                directory,
                temporary,
                os.O_WRONLY | os.O_EXCL,
                create=True,
                max_bytes=max_bytes,
            )
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, target, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)


def private_file_exists(path: Path, *, max_bytes: int) -> bool:
    with state_directory() as directory:
        try:
            descriptor = _open_private_file(
                directory,
                _state_filename(path),
                os.O_RDONLY,
                max_bytes=max_bytes,
            )
        except FileNotFoundError:
            return False
        os.close(descriptor)
        return True


def consume_refresh_request() -> bool:
    with state_directory() as directory:
        name = _state_filename(REFRESH_REQUEST)
        try:
            descriptor = _open_private_file(directory, name, os.O_RDONLY, max_bytes=0)
        except FileNotFoundError:
            return False
        os.close(descriptor)
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)
        return True


def load_state() -> dict[str, Any]:
    value = read_json(STATE_FILE, default_state(), max_bytes=MAX_STATE_BYTES)
    return normalize_state(value)


def mutate_state(mutator: Callable[[dict[str, Any]], Any]) -> Any:
    with locked(STATE_LOCK):
        before = load_state()
        state = json.loads(json.dumps(before))
        result = mutator(state)
        state = normalize_state(state)
        state["unread_count"] = sum(1 for article in state.get("articles", []) if article.get("unread"))
        if state != before:
            state["updated_at"] = iso_from_ts()
            atomic_json(STATE_FILE, state, max_bytes=MAX_STATE_BYTES)
        return result


def load_config() -> dict[str, Any]:
    value = read_json(CONFIG_FILE, default_config(), max_bytes=MAX_CONFIG_BYTES)
    base = default_config()
    if isinstance(value, dict):
        base["notify"] = value.get("notify") is not False
        base["include_owned"] = value.get("include_owned") is True
    base["notify"] = base.get("notify") is not False
    base["include_owned"] = base.get("include_owned") is True
    return base


def save_config(changes: dict[str, Any]) -> None:
    # Multiple panel instances can propagate settings at once on a shell
    # reload. Merge under a process lock so one toggle never erases another.
    with locked(CONFIG_LOCK):
        config = load_config()
        for key in ("notify", "include_owned"):
            if key in changes:
                config[key] = changes[key] is True
        atomic_json(CONFIG_FILE, config, max_bytes=MAX_CONFIG_BYTES)


def secret_lookup() -> dict[str, str]:
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", *SECRET_ATTRIBUTES],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0 or not result.stdout.strip() or len(result.stdout.encode("utf-8")) > MAX_SECRET_BYTES:
        return {}
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    return normalize_session_cookies(value)


def cookie_value_is_safe(value: str) -> bool:
    encoded = value.encode("utf-8", errors="ignore")
    return bool(value) and len(encoded) <= MAX_COOKIE_VALUE_BYTES and not re.search(r"[\x00-\x20;\x7f]", value)


def normalize_session_cookies(value: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name in ("connect.sid", "substack.sid"):
        cookie = str(value.get(name) or "")
        if cookie_value_is_safe(cookie):
            normalized[name] = cookie
    return normalized


def secret_store(cookies: dict[str, str]) -> None:
    normalized = normalize_session_cookies(cookies)
    if not normalized:
        raise BackendError("Substack did not provide a valid session cookie")
    payload = json.dumps(normalized, separators=(",", ":"))
    try:
        result = subprocess.run(
            ["secret-tool", "store", "--label=Omarchy Substack session", *SECRET_ATTRIBUTES],
            input=payload,
            text=True,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackendError("The desktop keyring could not store the Substack session") from exc
    if result.returncode != 0:
        raise BackendError("The desktop keyring rejected the Substack session")


def secret_clear() -> None:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["secret-tool", "clear", *SECRET_ATTRIBUTES],
            check=False,
            capture_output=True,
            timeout=8,
        )


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in normalize_session_cookies(cookies).items())


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Fail before following any redirect, so secrets and feed fetches never change origin."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        raise BackendError("Substack returned an unexpected redirect")


HTTP_OPENER = urllib.request.build_opener(RejectRedirects())


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP peer comes from one validated DNS result."""

    def __init__(self, hostname: str, addresses: tuple[str, ...], *, timeout: int) -> None:
        if not addresses:
            raise BackendError("The publication has no validated network address")
        try:
            parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
        except ValueError as exc:
            raise BackendError("The publication has an invalid network address") from exc
        if any(not address.is_global for address in parsed_addresses):
            raise BackendError("The publication network address is not public")
        super().__init__(hostname, port=443, timeout=timeout, context=ssl.create_default_context())
        self.addresses = tuple(str(address) for address in parsed_addresses)

    def connect(self) -> None:
        last_error: OSError | None = None
        expected = {str(ipaddress.ip_address(address)) for address in self.addresses}
        for address in self.addresses:
            parsed = ipaddress.ip_address(address)
            family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
            destination: tuple[Any, ...] = (address, self.port, 0, 0) if parsed.version == 6 else (address, self.port)
            raw_socket = socket.socket(family, socket.SOCK_STREAM)
            try:
                raw_socket.settimeout(self.timeout)
                raw_socket.connect(destination)
                peer = str(ipaddress.ip_address(str(raw_socket.getpeername()[0])))
                if peer not in expected:
                    raise OSError("connected peer was outside the validated address set")
                # self.host remains the authenticated custom hostname, so the
                # default SSL context performs SNI and certificate-hostname
                # verification even though TCP is connected to a pinned IP.
                tls_socket = self._context.wrap_socket(raw_socket, server_hostname=self.host)
                tls_peer = str(ipaddress.ip_address(str(tls_socket.getpeername()[0])))
                if tls_peer not in expected:
                    tls_socket.close()
                    raise OSError("TLS peer was outside the validated address set")
                self.sock = tls_socket
                return
            except OSError as exc:
                last_error = exc
                raw_socket.close()
        raise last_error or OSError("no validated address was reachable")


def pinned_https_request(
    url: str,
    *,
    hostname: str,
    addresses: tuple[str, ...],
    headers: dict[str, str],
    max_bytes: int,
    timeout: int,
) -> tuple[int, str, dict[str, str], bytes]:
    parsed = urllib.parse.urlsplit(url)
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    connection = PinnedHTTPSConnection(hostname, addresses, timeout=timeout)
    try:
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        status = int(response.status)
        response_headers = {str(key): str(value) for key, value in response.getheaders()}
        if status == 304:
            return status, url, response_headers, b""
        if 300 <= status < 400:
            raise BackendError("Substack returned an unexpected redirect")
        if status in (401, 403):
            raise AuthenticationExpired("Substack asked you to sign in again")
        if status < 200 or status >= 300:
            response.read(min(max_bytes, 64_000))
            raise BackendError(f"Substack returned HTTP {status}")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise BackendError("Substack returned more data than the safety limit")
        return status, url, response_headers, body
    except (http.client.HTTPException, ssl.SSLError, TimeoutError, OSError) as exc:
        raise BackendError("Could not securely reach the Substack publication") from exc
    finally:
        connection.close()


def validate_request_target(url: str, allowed_hosts: set[str], cookies: dict[str, str] | None) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BackendError("The remote address is invalid") from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or hostname not in allowed_hosts
    ):
        raise BackendError("The remote address is outside the permitted Substack origin")
    if cookies and hostname != "substack.com":
        raise BackendError("The Substack session may only be sent to substack.com")


def request(
    url: str,
    *,
    allowed_hosts: set[str],
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    max_bytes: int = MAX_JSON_BYTES,
    timeout: int = 20,
    pinned_addresses: tuple[str, ...] = (),
) -> tuple[int, str, dict[str, str], bytes]:
    validate_request_target(url, allowed_hosts, cookies)
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1",
    }
    request_headers.update(headers or {})
    if cookies:
        request_headers["Cookie"] = cookie_header(cookies)
    if pinned_addresses:
        if cookies:
            raise BackendError("Authentication cannot be sent through a pinned publication connection")
        return pinned_https_request(
            url,
            hostname=(urllib.parse.urlsplit(url).hostname or "").lower().rstrip("."),
            addresses=pinned_addresses,
            headers=request_headers,
            max_bytes=max_bytes,
            timeout=timeout,
        )
    req = urllib.request.Request(url, headers=request_headers)
    try:
        response = HTTP_OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, exc.geturl(), dict(exc.headers), b""
        body = exc.read(min(max_bytes, 64_000))
        if exc.code in (401, 403):
            raise AuthenticationExpired("Substack asked you to sign in again") from exc
        raise BackendError(f"Substack returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise BackendError("Could not reach Substack") from exc

    with response:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise BackendError("Substack returned more data than the safety limit")
        return response.status, response.geturl(), dict(response.headers), body


def request_json(path: str, cookies: dict[str, str]) -> dict[str, Any]:
    status, _, _, body = request(SUBSTACK_ORIGIN + path, allowed_hosts={"substack.com"}, cookies=cookies)
    if status != 200:
        raise BackendError(f"Unexpected Substack response ({status})")
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise BackendError("Substack returned invalid account data") from exc
    if not isinstance(value, dict):
        raise BackendError("Substack returned an unexpected account response")
    return value


def bounded_scalar(value: Any, max_chars: int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    source = str(value)
    return source[:max_chars]


def bounded_clean_text(value: Any, limit: int, *, source_limit: int = 4_096) -> str:
    return clean_text(bounded_scalar(value, source_limit), limit)


def bounded_number(value: Any, *, minimum: float = 0, maximum: float = 4_102_444_800) -> float:
    if isinstance(value, bool):
        return minimum
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return minimum
    if not math.isfinite(number):
        return minimum
    return min(maximum, max(minimum, number))


def subdomain_is_safe(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value.lower()))


def parse_publications(data: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Return (recognized response shape, normalized publications)."""
    payload = data.get("result") if isinstance(data.get("result"), dict) else data
    raw_subscriptions = payload.get("subscriptions")
    if raw_subscriptions is None and isinstance(payload.get("items"), list):
        raw_subscriptions = payload.get("items")
    if not isinstance(raw_subscriptions, list):
        return False, []
    if len(raw_subscriptions) > MAX_SUBSCRIPTIONS:
        raise BackendError("Substack returned too many subscriptions")

    lookup: dict[str, dict[str, Any]] = {}
    raw_publications = payload.get("publications", [])
    if isinstance(payload.get("publicationMap"), dict):
        if len(payload["publicationMap"]) > MAX_PUBLICATIONS:
            raise BackendError("Substack returned too many publications")
        raw_publications = list(payload["publicationMap"].values())
    if not isinstance(raw_publications, list):
        raw_publications = []
    if len(raw_publications) > MAX_PUBLICATIONS:
        raise BackendError("Substack returned too many publications")
    for publication in raw_publications:
        if isinstance(publication, dict) and publication.get("id") is not None:
            publication_id = bounded_scalar(publication.get("id"), 128)
            if publication_id:
                lookup[publication_id] = publication

    raw_publication_users = payload.get("publicationUsers", [])
    if not isinstance(raw_publication_users, list):
        raw_publication_users = []
    if len(raw_publication_users) > MAX_PUBLICATION_USERS:
        raise BackendError("Substack returned too many publication memberships")
    owned_publication_ids = {
        bounded_scalar(link.get("publication_id"), 128)
        for link in raw_publication_users
        if isinstance(link, dict)
        and link.get("publication_id") is not None
        and (
            link.get("is_primary") is True
            or bounded_scalar(link.get("role"), 32).lower() == "admin"
        )
    }

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for subscription in raw_subscriptions:
        if not isinstance(subscription, dict):
            continue
        publication = subscription.get("publication") or subscription.get("pub")
        if not isinstance(publication, dict):
            publication = lookup.get(bounded_scalar(subscription.get("publication_id"), 128), {})
        subdomain = bounded_scalar(publication.get("subdomain"), 63).strip().lower()
        if not subdomain_is_safe(subdomain) or subdomain in seen:
            continue
        seen.add(subdomain)
        publication_id = publication.get("id") or subscription.get("publication_id")
        persisted_publication_id = bounded_scalar(publication_id, 128)
        owned = persisted_publication_id in owned_publication_ids
        custom_domain = bounded_scalar(publication.get("custom_domain"), 253).strip().lower()
        custom_url = safe_article_url(f"https://{custom_domain}") if custom_domain else ""
        custom_parsed = urllib.parse.urlsplit(custom_url) if custom_url else None
        custom_host = (custom_parsed.hostname or "").lower() if custom_parsed else ""
        if custom_parsed and (
            custom_domain.rstrip(".") != custom_host
            or custom_parsed.path not in ("", "/")
            or custom_parsed.query
            or custom_parsed.fragment
        ):
            custom_url = ""
            custom_host = ""
        display_url = custom_url or f"https://{subdomain}.substack.com"
        canonical_feed_url = f"https://{subdomain}.substack.com/feed"
        membership = bounded_clean_text(
            subscription.get("membership_state") or subscription.get("type") or "subscribed",
            64,
            source_limit=128,
        )
        normalized.append(
            {
                "id": subdomain,
                "publication_id": persisted_publication_id,
                "name": bounded_clean_text(publication.get("name") or subdomain, 220),
                "author": bounded_clean_text(publication.get("author_name") or publication.get("author"), 120),
                "description": bounded_clean_text(publication.get("description"), 220),
                "logo_url": safe_image_url(bounded_scalar(publication.get("logo_url"), 4_096)),
                "author_photo_url": safe_image_url(bounded_scalar(publication.get("author_photo_url"), 4_096)),
                "subdomain": subdomain,
                "custom_domain": custom_host,
                "url": display_url,
                # Substack redirects a publication's canonical feed to its
                # custom domain. Go directly to the authenticated account
                # metadata's domain so the generic HTTP client can continue
                # rejecting every redirect.
                "feed_url": f"https://{custom_host}/feed" if custom_host else canonical_feed_url,
                "membership": membership,
                "owned": owned,
            }
        )
    return True, normalized


def fetch_subscriptions(cookies: dict[str, str]) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    recognized_empty: list[dict[str, Any]] | None = None
    for endpoint in SUBSCRIPTIONS_ENDPOINTS:
        try:
            data = request_json(endpoint, cookies)
            recognized, publications = parse_publications(data)
            if not recognized:
                continue
            had_publications = bool(publications)
            if not load_config().get("include_owned", False):
                publications = [publication for publication in publications if not publication.get("owned")]
            if publications or had_publications:
                return publications
            recognized_empty = []
        except AuthenticationExpired:
            raise
        except BackendError as exc:
            last_error = exc
    if recognized_empty is not None:
        return recognized_empty
    if last_error:
        raise BackendError(str(last_error))
    raise BackendError("Substack's subscription response has changed")


def fetch_profile(cookies: dict[str, str]) -> dict[str, Any]:
    try:
        data = request_json(PROFILE_ENDPOINT, cookies)
    except BackendError:
        return {}
    return {
        "name": bounded_clean_text(data.get("name") or data.get("handle") or "Substack reader", 160),
        "handle": bounded_clean_text(data.get("handle"), 80),
        "photo_url": safe_image_url(bounded_scalar(data.get("photo_url"), 4_096)),
    }


class PlainTextHTMLParser(HTMLParser):
    """Extract display text while discarding active and styling elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"script", "style"}:
            self.ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def clean_text(value: str, limit: int = 260) -> str:
    parser = PlainTextHTMLParser()
    try:
        parser.feed(value)
        parser.close()
        text = " ".join(parser.parts)
    except (AssertionError, ValueError):
        # Malformed feed markup should degrade to a safe empty excerpt rather
        # than leak tag contents or interrupt the rest of the RSS update.
        text = ""
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def parse_date(value: str) -> tuple[str, float]:
    source = str(value or "").strip()
    if not source:
        return "", 0
    parsed: dt.datetime | None = None
    try:
        parsed = email.utils.parsedate_to_datetime(source)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = dt.datetime.fromisoformat(source.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is None:
        return source, 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    parsed = parsed.astimezone(dt.timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z"), parsed.timestamp()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def first_child_text(node: ET.Element, names: set[str]) -> str:
    for child in list(node):
        if local_name(child.tag) in names and child.text:
            return child.text.strip()
    return ""


def safe_article_url(value: str) -> str:
    source = html.unescape(str(value or "").strip())
    if len(source) > 4096:
        return ""
    try:
        parsed = urllib.parse.urlsplit(source)
        port = parsed.port
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or not hostname_is_public_reference(hostname)
    ):
        return ""
    return urllib.parse.urlunsplit(parsed)


def hostname_is_public_reference(hostname: str) -> bool:
    value = hostname.lower().rstrip(".")
    if value == "localhost" or value.endswith((".localhost", ".local", ".internal", ".lan", ".home")):
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        labels = value.split(".")
        return len(labels) >= 2 and len(value) <= 253 and all(subdomain_is_safe(label) for label in labels)
    return address.is_global


def safe_image_url(value: str) -> str:
    safe = safe_article_url(value)
    if not safe:
        return ""
    hostname = (urllib.parse.urlsplit(safe).hostname or "").lower()
    allowed = (
        hostname == "substackcdn.com"
        or hostname.endswith(".substackcdn.com")
        or hostname == "substack-post-media.s3.amazonaws.com"
        or hostname == "bucketeer-e05bbc84-baa3-437e-9518-adb32be77984.s3.amazonaws.com"
    )
    return safe if allowed else ""


def bounded_header_value(value: Any, max_chars: int) -> str:
    candidate = bounded_scalar(value, max_chars)
    return "" if re.search(r"[\x00-\x1f\x7f]", candidate) else candidate


def normalize_subscription(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    subdomain = bounded_scalar(value.get("subdomain") or value.get("id"), 63).lower()
    if not subdomain_is_safe(subdomain):
        return None
    custom_domain = bounded_scalar(value.get("custom_domain"), 253).lower().rstrip(".")
    if custom_domain and not hostname_is_public_reference(custom_domain):
        custom_domain = ""
    canonical_url = f"https://{subdomain}.substack.com"
    display_url = f"https://{custom_domain}" if custom_domain else canonical_url
    feed_url = f"{display_url}/feed"
    seen_ids = value.get("seen_ids") if isinstance(value.get("seen_ids"), list) else []
    return {
        "id": subdomain,
        "publication_id": bounded_scalar(value.get("publication_id"), 128) or subdomain,
        "name": bounded_clean_text(value.get("name") or subdomain, 220),
        "author": bounded_clean_text(value.get("author"), 120),
        "description": bounded_clean_text(value.get("description"), 220),
        "logo_url": safe_image_url(bounded_scalar(value.get("logo_url"), 4_096)),
        "author_photo_url": safe_image_url(bounded_scalar(value.get("author_photo_url"), 4_096)),
        "subdomain": subdomain,
        "custom_domain": custom_domain,
        "url": display_url,
        "feed_url": feed_url,
        "membership": bounded_clean_text(value.get("membership") or "subscribed", 64, source_limit=128),
        "owned": value.get("owned") is True,
        "etag": bounded_header_value(value.get("etag"), 1_024),
        "last_modified": bounded_header_value(value.get("last_modified"), 256),
        "last_checked": bounded_number(value.get("last_checked")),
        "next_poll": bounded_number(value.get("next_poll")),
        "seen_ids": [
            item
            for item in (bounded_scalar(item, 24) for item in seen_ids[:MAX_SEEN_PER_PUBLICATION])
            if re.fullmatch(r"[0-9a-f]{24}", item)
        ],
        "error_count": int(bounded_number(value.get("error_count"), maximum=1_000)),
        "last_error": bounded_clean_text(value.get("last_error"), 180, source_limit=720),
    }


def normalize_article(value: Any, publication_ids: set[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    article_id = bounded_scalar(value.get("id"), 24)
    publication_id = bounded_scalar(value.get("publication_id"), 63).lower()
    link = safe_article_url(bounded_scalar(value.get("link"), 4_096))
    if (
        not re.fullmatch(r"[0-9a-f]{24}", article_id)
        or publication_id not in publication_ids
        or not link
    ):
        return None
    return {
        "id": article_id,
        "publication_id": publication_id,
        "publication": bounded_clean_text(value.get("publication") or publication_id, 220),
        "author": bounded_clean_text(value.get("author"), 120),
        "title": bounded_clean_text(value.get("title") or "Untitled post", 220),
        "link": link,
        "published": bounded_scalar(value.get("published"), 64),
        "published_ts": bounded_number(value.get("published_ts")),
        "excerpt": bounded_clean_text(value.get("excerpt"), 280, source_limit=1_120),
        "image_url": safe_image_url(bounded_scalar(value.get("image_url"), 4_096)),
        "publication_logo_url": safe_image_url(bounded_scalar(value.get("publication_logo_url"), 4_096)),
        "unread": value.get("unread") is True,
    }


def normalize_state(value: Any) -> dict[str, Any]:
    """Return only the bounded schema consumed by the daemon and QML panel."""
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value.get("schema") != 1:
        return default_state()
    raw_subscriptions = value.get("subscriptions") if isinstance(value.get("subscriptions"), list) else []
    subscriptions: list[dict[str, Any]] = []
    seen_publications: set[str] = set()
    for candidate in raw_subscriptions[:MAX_SUBSCRIPTIONS]:
        publication = normalize_subscription(candidate)
        if publication is None or publication["id"] in seen_publications:
            continue
        seen_publications.add(publication["id"])
        subscriptions.append(publication)

    raw_articles = value.get("articles") if isinstance(value.get("articles"), list) else []
    articles: list[dict[str, Any]] = []
    seen_articles: set[str] = set()
    for candidate in raw_articles[:MAX_ARTICLES]:
        article = normalize_article(candidate, seen_publications)
        if article is None or article["id"] in seen_articles:
            continue
        seen_articles.add(article["id"])
        articles.append(article)
    articles.sort(key=lambda item: item["published_ts"], reverse=True)

    raw_account = value.get("account") if isinstance(value.get("account"), dict) else {}
    status = bounded_scalar(value.get("status"), 32)
    if status not in {"starting", "syncing", "ready", "error", "expired", "disconnected"}:
        status = "starting"
    state = {
        "schema": 1,
        "status": status,
        "message": bounded_clean_text(value.get("message") or "Starting Substack…", 240, source_limit=960),
        "authenticated": value.get("authenticated") is True,
        "syncing": value.get("syncing") is True,
        "account": {
            "name": bounded_clean_text(raw_account.get("name"), 160),
            "handle": bounded_clean_text(raw_account.get("handle"), 80),
            "photo_url": safe_image_url(bounded_scalar(raw_account.get("photo_url"), 4_096)),
        },
        "subscriptions": subscriptions,
        "articles": articles,
        "unread_count": sum(1 for article in articles if article["unread"]),
        "last_sync": bounded_scalar(value.get("last_sync"), 64) or None,
        "last_subscription_sync": bounded_scalar(value.get("last_subscription_sync"), 64) or None,
        "subscription_sync_due": bounded_number(value.get("subscription_sync_due")),
        "empty_subscription_confirmations": int(
            bounded_number(value.get("empty_subscription_confirmations"), maximum=2)
        ),
        "last_error": bounded_clean_text(value.get("last_error"), 240, source_limit=960),
        "updated_at": bounded_scalar(value.get("updated_at"), 64) or iso_from_ts(),
    }
    return state


def resolve_substack_custom_domain(hostname: str) -> tuple[str, ...]:
    """Resolve once and return only public addresses routed through Substack."""
    host = str(hostname or "").lower().rstrip(".")
    if not hostname_is_public_reference(host):
        raise BackendError("The publication custom domain is not a public address")
    try:
        resolved = socket.getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM,
            flags=socket.AI_CANONNAME,
        )
    except OSError as exc:
        raise BackendError("The publication custom domain could not be resolved") from exc
    if not resolved:
        raise BackendError("The publication custom domain could not be resolved")

    canonical_names = {
        str(item[3] or "").lower().rstrip(".")
        for item in resolved
        if str(item[3] or "").strip()
    }
    if not any(name.endswith(SUBSTACK_CUSTOM_DOMAIN_SUFFIX) for name in canonical_names):
        raise BackendError("The publication custom domain is not routed through Substack")

    addresses: list[str] = []
    for item in resolved:
        try:
            address = ipaddress.ip_address(str(item[4][0]))
        except (IndexError, TypeError, ValueError) as exc:
            raise BackendError("The publication custom domain returned an invalid address") from exc
        if not address.is_global:
            raise BackendError("The publication custom domain resolved outside the public internet")
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise BackendError("The publication custom domain could not be resolved")
    return tuple(addresses)


def publication_feed_target(publication: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    """Return the exact URL, host, and DNS-pinned addresses for one feed."""
    feed_url = safe_article_url(str(publication.get("feed_url") or ""))
    if not feed_url:
        raise BackendError("The publication feed address is invalid")
    parsed = urllib.parse.urlsplit(feed_url)
    if parsed.path != "/feed" or parsed.query or parsed.fragment:
        raise BackendError("The publication feed address is invalid")

    subdomain = str(publication.get("subdomain") or publication.get("id") or "").lower()
    if not subdomain_is_safe(subdomain):
        raise BackendError("The publication feed identity is invalid")
    host = (parsed.hostname or "").lower().rstrip(".")
    canonical_host = f"{subdomain}.substack.com"
    if host == canonical_host:
        return feed_url, host, ()

    custom_domain = str(publication.get("custom_domain") or "").lower().rstrip(".")
    if not custom_domain or host != custom_domain:
        raise BackendError("The publication feed is outside its permitted domain")
    return feed_url, host, resolve_substack_custom_domain(custom_domain)


def response_header(headers: dict[str, str], name: str) -> str:
    expected = name.lower()
    for key, value in headers.items():
        if str(key).lower() == expected:
            return str(value)
    return ""


def validate_custom_feed_response(publication: dict[str, Any], headers: dict[str, str]) -> None:
    subdomain = str(publication.get("subdomain") or publication.get("id") or "").lower()
    if (
        response_header(headers, "X-Sub").lower() != subdomain
        or response_header(headers, "X-Served-By").lower() != "substack"
    ):
        raise BackendError("The custom domain did not identify the expected Substack publication")


def image_from_html(value: str) -> str:
    match = re.search(r"<img\b[^>]*\bsrc=[\"']([^\"']+)", value, flags=re.I)
    return safe_image_url(match.group(1)) if match else ""


def parse_feed(body: bytes, publication: dict[str, Any]) -> list[dict[str, Any]]:
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise BackendError("The feed contained a disallowed XML declaration")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise BackendError("The publication returned invalid RSS") from exc

    nodes: list[ET.Element] = []
    if local_name(root.tag) == "rss":
        channel = next((child for child in list(root) if local_name(child.tag) == "channel"), None)
        if channel is not None:
            nodes = [child for child in list(channel) if local_name(child.tag) == "item"]
    elif local_name(root.tag) == "feed":
        nodes = [child for child in list(root) if local_name(child.tag) == "entry"]

    parsed_items: list[dict[str, Any]] = []
    for node in nodes[:40]:
        title = bounded_clean_text(first_child_text(node, {"title"}), 220)
        link = first_child_text(node, {"link"})
        if not link:
            for child in list(node):
                if local_name(child.tag) == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        link = safe_article_url(link)
        if not title or not link:
            continue

        guid = bounded_scalar(first_child_text(node, {"guid", "id"}), 4_096) or link
        author = first_child_text(node, {"creator", "author"}) or publication.get("author", "")
        published_raw = bounded_scalar(first_child_text(node, {"pubdate", "published", "updated"}), 128)
        published, published_ts = parse_date(published_raw)
        raw_description = bounded_scalar(
            first_child_text(node, {"description", "summary", "encoded", "content"}),
            16_384,
        )
        image = ""
        for child in list(node):
            if local_name(child.tag) in {"enclosure", "thumbnail", "content"}:
                candidate = child.attrib.get("url", "")
                mime = child.attrib.get("type", "")
                if candidate and (not mime or mime.startswith("image/")):
                    image = safe_image_url(candidate)
                    if image:
                        break
        if not image:
            image = image_from_html(raw_description)
        subdomain = str(publication.get("subdomain") or publication.get("id") or "").lower()
        identity_feed_url = (
            f"https://{subdomain}.substack.com/feed"
            if subdomain_is_safe(subdomain)
            else str(publication["feed_url"])
        )
        identity = hashlib.sha256((identity_feed_url + "\0" + guid).encode("utf-8")).hexdigest()[:24]
        parsed_items.append(
            {
                "id": identity,
                "publication_id": publication["id"],
                "publication": publication["name"],
                "author": bounded_clean_text(author, 120),
                "title": title,
                "link": link,
                "published": published,
                "published_ts": published_ts,
                "excerpt": bounded_clean_text(raw_description, 280, source_limit=16_384),
                "image_url": image,
                "publication_logo_url": publication.get("logo_url", ""),
            }
        )

    parsed_items.sort(key=lambda item: item.get("published_ts", 0), reverse=True)
    return parsed_items[:20]


def adaptive_interval(latest_timestamp: float, publication_id: str) -> int:
    age = max(0, now_ts() - latest_timestamp) if latest_timestamp else 365 * 24 * 60 * 60
    if age <= 3 * 24 * 60 * 60:
        base = 20 * 60
    elif age <= 30 * 24 * 60 * 60:
        base = 60 * 60
    else:
        base = 6 * 60 * 60
    digest = int(hashlib.sha256(publication_id.encode()).hexdigest()[:4], 16)
    jitter = 0.85 + (digest / 65535) * 0.30
    return max(10 * 60, round(base * jitter))


def merge_feed(publication_id: str, fetched_items: list[dict[str, Any]], checked_at: float) -> list[dict[str, Any]]:
    new_for_notification: list[dict[str, Any]] = []

    def update(state: dict[str, Any]) -> None:
        nonlocal new_for_notification
        publication = next((item for item in state["subscriptions"] if item.get("id") == publication_id), None)
        if publication is None:
            return
        existing = {item.get("id"): item for item in state["articles"] if isinstance(item, dict)}
        seen = list(publication.get("seen_ids") or [])
        seen_set = set(seen)
        seeded = bool(publication.get("last_checked")) or bool(seen)

        merged: list[dict[str, Any]] = []
        for fetched in fetched_items:
            article = dict(fetched)
            prior = existing.get(article["id"])
            if prior:
                article["unread"] = bool(prior.get("unread"))
            else:
                article["unread"] = bool(seeded and article["id"] not in seen_set)
                if article["unread"]:
                    new_for_notification.append(article)
            merged.append(article)
            if article["id"] not in seen_set:
                seen.append(article["id"])
                seen_set.add(article["id"])

        fetched_ids = {article["id"] for article in fetched_items}
        for article in state["articles"]:
            if article.get("id") not in fetched_ids:
                merged.append(article)
        merged.sort(key=lambda item: item.get("published_ts", 0), reverse=True)
        state["articles"] = merged[:MAX_ARTICLES]
        publication["seen_ids"] = seen[-MAX_SEEN_PER_PUBLICATION:]
        publication["last_checked"] = checked_at
        publication["last_error"] = ""
        publication["error_count"] = 0
        latest = fetched_items[0].get("published_ts", 0) if fetched_items else 0
        publication["next_poll"] = checked_at + adaptive_interval(float(latest or 0), publication_id)
        state["last_sync"] = iso_from_ts(checked_at)
        state["last_error"] = ""

    mutate_state(update)
    return new_for_notification


def merge_not_modified(publication_id: str, checked_at: float) -> None:
    def update(state: dict[str, Any]) -> None:
        publication = next((item for item in state["subscriptions"] if item.get("id") == publication_id), None)
        if publication is None:
            return
        latest = max(
            (float(item.get("published_ts") or 0) for item in state["articles"] if item.get("publication_id") == publication_id),
            default=0,
        )
        publication["last_checked"] = checked_at
        publication["last_error"] = ""
        publication["error_count"] = 0
        publication["next_poll"] = checked_at + adaptive_interval(latest, publication_id)
        state["last_sync"] = iso_from_ts(checked_at)

    mutate_state(update)


def merge_feed_error(publication_id: str, message: str, checked_at: float) -> None:
    def update(state: dict[str, Any]) -> None:
        publication = next((item for item in state["subscriptions"] if item.get("id") == publication_id), None)
        if publication is None:
            return
        errors = int(publication.get("error_count") or 0) + 1
        publication["error_count"] = errors
        publication["last_error"] = message[:180]
        publication["next_poll"] = checked_at + min(6 * 60 * 60, (15 * 60) * (2 ** min(errors - 1, 5)))

    mutate_state(update)


def sync_publications(cookies: dict[str, str]) -> None:
    publications = fetch_subscriptions(cookies)
    profile = fetch_profile(cookies)
    stamp = now_ts()

    def update(state: dict[str, Any]) -> None:
        previous_publications = [item for item in state["subscriptions"] if isinstance(item, dict)]
        if not publications and previous_publications:
            confirmations = int(state.get("empty_subscription_confirmations") or 0) + 1
            state["empty_subscription_confirmations"] = confirmations
            if confirmations < 2:
                # The account API is undocumented. A single empty response can
                # be a transient or a shape change, so preserve the last good
                # queue and confirm it on a short follow-up before deleting it.
                state["authenticated"] = True
                state["account"] = profile
                state["last_subscription_sync"] = iso_from_ts(stamp)
                state["subscription_sync_due"] = stamp + EMPTY_SUBSCRIPTION_RECHECK_SECONDS
                state["status"] = "ready"
                state["syncing"] = False
                state["message"] = f"Following {len(previous_publications)} publications"
                state["last_error"] = (
                    "Substack returned an empty list; keeping the last good feed until it is confirmed."
                )
                return

        state["empty_subscription_confirmations"] = 0
        prior_by_id = {item.get("id"): item for item in state["subscriptions"] if isinstance(item, dict)}
        next_publications: list[dict[str, Any]] = []
        active_ids: set[str] = set()
        for publication in publications:
            active_ids.add(publication["id"])
            prior = prior_by_id.get(publication["id"], {})
            same_feed = prior.get("feed_url") == publication.get("feed_url")
            publication.update(
                {
                    "etag": prior.get("etag", "") if same_feed else "",
                    "last_modified": prior.get("last_modified", "") if same_feed else "",
                    "last_checked": prior.get("last_checked", 0),
                    "next_poll": prior.get("next_poll", 0) if same_feed else 0,
                    "seen_ids": prior.get("seen_ids", []),
                    "error_count": prior.get("error_count", 0) if same_feed else 0,
                    "last_error": prior.get("last_error", "") if same_feed else "",
                }
            )
            next_publications.append(publication)
        state["subscriptions"] = next_publications
        state["articles"] = [item for item in state["articles"] if item.get("publication_id") in active_ids]
        state["authenticated"] = True
        state["account"] = profile
        state["last_subscription_sync"] = iso_from_ts(stamp)
        state["subscription_sync_due"] = stamp + SUBSCRIPTION_SYNC_SECONDS
        state["status"] = "syncing"
        state["syncing"] = True
        state["message"] = f"Checking {len(next_publications)} publication{'s' if len(next_publications) != 1 else ''}…"
        state["last_error"] = ""

    mutate_state(update)


def send_desktop_notification(summary: Any, body: Any, exec_argv: list[str] | None = None) -> bool:
    """Send private display text over D-Bus, never through a process argument."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        hints = {
            "urgency": GLib.Variant("y", 1),
            "omarchy-glyph": GLib.Variant("s", "󰂺"),
        }
        if exec_argv:
            bounded_argv = [bounded_scalar(argument, 4_096) for argument in exec_argv[:8]]
            hints["omarchy-exec-argv"] = GLib.Variant(
                "s",
                json.dumps(bounded_argv, ensure_ascii=False, separators=(",", ":")),
            )
        parameters = GLib.Variant(
            "(susssasa{sv}i)",
            (
                "Substack",
                0,
                "",
                bounded_clean_text(summary or "New Substack post", 220),
                bounded_clean_text(body or "Substack", 220),
                [],
                hints,
                -1,
            ),
        )
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        connection.call_sync(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "Notify",
            parameters,
            GLib.VariantType("(u)"),
            Gio.DBusCallFlags.NONE,
            8_000,
            None,
        )
        return True
    except Exception:
        # Notifications are advisory; feed synchronization must continue if
        # the desktop bus or notification service is temporarily unavailable.
        return False


def send_notifications(articles: list[dict[str, Any]]) -> None:
    if not articles or not load_config().get("notify", True):
        return
    for article in articles[:3]:
        send_desktop_notification(
            article.get("title", "New Substack post"),
            article.get("publication", "Substack"),
            ["python3", str(SCRIPT_PATH), "open", bounded_scalar(article.get("id"), 24)],
        )
    if len(articles) > 3:
        send_desktop_notification(
            f"{min(len(articles) - 3, MAX_ARTICLES)} more new posts",
            "Open the Substack panel to see them.",
        )


class FeedDaemon:
    def __init__(self) -> None:
        self.running = True
        self.cookies = secret_lookup()

    def stop(self, *_: Any) -> None:
        self.running = False

    def force_refresh(self) -> None:
        self.cookies = secret_lookup()

        def update(state: dict[str, Any]) -> None:
            state["subscription_sync_due"] = 0
            for publication in state["subscriptions"]:
                publication["next_poll"] = 0
            state["syncing"] = True
            state["status"] = "syncing"
            state["message"] = "Refreshing your Substack feed…"

        mutate_state(update)

    def mark_auth_expired(self) -> None:
        secret_clear()
        self.cookies = {}

        def update(state: dict[str, Any]) -> None:
            state["authenticated"] = False
            state["status"] = "expired"
            state["syncing"] = False
            state["message"] = "Reconnect Substack to update your subscriptions."
            state["last_error"] = "Your Substack session expired. Existing RSS feeds will keep updating."

        mutate_state(update)

    def subscription_sync(self) -> bool:
        if not self.cookies:
            return False
        state = load_state()
        if float(state.get("subscription_sync_due") or 0) > now_ts():
            return False

        mutate_state(
            lambda value: value.update(
                {"status": "syncing", "syncing": True, "message": "Updating your subscriptions…", "last_error": ""}
            )
        )
        try:
            sync_publications(self.cookies)
        except AuthenticationExpired:
            self.mark_auth_expired()
        except BackendError as exc:
            def fail(value: dict[str, Any]) -> None:
                value["subscription_sync_due"] = now_ts() + 15 * 60
                value["last_error"] = str(exc)
                value["syncing"] = False
                value["status"] = "error"
                value["message"] = "Subscription sync will retry shortly."

            mutate_state(fail)
        return True

    def next_due_publication(self) -> dict[str, Any] | None:
        state = load_state()
        due = [item for item in state["subscriptions"] if float(item.get("next_poll") or 0) <= now_ts()]
        due.sort(key=lambda item: float(item.get("next_poll") or 0))
        return due[0] if due else None

    def poll_publication(self, publication: dict[str, Any]) -> None:
        publication_id = str(publication["id"])
        checked = now_ts()
        headers = {}
        if publication.get("etag"):
            headers["If-None-Match"] = str(publication["etag"])
        if publication.get("last_modified"):
            headers["If-Modified-Since"] = str(publication["last_modified"])

        mutate_state(
            lambda state: state.update(
                {
                    "status": "syncing",
                    "syncing": True,
                    "message": f"Checking {publication.get('name', publication_id)}…",
                }
            )
        )
        try:
            feed_url, expected_feed_host, pinned_addresses = publication_feed_target(publication)
            status, _, response_headers, body = request(
                feed_url,
                allowed_hosts={expected_feed_host},
                headers=headers,
                max_bytes=MAX_FEED_BYTES,
                timeout=20,
                pinned_addresses=pinned_addresses,
            )
            if pinned_addresses:
                validate_custom_feed_response(publication, response_headers)
            if status == 304:
                merge_not_modified(publication_id, checked)
                return
            items = parse_feed(body, publication)
            new_articles = merge_feed(publication_id, items, checked)
            etag = bounded_header_value(response_headers.get("ETag"), 1_024)
            last_modified = bounded_header_value(response_headers.get("Last-Modified"), 256)
            if etag or last_modified:
                def store_cache_validators(state: dict[str, Any]) -> None:
                    target = next((item for item in state["subscriptions"] if item.get("id") == publication_id), None)
                    if target is not None:
                        if etag:
                            target["etag"] = etag
                        if last_modified:
                            target["last_modified"] = last_modified
                mutate_state(store_cache_validators)
            send_notifications(new_articles)
        except BackendError as exc:
            merge_feed_error(publication_id, str(exc), checked)

    def settle_status(self) -> None:
        def update(state: dict[str, Any]) -> None:
            state["syncing"] = False
            if self.cookies:
                state["authenticated"] = True
                state["status"] = "ready"
                count = len(state["subscriptions"])
                state["message"] = f"Following {count} publication{'s' if count != 1 else ''}"
                if not state.get("last_error"):
                    state["last_error"] = ""
            elif state["subscriptions"]:
                state["authenticated"] = False
                state["status"] = "expired"
                state["message"] = "RSS is updating; reconnect to sync subscriptions."
            else:
                state["authenticated"] = False
                state["status"] = "disconnected"
                state["message"] = "Connect Substack to build your feed."

        mutate_state(update)

    def run_locked(self) -> int:
        mutate_state(lambda state: state.update({"status": "starting", "message": "Starting Substack…"}))
        idle_rounds = 0
        while self.running:
            worked = False
            if consume_refresh_request():
                self.force_refresh()
                worked = True

            if self.subscription_sync():
                worked = True

            publication = self.next_due_publication()
            if publication is not None:
                self.poll_publication(publication)
                worked = True
                time.sleep(0.75)
            else:
                self.settle_status()

            idle_rounds = 0 if worked else idle_rounds + 1
            time.sleep(0.35 if worked else min(3.0, 0.5 + idle_rounds * 0.25))
        return 0

    def run(self) -> int:
        ensure_dirs()
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        if not private_file_exists(STATE_FILE, max_bytes=MAX_STATE_BYTES):
            atomic_json(STATE_FILE, default_state(), max_bytes=MAX_STATE_BYTES)
        try:
            with locked(DAEMON_LOCK, blocking=False):
                return self.run_locked()
        except BlockingIOError:
            return 0


def touch_refresh() -> None:
    with state_directory() as directory:
        descriptor = _open_private_file(
            directory,
            _state_filename(REFRESH_REQUEST),
            os.O_WRONLY | os.O_TRUNC,
            create=True,
            max_bytes=0,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory)


def command_open(article_id: str) -> int:
    if not re.fullmatch(r"[0-9a-f]{24}", article_id):
        return 2
    target: dict[str, str] = {}

    def update(state: dict[str, Any]) -> None:
        for article in state["articles"]:
            if article.get("id") == article_id:
                article["unread"] = False
                target["url"] = safe_article_url(str(article.get("link") or ""))
                break

    mutate_state(update)
    if not target.get("url"):
        return 1
    subprocess.Popen(
        ["omarchy", "launch", "browser", target["url"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return 0


def command_mark_read(article_id: str | None = None) -> int:
    def update(state: dict[str, Any]) -> None:
        for article in state["articles"]:
            if article_id is None or article.get("id") == article_id:
                article["unread"] = False

    mutate_state(update)
    return 0


def command_disconnect() -> int:
    secret_clear()

    def update(state: dict[str, Any]) -> None:
        fresh = default_state()
        fresh.update({"status": "disconnected", "message": "Connect Substack to build your feed."})
        state.clear()
        state.update(fresh)

    mutate_state(update)
    touch_refresh()
    return 0


def verify_cookies(cookies: dict[str, str]) -> bool:
    try:
        request_json(PROFILE_ENDPOINT, cookies)
        return True
    except BackendError:
        return False


def magic_link_allowed(value: str) -> bool:
    source = str(value or "").strip()
    if len(source) > 8192:
        return False
    try:
        parsed = urllib.parse.urlsplit(source)
        port = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        return False
    if hostname not in AUTH_TOP_LEVEL_HOSTS:
        return False
    query = urllib.parse.parse_qs(parsed.query)
    tokens = query.get("token") or []
    return (
        parsed.path.rstrip("/") == "/sign-in"
        and len(tokens) == 1
        and 0 < len(tokens[0]) <= 4_096
    )


def auth_navigation_allowed(value: str) -> bool:
    source = str(value or "").strip()
    if len(source) > 8192:
        return False
    try:
        parsed = urllib.parse.urlsplit(source)
        port = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        return False
    return hostname in AUTH_TOP_LEVEL_HOSTS


def _auth_window_unlocked() -> int:
    """Run Substack's own login page in a separate ephemeral WebKit window."""
    # WebKitGTK's DMA-BUF renderer currently trips a Wayland protocol error on
    # this Omarchy/Hyprland stack and closes the window as soon as it appears.
    # The shared-memory renderer is visually identical for a sign-in page and
    # avoids that compositor-specific crash.
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("WebKit2", "4.1")
        from gi.repository import Gdk, GLib, Gtk, WebKit2
    except (ImportError, ValueError) as exc:
        print(f"Substack sign-in needs GTK WebKit: {exc}", file=sys.stderr)
        return 1

    class Login:
        def __init__(self) -> None:
            self.finished = False
            self.checking = False
            self.password_requested = False
            self.window = Gtk.Window(title="Connect Substack")
            self.window.set_default_size(1040, 760)
            self.window.set_position(Gtk.WindowPosition.CENTER)
            self.window.connect("destroy", lambda *_: Gtk.main_quit())

            header = Gtk.HeaderBar()
            header.set_show_close_button(True)
            header.props.title = "Connect Substack"
            header.props.subtitle = "substack.com · temporary private session"
            self.header = header

            back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
            back_button.set_tooltip_text("Back / start over")
            back_button.connect("clicked", self.go_back)
            header.pack_start(back_button)
            self.back_button = back_button

            reload_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
            reload_button.set_tooltip_text("Reload Substack")
            reload_button.connect("clicked", lambda *_: self.webview.reload())
            header.pack_start(reload_button)

            paste_button = Gtk.Button(label="Paste email link")
            paste_button.set_tooltip_text("Open a copied Substack magic link in this secure window")
            paste_button.connect("clicked", self.paste_magic_link)
            header.pack_end(paste_button)

            password_button = Gtk.Button(label="Use password")
            password_button.set_tooltip_text("Skip email delivery and show Substack's password form")
            password_button.connect("clicked", self.use_password)
            header.pack_end(password_button)
            self.window.set_titlebar(header)

            overlay = Gtk.Overlay()
            self.context = WebKit2.WebContext.new_ephemeral()
            self.webview = WebKit2.WebView.new_with_context(self.context)
            self.webview.get_settings().set_property("enable-developer-extras", False)
            self.webview.connect("load-changed", self.on_load_changed)
            self.webview.connect("load-failed", self.on_load_failed)
            self.webview.connect("decide-policy", self.on_decide_policy)
            self.webview.connect("permission-request", self.on_permission_request)
            overlay.add(self.webview)

            self.banner = Gtk.Label(label="")
            self.banner.set_halign(Gtk.Align.CENTER)
            self.banner.set_valign(Gtk.Align.END)
            self.banner.set_margin_bottom(24)
            self.banner.get_style_context().add_class("title")
            overlay.add_overlay(self.banner)
            self.window.add(overlay)

            manager = self.context.get_cookie_manager()
            # Substack's sign-in is protected by Cloudflare. Its challenge may
            # use a third-party cookie even though the eventual session is a
            # first-party substack.com cookie. The entire context is ephemeral,
            # so allowing it here does not leak into the user's main browser or
            # survive this one-purpose window.
            manager.set_accept_policy(WebKit2.CookieAcceptPolicy.ALWAYS)
            self.cookie_manager = manager
            self.webview.load_uri("https://substack.com/sign-in?redirect=%2Flibrary")
            GLib.timeout_add(1200, self.poll)

        def on_load_changed(self, _view: Any, event: Any) -> None:
            self.back_button.set_sensitive(True)
            current_uri = str(self.webview.get_uri() or "")
            if current_uri == "about:blank":
                self.header.props.subtitle = "No remote origin · temporary private session"
            elif auth_navigation_allowed(current_uri):
                hostname = urllib.parse.urlsplit(current_uri).hostname or "substack.com"
                self.header.props.subtitle = f"{hostname} · temporary private session"
            else:
                self.webview.stop_loading()
                self.header.props.subtitle = "Blocked origin · temporary private session"
                self.banner.set_text("Blocked navigation outside the permitted Substack sign-in hosts")
                return
            if event == WebKit2.LoadEvent.FINISHED:
                if self.password_requested:
                    self.password_requested = False
                    GLib.timeout_add(100, self.activate_password_form)
                self.poll()

        def on_decide_policy(self, _view: Any, decision: Any, decision_type: Any) -> bool:
            if (
                decision_type != WebKit2.PolicyDecisionType.NAVIGATION_ACTION
                and decision_type != WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION
            ):
                return False
            try:
                uri = str(decision.get_request().get_uri() or "")
            except (AttributeError, GLib.Error):
                decision.ignore()
                self.banner.set_text("Blocked an invalid navigation request")
                return True
            if auth_navigation_allowed(uri) and decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
                return False
            if auth_navigation_allowed(uri) and decision_type == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                decision.ignore()
                self.webview.load_uri(uri)
                return True
            decision.ignore()
            self.header.props.subtitle = "Blocked origin · temporary private session"
            self.banner.set_text("Blocked navigation outside the permitted Substack sign-in hosts")
            return True

        def on_permission_request(self, _view: Any, request: Any) -> bool:
            request.deny()
            return True

        def on_load_failed(self, _view: Any, _event: Any, _uri: str, error: Any) -> bool:
            self.banner.set_text("Substack could not load: " + bounded_clean_text(error.message, 180))
            return False

        def go_back(self, *_: Any) -> None:
            if self.webview.can_go_back():
                self.webview.go_back()
            else:
                self.webview.load_uri("https://substack.com/sign-in?redirect=%2Flibrary")

        def use_password(self, *_: Any) -> None:
            # The password switch on Substack's page is a JavaScript-only link,
            # so there is no stable URL to open directly. Always return to the
            # canonical sign-in page, then activate Substack's own control.
            self.password_requested = True
            self.banner.set_text("Opening password sign-in…")
            self.webview.load_uri("https://substack.com/sign-in?redirect=%2Flibrary")

        def activate_password_form(self) -> bool:
            script = """
                (() => {
                    const link = Array.from(document.querySelectorAll('a')).find((element) =>
                        /sign in with\\s*password/i.test(element.textContent || '')
                    );
                    if (!link) return false;
                    link.click();
                    return true;
                })()
            """
            self.webview.run_javascript(script, None, None, None)
            self.banner.set_text("")
            return False

        def paste_magic_link(self, *_: Any) -> None:
            dialog = Gtk.Dialog(
                title="Paste Substack email link",
                transient_for=self.window,
                flags=Gtk.DialogFlags.MODAL,
            )
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("Open link", Gtk.ResponseType.OK)
            content = dialog.get_content_area()
            content.set_spacing(10)
            content.set_border_width(16)
            label = Gtk.Label(
                label="Copy the sign-in link from Substack’s email, then paste it here.\n"
                "It opens only inside this temporary Substack window."
            )
            label.set_xalign(0)
            entry = Gtk.Entry()
            entry.set_placeholder_text("https://substack.com/sign-in?token=…")
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard_text = clipboard.wait_for_text()
            if clipboard_text and magic_link_allowed(clipboard_text):
                entry.set_text(clipboard_text)
            content.add(label)
            content.add(entry)
            dialog.show_all()
            response = dialog.run()
            link = entry.get_text().strip()
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                return
            if not magic_link_allowed(link):
                self.banner.set_text("That is not a valid Substack sign-in link")
                return
            self.banner.set_text("Opening your Substack sign-in link…")
            self.webview.load_uri(link)

        def poll(self) -> bool:
            if self.finished or self.checking:
                return not self.finished
            self.checking = True
            self.cookie_manager.get_cookies("https://substack.com", None, self.cookies_ready)
            return True

        def cookies_ready(self, manager: Any, result: Any) -> None:
            self.checking = False
            try:
                cookies = manager.get_cookies_finish(result)
            except GLib.Error:
                return
            session = {}
            for cookie in cookies or []:
                name = cookie.get_name()
                if name in {"connect.sid", "substack.sid"} and cookie.get_value():
                    session[name] = cookie.get_value()
            if not session or not verify_cookies(session):
                return
            try:
                secret_store(session)
                touch_refresh()
            except BackendError as exc:
                self.banner.set_text(str(exc))
                return
            self.finished = True
            self.banner.set_text("Connected — your feed is syncing now")
            GLib.timeout_add(900, self.close)

        def close(self) -> bool:
            self.window.destroy()
            return False

        def run(self) -> int:
            self.window.show_all()
            Gtk.main()
            return 0 if self.finished else 1

    return Login().run()


def auth_window() -> int:
    try:
        with locked(AUTH_LOCK, blocking=False):
            return _auth_window_unlocked()
    except BlockingIOError:
        print("A Substack sign-in window is already open", file=sys.stderr)
        return 3


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def command_config(key: str, value: str) -> int:
    next_value = parse_bool(value)
    config = load_config()
    if config.get(key) == next_value:
        return 0
    save_config({key: next_value})
    if key == "include_owned":
        # Subscription membership has to be rebuilt so owned publications and
        # their cached posts disappear (or return) as one atomic state change.
        touch_refresh()
    return 0


def command_snapshot() -> int:
    payload = json.dumps(load_state(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_STATE_BYTES:
        raise BackendError("The local feed snapshot exceeds its safety limit")
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.write(b"\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Omarchy Substack feed backend")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("daemon")
    subparsers.add_parser("auth-window")
    subparsers.add_parser("refresh")
    subparsers.add_parser("mark-all-read")
    subparsers.add_parser("disconnect")
    subparsers.add_parser("status")
    subparsers.add_parser("snapshot")
    open_parser = subparsers.add_parser("open")
    open_parser.add_argument("article_id")
    read_parser = subparsers.add_parser("mark-read")
    read_parser.add_argument("article_id")
    config_parser = subparsers.add_parser("config")
    config_parser.add_argument("key", choices=("notify", "include_owned"))
    config_parser.add_argument("value")
    args = parser.parse_args(argv)

    ensure_dirs()
    if args.command == "daemon":
        return FeedDaemon().run()
    if args.command == "auth-window":
        return auth_window()
    if args.command == "refresh":
        touch_refresh()
        return 0
    if args.command == "open":
        return command_open(args.article_id)
    if args.command == "mark-read":
        return command_mark_read(args.article_id)
    if args.command == "mark-all-read":
        return command_mark_read()
    if args.command == "disconnect":
        return command_disconnect()
    if args.command == "status":
        print(json.dumps(load_state(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "snapshot":
        return command_snapshot()
    if args.command == "config":
        return command_config(args.key, args.value)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
