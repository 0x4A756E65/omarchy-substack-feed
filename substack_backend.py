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
import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
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
MAX_JSON_BYTES = 8_000_000
MAX_ARTICLES = 160
MAX_SEEN_PER_PUBLICATION = 240
SUBSCRIPTION_SYNC_SECONDS = 12 * 60 * 60
EMPTY_SUBSCRIPTION_RECHECK_SECONDS = 15 * 60
MAX_SECRET_BYTES = 64_000
MAX_COOKIE_VALUE_BYTES = 16_384
SUBSTACK_CUSTOM_DOMAIN_SUFFIX = ".substack-custom-domains.com"

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


def ensure_dirs() -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(STATE_ROOT, 0o700)


@contextlib.contextmanager
def locked(path: Path):
    ensure_dirs()
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        with contextlib.suppress(OSError):
            os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path: Path, fallback: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except (OSError, json.JSONDecodeError, TypeError):
        return fallback


def atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    ensure_dirs()
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def load_state() -> dict[str, Any]:
    value = read_json(STATE_FILE, default_state())
    if not isinstance(value, dict):
        return default_state()
    base = default_state()
    base.update(value)
    if not isinstance(base.get("subscriptions"), list):
        base["subscriptions"] = []
    if not isinstance(base.get("articles"), list):
        base["articles"] = []
    return base


def mutate_state(mutator: Callable[[dict[str, Any]], Any]) -> Any:
    with locked(STATE_LOCK):
        before = load_state()
        state = json.loads(json.dumps(before))
        result = mutator(state)
        state["unread_count"] = sum(1 for article in state.get("articles", []) if article.get("unread"))
        if state != before:
            state["updated_at"] = iso_from_ts()
            atomic_json(STATE_FILE, state)
        return result


def load_config() -> dict[str, Any]:
    value = read_json(CONFIG_FILE, default_config())
    base = default_config()
    if isinstance(value, dict):
        base.update(value)
    base["notify"] = base.get("notify") is not False
    base["include_owned"] = base.get("include_owned") is True
    return base


def save_config(changes: dict[str, Any]) -> None:
    # Multiple panel instances can propagate settings at once on a shell
    # reload. Merge under a process lock so one toggle never erases another.
    with locked(CONFIG_LOCK):
        config = load_config()
        config.update(changes)
        atomic_json(CONFIG_FILE, config)


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
) -> tuple[int, str, dict[str, str], bytes]:
    validate_request_target(url, allowed_hosts, cookies)
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1",
    }
    request_headers.update(headers or {})
    if cookies:
        request_headers["Cookie"] = cookie_header(cookies)
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

    lookup: dict[str, dict[str, Any]] = {}
    raw_publications = payload.get("publications", [])
    if isinstance(payload.get("publicationMap"), dict):
        raw_publications = list(payload["publicationMap"].values())
    for publication in raw_publications:
        if isinstance(publication, dict) and publication.get("id") is not None:
            lookup[str(publication.get("id"))] = publication

    owned_publication_ids = {
        str(link.get("publication_id"))
        for link in payload.get("publicationUsers", [])
        if isinstance(link, dict)
        and link.get("publication_id") is not None
        and (link.get("is_primary") is True or str(link.get("role") or "").lower() == "admin")
    }

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for subscription in raw_subscriptions:
        if not isinstance(subscription, dict):
            continue
        publication = subscription.get("publication") or subscription.get("pub")
        if not isinstance(publication, dict):
            publication = lookup.get(str(subscription.get("publication_id")), {})
        subdomain = str(publication.get("subdomain") or "").strip().lower()
        if not subdomain_is_safe(subdomain) or subdomain in seen:
            continue
        seen.add(subdomain)
        publication_id = publication.get("id") or subscription.get("publication_id")
        owned = str(publication_id) in owned_publication_ids
        custom_domain = str(publication.get("custom_domain") or "").strip().lower()
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
        membership = str(subscription.get("membership_state") or subscription.get("type") or "subscribed")
        normalized.append(
            {
                "id": subdomain,
                "publication_id": publication_id,
                "name": str(publication.get("name") or subdomain),
                "author": str(publication.get("author_name") or publication.get("author") or ""),
                "description": clean_text(str(publication.get("description") or ""), 220),
                "logo_url": safe_image_url(str(publication.get("logo_url") or "")),
                "author_photo_url": safe_image_url(str(publication.get("author_photo_url") or "")),
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
        "name": str(data.get("name") or data.get("handle") or "Substack reader"),
        "handle": str(data.get("handle") or ""),
        "photo_url": safe_image_url(str(data.get("photo_url") or "")),
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


def validate_substack_custom_domain(hostname: str) -> None:
    """Require a public DNS route through Substack's custom-domain service."""
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

    for item in resolved:
        try:
            address = ipaddress.ip_address(str(item[4][0]))
        except (IndexError, TypeError, ValueError) as exc:
            raise BackendError("The publication custom domain returned an invalid address") from exc
        if not address.is_global:
            raise BackendError("The publication custom domain resolved outside the public internet")


def publication_feed_target(publication: dict[str, Any]) -> tuple[str, str, bool]:
    """Return (URL, exact allowed host, is custom domain) for one feed."""
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
        return feed_url, host, False

    custom_domain = str(publication.get("custom_domain") or "").lower().rstrip(".")
    if not custom_domain or host != custom_domain:
        raise BackendError("The publication feed is outside its permitted domain")
    validate_substack_custom_domain(custom_domain)
    return feed_url, host, True


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
        title = clean_text(first_child_text(node, {"title"}), 220)
        link = first_child_text(node, {"link"})
        if not link:
            for child in list(node):
                if local_name(child.tag) == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        link = safe_article_url(link)
        if not title or not link:
            continue

        guid = first_child_text(node, {"guid", "id"}) or link
        author = first_child_text(node, {"creator", "author"}) or publication.get("author", "")
        published_raw = first_child_text(node, {"pubdate", "published", "updated"})
        published, published_ts = parse_date(published_raw)
        raw_description = first_child_text(node, {"description", "summary", "encoded", "content"})
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
                "author": clean_text(author, 120),
                "title": title,
                "link": link,
                "published": published,
                "published_ts": published_ts,
                "excerpt": clean_text(raw_description, 280),
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


def send_notifications(articles: list[dict[str, Any]]) -> None:
    if not articles or not load_config().get("notify", True):
        return
    for article in articles[:3]:
        command = [
            "omarchy",
            "notification",
            "send",
            "--app-name",
            "Substack",
            "-g",
            "󰂺",
            "-u",
            "normal",
            article.get("title", "New Substack post"),
            article.get("publication", "Substack"),
            "--exec",
            "python3",
            str(SCRIPT_PATH),
            "open",
            article["id"],
        ]
        with contextlib.suppress(OSError):
            subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
    if len(articles) > 3:
        with contextlib.suppress(OSError):
            subprocess.run(
                [
                    "omarchy",
                    "notification",
                    "send",
                    "--app-name",
                    "Substack",
                    "-g",
                    "󰂺",
                    f"{len(articles) - 3} more new posts",
                    "Open the Substack panel to see them.",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
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
            feed_url, expected_feed_host, is_custom_domain = publication_feed_target(publication)
            status, _, response_headers, body = request(
                feed_url,
                allowed_hosts={expected_feed_host},
                headers=headers,
                max_bytes=MAX_FEED_BYTES,
                timeout=20,
            )
            if is_custom_domain:
                validate_custom_feed_response(publication, response_headers)
            if status == 304:
                merge_not_modified(publication_id, checked)
                return
            items = parse_feed(body, publication)
            new_articles = merge_feed(publication_id, items, checked)
            etag = str(response_headers.get("ETag") or "")
            last_modified = str(response_headers.get("Last-Modified") or "")
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

    def run(self) -> int:
        ensure_dirs()
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        if not STATE_FILE.exists():
            atomic_json(STATE_FILE, default_state())

        daemon_descriptor = os.open(DAEMON_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(daemon_descriptor, "a+") as daemon_handle:
            with contextlib.suppress(OSError):
                os.fchmod(daemon_handle.fileno(), 0o600)
            try:
                fcntl.flock(daemon_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0

            mutate_state(lambda state: state.update({"status": "starting", "message": "Starting Substack…"}))
            idle_rounds = 0
            while self.running:
                worked = False
                if REFRESH_REQUEST.exists():
                    with contextlib.suppress(FileNotFoundError):
                        REFRESH_REQUEST.unlink()
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


def touch_refresh() -> None:
    ensure_dirs()
    REFRESH_REQUEST.touch(mode=0o600, exist_ok=True)


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
    if hostname != "substack.com" and not hostname.endswith(".substack.com"):
        return False
    query = urllib.parse.parse_qs(parsed.query)
    return parsed.path.rstrip("/") == "/sign-in" and bool(query.get("token"))


def auth_navigation_allowed(value: str) -> bool:
    source = str(value or "").strip()
    if source == "about:blank":
        return True
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
    return hostname == "substack.com" or hostname.endswith(".substack.com") or hostname == "challenges.cloudflare.com"


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
            if auth_navigation_allowed(current_uri):
                hostname = urllib.parse.urlsplit(current_uri).hostname or "substack.com"
                self.header.props.subtitle = f"{hostname} · temporary private session"
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
            if auth_navigation_allowed(uri):
                return False
            decision.ignore()
            self.banner.set_text("Blocked navigation outside Substack")
            return True

        def on_permission_request(self, _view: Any, request: Any) -> bool:
            request.deny()
            return True

        def on_load_failed(self, _view: Any, _event: Any, _uri: str, error: Any) -> bool:
            self.banner.set_text("Substack could not load: " + str(error.message))
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
    ensure_dirs()
    descriptor = os.open(AUTH_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        with contextlib.suppress(OSError):
            os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A Substack sign-in window is already open", file=sys.stderr)
            return 3
        return _auth_window_unlocked()


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Omarchy Substack feed backend")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("daemon")
    subparsers.add_parser("auth-window")
    subparsers.add_parser("refresh")
    subparsers.add_parser("mark-all-read")
    subparsers.add_parser("disconnect")
    subparsers.add_parser("status")
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
    if args.command == "config":
        return command_config(args.key, args.value)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
