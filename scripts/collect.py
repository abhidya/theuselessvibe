#!/usr/bin/env python3
"""Discover owned GitHub Pages URLs, checkpoint in SQLite, export static data.

Search is not an atomic snapshot. We detect incomplete pages, changing totals,
and duplicate/missing results within a partition instead of calling those scans
complete. A completed scan means all observed partitions and checks finished;
it does not prove that every reachable page is a game or that it remains online.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from http.client import InvalidURL
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit, unquote
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from game_metadata import VERSION as TAG_VERSION, classify, candidate_files, MAX_FILE_BYTES


SCHEMA_VERSION = 1
SEARCH_LIMIT = 1000
PAGE_SIZE = 100
MAX_README_BYTES = 1024 * 1024
MAX_PAGE_BYTES = 256 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
REPOSITORY_REFRESH_SECONDS = 86400
ATTRIBUTION_SCOPE = "Latest 30 commits and pull requests among the latest 100 issue records"
USER_AGENT = "TheUselessVibeCatalog/1.0 (+https://github.com/abhidya/theuselessvibe)"
PAGE_URL_PATTERN = re.compile(r"https://[^\s<>\"'`]+", re.IGNORECASE)


class CollectorError(Exception):
    """An actionable collection failure, without credentials in its message."""


class ResponseTooLarge(CollectorError):
    """A single resource exceeds its configured acquisition budget."""


class BudgetExpired(CollectorError):
    """Pause safely; the next invocation resumes from the database."""


class HttpFailure(CollectorError):
    def __init__(self, status: int, message: str = "Remote HTTP request failed"):
        super().__init__(f"{message} (HTTP {status})")
        self.status = status


def utc_iso(seconds: int | float | None = None) -> str:
    dt = datetime.fromtimestamp(time.time() if seconds is None else seconds, timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> float:
    if not value:
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    base = config.get("baseQuery")
    terms = config.get("phrases")
    if not isinstance(base, str) or not base.strip():
        raise CollectorError("Search config must contain a nonempty baseQuery")
    if not isinstance(terms, list) or not terms:
        raise CollectorError("Search config must contain at least one phrase")
    normalized = []
    for term in terms:
        if not isinstance(term, str) or not term.strip() or '"' in term or "\\" in term:
            raise CollectorError("Search phrases must be nonempty strings without quotes or backslashes")
        clean = " ".join(term.split())
        if len(clean) > 120:
            raise CollectorError("A search phrase exceeds 120 characters")
        if clean.casefold() not in {existing.casefold() for existing in normalized}:
            normalized.append(clean)
    discovery = config.get("discoveryQueries", [])
    if (not isinstance(discovery, list) or len(discovery) > 24
            or not all(isinstance(q, str) and 0 < len(q.strip()) <= 200
                       and not any(ord(c) < 32 for c in q) for q in discovery)):
        raise CollectorError("discoveryQueries must contain at most 24 nonempty single-line queries")
    config = dict(config, baseQuery=base.strip(), phrases=normalized,
                  discoveryQueries=list(dict.fromkeys(q.strip() for q in discovery)))
    for setting in ("verificationMaxAgeDays", "attributionMaxAgeDays"):
        value = config.get(setting, 7)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 30:
            raise CollectorError(f"{setting} must be between 0 and 30 days")
        config[setting] = value
    exclusions_path = path.parent / "exclusions.json"
    exclusions = json.loads(exclusions_path.read_text(encoding="utf-8")) if exclusions_path.exists() else {}
    for key in ("repositories", "urls"):
        values = exclusions.get(key, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise CollectorError(f"exclusions.json {key} must be an array of strings")
    config["exclusions"] = exclusions
    return config


def exclusion_url(url: str) -> str:
    """A removal covers the same route with any fragment or query string."""
    try:
        parsed = urlsplit(request_url(url.strip()))
        netloc = parsed.netloc.lower()
        if netloc.endswith(":443") and parsed.scheme.lower() == "https":
            netloc = netloc[:-4]
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/") or "/", "", ""))
    except (ValueError, UnicodeError):
        return url.strip()


def is_excluded(repo: dict, url: str | None, config: dict) -> bool:
    exclusions = config.get("exclusions", {})
    names = {value.strip().strip("/").casefold() for value in exclusions.get("repositories", [])}
    if str(repo.get("full_name") or "").casefold() in names:
        return True
    return bool(url and exclusion_url(url) in {exclusion_url(value) for value in exclusions.get("urls", [])})


def search_queries(config: dict) -> list[str]:
    # Keep legacy queries first so an appended discovery source can resume the
    # existing checkpoint. Enforce the same eligibility on every new query.
    legacy = [f'{config["baseQuery"]} "{term}"' for term in config["phrases"]]
    extra = [f'{query} stars:<2 license:mit fork:false'
             for query in config.get("discoveryQueries", [])]
    return list(dict.fromkeys(legacy + extra))


def search_url(query: str) -> str:
    return "https://github.com/search?" + urlencode({"q": query, "type": "repositories", "s": "updated", "o": "desc"})


def split_range(start: int, end: int) -> tuple[tuple[int, int], tuple[int, int]]:
    """Inclusive, disjoint ranges, down to GitHub's one-second granularity."""
    if start >= end:
        raise ValueError("Cannot split a single-second range")
    middle = (start + end) // 2
    return (start, middle), (middle + 1, end)


def partition_query(query: str, start: int, end: int) -> str:
    return f"{query} created:{utc_iso(start)}..{utc_iso(end)}"


def compact_repository(repo: dict) -> dict:
    fields = ("id", "name", "full_name", "description", "homepage", "stargazers_count",
              "fork", "private", "license", "created_at", "updated_at", "pushed_at", "has_pages", "topics", "language", "default_branch")
    result = {field: repo.get(field) for field in fields}
    result["owner"] = {"login": (repo.get("owner") or {}).get("login", "")}
    return result


def valid_stars(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def repository_eligible(repo: dict, *, admitted: bool = False) -> bool:
    stars = repo.get("stargazers_count")
    return (
        repo.get("fork") is False
        and repo.get("private") is False
        and valid_stars(stars) and (admitted or stars < 2)
        and (repo.get("license") or {}).get("spdx_id") == "MIT"
        and bool(repo.get("full_name")) and bool(repo.get("id"))
    )


def request_url(value: str) -> str:
    """Encode an IRI for urllib without decoding escaped path separators.

    Host/path ownership is checked separately before every network request.
    See urllib.parse.quote; raw Unicode cannot be sent in an HTTP request line.
    """
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("Invalid URL characters")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected an HTTPS URL without credentials")
    if parsed.port not in (None, 443):
        raise ValueError("Unexpected port")
    # Current allowed origins are ASCII GitHub hosts. Do not reinterpret a
    # Unicode/lookalike hostname or silently drop malformed authority data.
    parsed.netloc.encode("ascii")
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
    return urlunsplit(("https", parsed.hostname, path, query, ""))


def owned_pages_url(raw: str, repo: dict) -> str | None:
    """Accept actual, explicit HTTPS URLs for this repository's own Pages path."""
    if not isinstance(raw, str) or len(raw) > 4096:
        return None
    value = html.unescape(raw.strip())
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        return None
    owner = str((repo.get("owner") or {}).get("login") or "")
    name = str(repo.get("name") or "")
    if not owner or not name:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() != "https" or parsed.hostname != f"{owner.lower()}.github.io"
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            return None
        path = parsed.path or "/"
        # Disallow encoded separators/traversal before doing ownership checks.
        decoded = path
        for _ in range(3):
            next_decoded = unquote(decoded)
            if next_decoded == decoded:
                break
            decoded = next_decoded
        if "\\" in decoded or any(part in (".", "..") for part in decoded.split("/")):
            return None
        if re.search(r"%(?:2f|5c)", path, re.IGNORECASE):
            return None
        segments = [part for part in decoded.split("/") if part]
        if name.casefold() == f"{owner}.github.io".casefold():
            # A user-site README can list many other projects on the same host.
            if segments and segments not in (["index.html"], ["index.htm"]):
                return None
        elif not segments or segments[0].casefold() != name.casefold():
            return None
        return request_url(urlunsplit(("https", parsed.hostname, path, parsed.query, "")))
    except (ValueError, UnicodeError):
        return None


def candidate_urls(repo: dict, readme: str = "", *, include_default: bool = False) -> list[str]:
    explicit = [repo.get("homepage") or ""]
    for match in PAGE_URL_PATTERN.finditer(html.unescape(readme)):
        raw = match.group(0).rstrip(".,;:!\"'`")
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
            while raw.endswith(closing) and raw.count(closing) > raw.count(opening):
                raw = raw[:-1]
        explicit.append(raw)
    # This is only a candidate, never proof that Pages is enabled. Search
    # metadata may omit has_pages; an explicit false prevents the probe.
    default = None
    if include_default and repo.get("has_pages") is not False:
        owner = str((repo.get("owner") or {}).get("login") or "")
        name = str(repo.get("name") or "")
        if owner and name:
            path = "/" if name.casefold() == f"{owner}.github.io".casefold() else f"/{quote(name, safe='')}/"
            default = owned_pages_url(f"https://{owner.lower()}.github.io{path}", repo)
    result = []
    for value in explicit:
        url = owned_pages_url(value, repo)
        if url is not None and url not in result:
            result.append(url)
    # Homepage first, then shallow pages ahead of screenshots/deep documentation.
    home = owned_pages_url(repo.get("homepage") or "", repo)
    result = sorted(result, key=lambda url: (url != home, urlsplit(url).path.count("/"), len(url)))[:11]
    if default and default not in result:
        result.append(default)
    return result


# Evidence is recorded, not advertised as an automated playability test. These
# cues supplement the exact phrase filters and allow short/alternative READMEs.
GAME_TOPICS = {"browser-game", "html5-game", "javascript-game", "web-game", "godot-game", "js13k"}
GAME_WORDS = re.compile(r"\b(?:games?|pacman|tetris|minesweeper|sokoban|solitaire|breakout|platformer|roguelike|shootemup)\b", re.I)
PLAY_CUES = ("playable at", "play here", "play now", "browser game", "html5 game", "press space", "press enter", "tap to play")


def game_evidence(repo: dict, readme: str, page: str = "") -> list[dict]:
    evidence = []
    for topic in repo.get("topics") or []:
        if isinstance(topic, str) and topic.casefold() in GAME_TOPICS:
            evidence.append({"source": "topic", "value": topic.casefold()})
    # Names/descriptions alone are hints; they still require an owned, reachable
    # HTML page. No canvas or 200 response alone is labelled a working game.
    for source, value in (("metadata", str(repo.get("name") or "") + " " + str(repo.get("description") or "")),
                          ("readme", readme), ("page", page)):
        normalized = " ".join(value.casefold().split())
        match = GAME_WORDS.search(normalized)
        if match:
            evidence.append({"source": source, "value": match.group(0)})
        for cue in PLAY_CUES:
            if cue in normalized:
                evidence.append({"source": source, "value": cue})
    return evidence


class VisibleText(HTMLParser):
    OMIT = {"script", "style", "head", "template", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.OMIT:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag in self.OMIT and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


def visible_text(body: str) -> str:
    parser = VisibleText()
    parser.feed(body)
    return " ".join(" ".join(parser.parts).split())


def detect_signals(phrases: list[str], readme: str, page: str) -> list[dict]:
    signals = []
    for source, content in (("readme", readme), ("page", page)):
        normalized = " ".join(html.unescape(content).casefold().split())
        for phrase in phrases:
            # A word boundary avoids matching WASD inside a random identifier.
            needle = re.escape(" ".join(phrase.casefold().split()))
            if re.search(r"(?<!\w)" + needle + r"(?!\w)", normalized):
                signals.append({"term": phrase, "source": source})
    return signals


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class Budget:
    def __init__(self, seconds: float, *, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.deadline = clock() + seconds

    def check(self, minimum: float = 1) -> None:
        if self.deadline - self.clock() < minimum:
            raise BudgetExpired("Time budget reached; collection will resume next run")

    def wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.check(seconds + 1)
        until = self.clock() + seconds
        while until > self.clock():
            self.sleep(min(30, until - self.clock()))


class HttpClient:
    def __init__(self, budget: Budget, token: str | None = None, timeout: float = 20):
        self.budget, self.token, self.timeout = budget, token, timeout
        self.opener = build_opener(NoRedirect())
        self.next_search = 0.0
        self.not_before: dict[str, float] = {}

    def _request(self, url: str, *, headers: dict, allowed: Callable[[str], bool],
                 limit: int, resource: str | None = None) -> tuple[bytes, dict, str]:
        for attempt in range(4):
            self.budget.check(self.timeout + 1)
            if resource:
                self.budget.wait(self.not_before.get(resource, 0) - time.time())
            if resource == "search":
                self.budget.wait(self.next_search - time.monotonic())
                self.next_search = time.monotonic() + (2.1 if self.token else 6.1)
            target = url
            try:
                for _ in range(6):
                    self.budget.check(self.timeout + 1)
                    try:
                        target = request_url(target)
                    except (ValueError, UnicodeError) as error:
                        raise HttpFailure(400, "Malformed URL") from error
                    if not allowed(target):
                        raise HttpFailure(400, "URL or redirect is outside the permitted host/path")
                    request = Request(target, headers=headers)
                    try:
                        response = self.opener.open(request, timeout=self.timeout)
                    except HTTPError as error:
                        response = error
                    except (InvalidURL, UnicodeError, ValueError) as error:
                        raise HttpFailure(400, "Malformed URL") from error
                    with response:
                        status = response.code
                        response_headers = {key.lower(): value for key, value in response.headers.items()}
                        if resource and response_headers.get("x-ratelimit-remaining") == "0":
                            self.not_before[resource] = float(response_headers.get("x-ratelimit-reset", time.time() + 60)) + 1
                        if status in (301, 302, 303, 307, 308):
                            location = response_headers.get("location")
                            if not location:
                                raise HttpFailure(status, "Redirect has no destination")
                            target = urljoin(target, location)
                            continue
                        rate_message = ""
                        if resource and status == 403:
                            rate_message = response.read(8192).decode("utf-8", errors="replace").lower()
                        if status == 429 or (resource and status == 403 and (
                                "retry-after" in response_headers
                                or response_headers.get("x-ratelimit-remaining") == "0"
                                or "rate limit" in rate_message or "abuse detection" in rate_message)):
                            delay = retry_delay(response_headers)
                            self.not_before[resource or "pages"] = time.time() + delay
                            self.budget.wait(delay)
                            break
                        if status >= 500 and attempt < 3:
                            self.budget.wait(2 ** (attempt + 1))
                            break
                        if not 200 <= status < 300:
                            raise HttpFailure(status)
                        chunks, received = [], 0
                        read_chunk = getattr(response, "read1", response.read)
                        while received <= limit:
                            self.budget.check()
                            chunk = read_chunk(min(16384, limit + 1 - received))
                            if not chunk:
                                break
                            chunks.append(chunk)
                            received += len(chunk)
                        body = b"".join(chunks)
                        if len(body) > limit and resource:
                            raise ResponseTooLarge("GitHub response exceeded the configured size limit")
                        return body[:limit], response_headers, target
                else:
                    raise HttpFailure(400, "Too many redirects")
            except (URLError, TimeoutError, OSError) as error:
                if attempt == 3:
                    raise CollectorError(f"Network request failed ({type(error).__name__})") from None
                self.budget.wait(2 ** (attempt + 1))
        raise CollectorError("Remote request remained rate limited or unavailable after retries")

    def github(self, path: str, params: dict | None = None, *, raw: bool = False) -> Any:
        resource = "search" if path.startswith("/search/") else "core"
        url = "https://api.github.com" + path
        if params:
            url += "?" + urlencode(params)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2026-03-10"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        def allowed(candidate):
            parsed = urlsplit(candidate)
            return parsed.scheme == "https" and parsed.hostname == "api.github.com" and parsed.username is None and parsed.port in (None, 443)
        body, _, _ = self._request(url, headers=headers, allowed=allowed,
                                   limit=MAX_README_BYTES if raw else MAX_JSON_BYTES, resource=resource)
        return body.decode("utf-8", errors="replace") if raw else json.loads(body)

    def search(self, query: str, page: int) -> dict:
        return self.github("/search/repositories", {"q": query, "sort": "updated", "order": "desc", "per_page": PAGE_SIZE, "page": page})

    def readme(self, repo: dict) -> str:
        path = "/repos/" + quote(repo["full_name"], safe="/") + "/readme"
        try:
            return self.github(path, raw=True)
        except HttpFailure as error:
            if error.status in (404, 410):
                return ""
            raise

    def page(self, url: str, repo: dict) -> tuple[str, str] | None:
        def allowed(candidate):
            return owned_pages_url(candidate, repo) is not None
        try:
            body, headers, final_url = self._request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                                                   allowed=allowed, limit=MAX_PAGE_BYTES)
        except BudgetExpired:
            raise
        except CollectorError:
            return None
        mime = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if mime not in ("text/html", "application/xhtml+xml") or not body.strip():
            return None
        return owned_pages_url(final_url, repo), visible_text(body.decode("utf-8", errors="replace"))

    def attribution(self, repo: dict) -> dict:
        from attribution import detect_attribution
        prefix = "/repos/" + quote(repo["full_name"], safe="/")
        commits = self.github(prefix + "/commits", {"per_page": 30})
        pulls = self.github(prefix + "/issues", {"state": "all", "sort": "created", "direction": "desc", "per_page": 100})
        return detect_attribution(commits, pulls)


def retry_delay(headers: dict) -> float:
    retry = headers.get("retry-after")
    if retry:
        try:
            return max(1, float(retry))
        except ValueError:
            try:
                return max(1, parsedate_to_datetime(retry).timestamp() - time.time())
            except (ValueError, TypeError):
                pass
    if headers.get("x-ratelimit-remaining") == "0":
        try:
            return max(1, float(headers["x-ratelimit-reset"]) - time.time() + 1)
        except (KeyError, ValueError):
            pass
    return 60


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS partitions(
                id INTEGER PRIMARY KEY, query_index INTEGER NOT NULL,
                start_second INTEGER NOT NULL, end_second INTEGER NOT NULL,
                page INTEGER NOT NULL DEFAULT 1, expected_count INTEGER,
                seen_ids TEXT NOT NULL DEFAULT '[]', items_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending', reason TEXT
            );
            CREATE TABLE IF NOT EXISTS repositories(
                id TEXT PRIMARY KEY, full_name TEXT NOT NULL, payload TEXT NOT NULL,
                updated_at TEXT NOT NULL, seen_scan TEXT NOT NULL, checked_scan TEXT,
                check_fingerprint TEXT, checked_at TEXT, outcome TEXT, site TEXT,
                attribution TEXT, attribution_checked_at TEXT, attribution_fingerprint TEXT,
                attribution_status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE INDEX IF NOT EXISTS repository_scan ON repositories(seen_scan, checked_scan, updated_at DESC);
            CREATE TABLE IF NOT EXISTS game_metadata(
                repo_id TEXT PRIMARY KEY, inputs TEXT NOT NULL DEFAULT '{}',
                input_key TEXT, source_key TEXT, snapshot TEXT,
                payload TEXT, version TEXT, status TEXT NOT NULL DEFAULT 'pending',
                checked_at TEXT, retry_after TEXT
            );
            CREATE TABLE IF NOT EXISTS query_matches(
                query_index INTEGER NOT NULL, repo_id TEXT NOT NULL, PRIMARY KEY(query_index, repo_id)
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(partitions)")}
        if "items_json" not in columns:
            self.db.execute("ALTER TABLE partitions ADD COLUMN items_json TEXT NOT NULL DEFAULT '[]'")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(repositories)")}
        for column in ("admitted_at", "stars_checked_at", "repository_checked_at",
                       "repository_checked_scan", "repository_retry_after"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE repositories ADD COLUMN {column} TEXT")
        if "admitted_at" not in columns:
            # Existing verified sites are evidence of an earlier low-star admission.
            # A historical page-check timestamp is not a star observation timestamp.
            self.db.execute("UPDATE repositories SET admitted_at=COALESCE(checked_at, ?) WHERE site IS NOT NULL", (utc_iso(),))
        self.db.commit()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value: Any):
        self.db.execute("INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json_text(value)))

    @property
    def scan(self):
        return self.get("scanId")

    def start(self, config: dict, *, refresh: bool = False, now: int | None = None, export_only: bool = False):
        # Maintainer removals take effect immediately without restarting a scan.
        search_config = {key: value for key, value in config.items() if key != "exclusions"}
        config_hash = hashlib.sha256(json_text(search_config).encode()).hexdigest()
        old_hash = self.get("configHash")
        if self.scan:
            if export_only:
                if old_hash != config_hash:
                    raise CollectorError("Search configuration changed; collect the new configuration before exporting it")
                return
            old_config = self.get("config", {})
            old_queries = search_queries(old_config) if old_config.get("baseQuery") else []
            new_queries = search_queries(config)
            unchanged = lambda value: {k: v for k, v in value.items() if k not in ("exclusions", "discoveryQueries")}
            if (not refresh and self.get("finishedAt") is None and old_queries
                    and unchanged(old_config) == unchanged(config)
                    and new_queries[:len(old_queries)] == old_queries
                    and len(new_queries) > len(old_queries)):
                with self.db:
                    for index in range(len(old_queries), len(new_queries)):
                        self.db.execute("INSERT INTO partitions(query_index,start_second,end_second) VALUES(?,?,?)",
                                        (index, 0, self.get("throughSecond")))
                    self.put("configHash", config_hash)
                    self.put("config", config)
                    # Earlier rejected candidates can now qualify through new
                    # evidence/default URL checks, without discarding good sites.
                    self.db.execute("UPDATE repositories SET checked_scan=NULL WHERE seen_scan=? AND site IS NULL", (self.scan,))
                return
            refresh = refresh or old_hash != config_hash or self.get("finishedAt") is not None
            if not refresh:
                return
        now = int(time.time()) if now is None else now
        with self.db:
            self.db.execute("DELETE FROM partitions")
            self.db.execute("DELETE FROM query_matches")
            self.put("schemaVersion", SCHEMA_VERSION)
            self.put("configHash", config_hash)
            self.put("config", config)
            self.put("scanId", uuid.uuid4().hex)
            self.put("startedAt", utc_iso(now))
            self.put("throughSecond", now)
            self.put("finishedAt", None)
            self.put("reportedMatchesByQuery", {})
            for index, _ in enumerate(search_queries(config)):
                self.db.execute("INSERT INTO partitions(query_index,start_second,end_second) VALUES(?,?,?)", (index, 0, now))

    def fingerprint(self, repo: dict) -> str:
        # Popularity changes should not invalidate the separate HTML cache.
        metadata = {key: value for key, value in repo.items() if key != "stargazers_count"}
        return hashlib.sha256((self.get("configHash") + json_text(metadata)).encode()).hexdigest()

    def add_repository(self, repo: dict, query_index: int, *, observed_at: str | None = None):
        repo = compact_repository(repo)
        if not repo.get("id") or not repo.get("full_name"):
            raise CollectorError("GitHub returned a repository without an ID or name")
        identifier = str(repo["id"])
        buffered_observation = observed_at is not None
        observed_at = observed_at or utc_iso()
        previous = self.db.execute("SELECT * FROM repositories WHERE id=?", (identifier,)).fetchone()
        self.db.execute("INSERT OR IGNORE INTO query_matches VALUES(?,?)", (query_index, identifier))
        if previous and (max(previous["stars_checked_at"] or "", previous["repository_checked_at"] or "") > observed_at
                         or (buffered_observation and previous["repository_checked_at"]
                             and previous["repository_checked_at"] >= observed_at)):
            # A multi-page search checkpoint can be older than a direct refresh.
            # On same-second ties, prefer the independent repository check.
            self.db.execute("UPDATE repositories SET seen_scan=? WHERE id=?", (self.scan, identifier))
            return
        self.db.execute("""INSERT INTO repositories(id,full_name,payload,updated_at,seen_scan,stars_checked_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name,
            payload=excluded.payload, updated_at=excluded.updated_at, seen_scan=excluded.seen_scan,
            stars_checked_at=excluded.stars_checked_at,
            checked_scan=CASE WHEN repositories.payload != excluded.payload THEN NULL ELSE repositories.checked_scan END""",
            (identifier, repo["full_name"], json_text(repo), repo.get("updated_at") or repo.get("pushed_at") or repo.get("created_at") or "", self.scan,
             observed_at if valid_stars(repo.get("stargazers_count")) else None))
        if not repository_eligible(repo, admitted=bool(previous and previous["admitted_at"])):
            self.db.execute("UPDATE repositories SET site=NULL,admitted_at=NULL,outcome='no-longer-eligible',checked_scan=?,checked_at=? WHERE id=?", (self.scan, utc_iso(), identifier))

    def next_partition(self):
        return self.db.execute("SELECT * FROM partitions WHERE status='pending' ORDER BY start_second DESC, query_index, id LIMIT 1").fetchone()

    def split_partition(self, partition, reason: str):
        with self.db:
            if partition["start_second"] == partition["end_second"]:
                self.db.execute("UPDATE partitions SET status='unresolved',reason=? WHERE id=?", (reason, partition["id"]))
                return
            self.db.execute("UPDATE partitions SET status='split',reason=?,items_json='[]' WHERE id=?", (reason, partition["id"]))
            for start, end in split_range(partition["start_second"], partition["end_second"]):
                self.db.execute("INSERT INTO partitions(query_index,start_second,end_second) VALUES(?,?,?)", (partition["query_index"], start, end))

    def next_check(self):
        return self.db.execute("""SELECT * FROM repositories WHERE
            (seen_scan=? OR (admitted_at IS NOT NULL AND repository_checked_scan=?))
            AND (checked_scan IS NULL OR checked_scan!=?)
            ORDER BY updated_at DESC,id LIMIT 1""", (self.scan, self.scan, self.scan)).fetchone()

    def due_repository_refreshes(self, *, count: bool = False, scan_only: bool = False):
        # Oldest checked first; retries back off and neither a huge catalog nor
        # a repeatedly failing entry can monopolize this lane.
        selection = "COUNT(*)" if count else "*"
        order = "" if count else " ORDER BY COALESCE(repository_checked_at, ''),id LIMIT 1"
        freshness = "" if scan_only else " OR repository_checked_at IS NULL OR repository_checked_at<=?"
        params = (utc_iso(), self.scan)
        if not scan_only:
            params += (utc_iso(time.time() - REPOSITORY_REFRESH_SECONDS),)
        return self.db.execute(f"""SELECT {selection} FROM repositories WHERE admitted_at IS NOT NULL
            AND (repository_retry_after IS NULL OR repository_retry_after<=?)
            AND (repository_checked_scan IS NULL OR repository_checked_scan!=?{freshness}){order}""", params)

    def next_repository_refresh(self):
        return self.due_repository_refreshes().fetchone()

    def observe_metadata(self, identifier: str, repo: dict, readme: str, observed_at: str):
        inputs = {"readme": readme.encode("utf-8")[:MAX_FILE_BYTES].decode("utf-8", errors="ignore"), "observedAt": observed_at}
        input_key = hashlib.sha256(json_text({"readme": inputs["readme"], "topics": repo.get("topics"),
                                              "language": repo.get("language"), "source": source_key(repo)}).encode()).hexdigest()
        row = self.db.execute("SELECT * FROM game_metadata WHERE repo_id=?", (identifier,)).fetchone()
        if row and row["input_key"] == input_key and row["version"] == TAG_VERSION:
            return
        tags = classify(repo, inputs["readme"], observed_at=observed_at)
        snapshot = row["snapshot"] if row and row["source_key"] == source_key(repo) else None
        self.db.execute("""INSERT INTO game_metadata(repo_id,inputs,input_key,source_key,snapshot,payload,version,status)
            VALUES(?,?,?,?,?,?,?,'pending') ON CONFLICT(repo_id) DO UPDATE SET
            inputs=excluded.inputs,input_key=excluded.input_key,source_key=excluded.source_key,
            snapshot=excluded.snapshot,payload=excluded.payload,version=excluded.version,status='pending',retry_after=NULL""",
            (identifier,json_text(inputs),input_key,source_key(repo),snapshot,json_text(tags),TAG_VERSION))

    def next_metadata(self):
        return self.db.execute("""SELECT r.*, m.inputs AS metadata_inputs, m.snapshot AS metadata_snapshot,
            m.source_key AS metadata_source_key FROM repositories r LEFT JOIN game_metadata m ON m.repo_id=r.id
            WHERE r.site IS NOT NULL AND (m.repo_id IS NULL OR m.version!=? OR m.status='pending'
                OR (m.retry_after IS NOT NULL AND m.retry_after<=?))
            ORDER BY r.updated_at DESC,r.id LIMIT 1""", (TAG_VERSION, utc_iso())).fetchone()

    def next_attribution(self):
        return self.db.execute("SELECT * FROM repositories WHERE site IS NOT NULL AND attribution_status='pending' ORDER BY updated_at DESC,id LIMIT 1").fetchone()

    def counts(self) -> dict:
        current = self.scan
        discovered = self.db.execute("SELECT COUNT(*) FROM repositories WHERE seen_scan=?", (current,)).fetchone()[0]
        checked = self.db.execute("SELECT COUNT(*) FROM repositories WHERE seen_scan=? AND checked_scan=?", (current, current)).fetchone()[0]
        current_eligible = self.db.execute("SELECT COUNT(*) FROM repositories WHERE checked_scan=? AND outcome='reachable' AND site IS NOT NULL", (current,)).fetchone()[0]
        eligible = self.db.execute("SELECT COUNT(*) FROM repositories WHERE site IS NOT NULL").fetchone()[0]
        pending_checks = self.db.execute("""SELECT COUNT(*) FROM repositories WHERE
            (seen_scan=? OR (admitted_at IS NOT NULL AND repository_checked_scan=?))
            AND (checked_scan IS NULL OR checked_scan!=?)""", (current, current, current)).fetchone()[0]
        pending_attr = self.db.execute("SELECT COUNT(*) FROM repositories WHERE site IS NOT NULL AND attribution_status='pending'").fetchone()[0]
        unavailable_attr = self.db.execute("SELECT COUNT(*) FROM repositories WHERE site IS NOT NULL AND attribution_status='unavailable'").fetchone()[0]
        pending_stars = self.due_repository_refreshes(count=True).fetchone()[0]
        pending_scan_stars = self.due_repository_refreshes(count=True, scan_only=True).fetchone()[0]
        unavailable_stars = self.db.execute("SELECT COUNT(*) FROM repositories WHERE admitted_at IS NOT NULL AND repository_retry_after IS NOT NULL").fetchone()[0]
        pending_parts = self.db.execute("SELECT COUNT(*) FROM partitions WHERE status='pending'").fetchone()[0]
        unresolved = self.db.execute("SELECT COUNT(*) FROM partitions WHERE status='unresolved'").fetchone()[0]
        tag_counts = self.db.execute("""SELECT
            SUM(CASE WHEN m.repo_id IS NULL OR m.version!=? OR m.status='pending' THEN 1 ELSE 0 END),
            SUM(CASE WHEN m.version=? AND m.status='checked' THEN 1 ELSE 0 END),
            SUM(CASE WHEN m.version=? AND m.status='partial' THEN 1 ELSE 0 END)
            FROM repositories r LEFT JOIN game_metadata m ON r.id=m.repo_id WHERE r.site IS NOT NULL""",
            (TAG_VERSION,TAG_VERSION,TAG_VERSION)).fetchone()
        return dict(pendingMetadataChecks=tag_counts[0] or 0, metadataCheckedSites=tag_counts[1] or 0,
                    metadataPartialSites=tag_counts[2] or 0, discoveredRepositories=discovered, scannedRepositories=checked, eligibleSites=eligible,
                    currentScanEligibleSites=current_eligible, carriedForwardSites=eligible-current_eligible,
                    pendingRepositoryChecks=pending_checks, pendingAttributionChecks=pending_attr,
                    pendingStarRefreshes=pending_stars, unavailableStarRefreshes=unavailable_stars,
                    pendingScanStarRefreshes=pending_scan_stars,
                    unavailableAttributionChecks=unavailable_attr, pendingPartitions=pending_parts, unresolvedPartitions=unresolved)

    def finish_if_complete(self):
        counts = self.counts()
        # Periodic freshness must not keep a long discovery scan open forever.
        # Only its first pass over retained repositories gates scan completion.
        complete = not any(counts[key] for key in ("pendingPartitions", "unresolvedPartitions", "pendingRepositoryChecks", "pendingAttributionChecks", "pendingScanStarRefreshes"))
        if complete and self.get("finishedAt") is None:
            with self.db:
                self.put("finishedAt", utc_iso())
                # Admission survives search cutoff. Direct repository refreshes
                # retire deleted/private/forked/non-MIT records instead.
                self.db.execute("DELETE FROM repositories WHERE seen_scan!=? AND admitted_at IS NULL", (self.scan,))
                self.db.execute("DELETE FROM game_metadata WHERE repo_id NOT IN (SELECT id FROM repositories)")
        return complete

    def sites(self) -> list[dict]:
        result = []
        # Keep the previous verified catalog usable while a refresh accumulates.
        # New observations can remove entries immediately. Admitted entries
        # remain refreshable even after graduating beyond discovery's star cap.
        rows = self.db.execute("SELECT * FROM repositories WHERE site IS NOT NULL ORDER BY updated_at DESC,id")
        for row in rows:
            site = json.loads(row["site"])
            stars = json.loads(row["payload"]).get("stargazers_count")
            if not valid_stars(stars):
                stars = site.get("stars")
            site.update(stars=stars if valid_stars(stars) else None,
                        starsCheckedAt=row["stars_checked_at"])
            attribution = json.loads(row["attribution"]) if row["attribution"] else {"agents": [], "agentEvidence": []}
            site.update(attribution)
            tags = self.db.execute("SELECT payload,version FROM game_metadata WHERE repo_id=?", (row["id"],)).fetchone()
            if tags and tags["payload"] and tags["version"] == TAG_VERSION:
                site["metadata"] = json.loads(tags["payload"])
            else:
                site["metadata"] = classify(json.loads(row["payload"]), source_status="pending")
            site.update(attributionStatus=row["attribution_status"], attributionCheckedAt=row["attribution_checked_at"], attributionScope=ATTRIBUTION_SCOPE)
            result.append(site)
        return result

    def apply_exclusions(self, config: dict):
        removals = []
        for row in self.db.execute("SELECT id,payload,site FROM repositories"):
            repo = json.loads(row["payload"])
            site = json.loads(row["site"]) if row["site"] else None
            if is_excluded(repo, site.get("url") if site else None, config):
                removals.append((self.scan, row["id"]))
        with self.db:
            self.db.executemany("UPDATE repositories SET site=NULL,admitted_at=NULL,outcome='excluded',checked_scan=?,check_fingerprint=NULL WHERE id=?", removals)


def step_partition(store: Store, client: HttpClient, config: dict) -> bool:
    partition = store.next_partition()
    if partition is None:
        return False
    query = search_queries(config)[partition["query_index"]]
    result = client.search(partition_query(query, partition["start_second"], partition["end_second"]), partition["page"])
    count = result.get("total_count")
    items = result.get("items")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0 or not isinstance(items, list):
        raise CollectorError("GitHub search returned malformed pagination metadata")
    if partition["page"] == 1 and partition["start_second"] == 0 and partition["end_second"] == store.get("throughSecond"):
        reported = store.get("reportedMatchesByQuery", {})
        reported[str(partition["query_index"])] = {"query": query, "count": count, "incomplete": result.get("incomplete_results") is not False}
        with store.db:
            store.put("reportedMatchesByQuery", reported)
    if result.get("incomplete_results") is not False:
        store.split_partition(partition, "GitHub returned incomplete_results")
        return True
    if count > SEARCH_LIMIT:
        store.split_partition(partition, "Search exceeds GitHub's 1,000-result window")
        return True
    expected = partition["expected_count"]
    if expected is not None and expected != count:
        store.split_partition(partition, "Search count changed during pagination")
        return True
    expected = count
    seen = set(json.loads(partition["seen_ids"]))
    page_ids = [str(repo.get("id")) for repo in items if repo.get("id")]
    if len(page_ids) != len(items) or len(set(page_ids)) != len(page_ids) or seen.intersection(page_ids):
        store.split_partition(partition, "Missing or repeated IDs during pagination")
        return True
    remaining = max(0, expected - PAGE_SIZE * (partition["page"] - 1))
    if len(items) != min(PAGE_SIZE, remaining):
        store.split_partition(partition, "Search page length did not match its reported total")
        return True
    seen.update(page_ids)
    observed_at = utc_iso()
    collected_items = json.loads(partition["items_json"]) + [dict(compact_repository(repo), _observedAt=observed_at) for repo in items]
    final_page = partition["page"] >= max(1, math.ceil(expected / PAGE_SIZE))
    if final_page and len(seen) != expected:
        store.split_partition(partition, "Unique repository count did not match reported total")
        return True
    with store.db:
        # Do not publish candidates from a partition until all its pages agree.
        # A later split must not leave old, superseded page results in the union.
        if final_page:
            for repo in collected_items:
                store.add_repository(repo, partition["query_index"],
                                     observed_at=repo.get("_observedAt") or store.get("startedAt"))
        store.db.execute("UPDATE partitions SET expected_count=?,seen_ids=?,items_json=?,page=?,status=? WHERE id=?",
                         (expected, json_text(sorted(seen)), "[]" if final_page else json_text(collected_items),
                          partition["page"] + 1, "done" if final_page else "pending", partition["id"]))
    return True


def cache_fresh(checked_at: str | None, days: float) -> bool:
    age = time.time() - parse_time(checked_at)
    return 0 <= age < days * 86400


def step_repository_refresh(store: Store, client: HttpClient, config: dict) -> bool:
    """Refresh admitted repositories without relying on low-star search results.

    Each unit makes one bounded repository API request (with the HTTP client's
    normal retry budget). The persisted scheduler and oldest-first queue share
    runtime with discovery, page checks, tags and attribution.
    """
    row = store.next_repository_refresh()
    if row is None:
        return False
    previous = json.loads(row["payload"])
    site = json.loads(row["site"]) if row["site"] else None

    def retire(outcome):
        with store.db:
            store.db.execute("""UPDATE repositories SET admitted_at=NULL,site=NULL,
                outcome=?,checked_scan=?,repository_checked_scan=?,repository_checked_at=?,
                repository_retry_after=NULL,check_fingerprint=NULL WHERE id=?""",
                (outcome, store.scan, store.scan, utc_iso(), row["id"]))

    if is_excluded(previous, site.get("url") if site else previous.get("homepage"), config):
        retire("excluded")
        return True
    try:
        # IDs do not change when a repo is renamed/transferred, and cannot
        # accidentally refresh a different repository reusing the old name.
        response = client.github("/repositories/" + quote(row["id"], safe=""))
        required = {"id", "name", "full_name", "owner", "fork", "private", "license", "stargazers_count"}
        if (not isinstance(response, dict) or not required.issubset(response)
                or str(response["id"]) != row["id"]
                or not response["name"] or not response["full_name"]
                or not isinstance(response["owner"], dict) or not response["owner"].get("login")
                or not isinstance(response["fork"], bool) or not isinstance(response["private"], bool)
                or (response["license"] is not None and not isinstance(response["license"], dict))
                or not valid_stars(response["stargazers_count"])):
            raise ValueError("GitHub returned incomplete repository metadata")
        repo = compact_repository(response)
    except BudgetExpired:
        # The row remains due; the already-persisted scheduler turn prevents
        # this API request from starving other lanes on a short resumed run.
        raise
    except HttpFailure as error:
        if error.status in (404, 410, 451):
            retire("repository-unavailable")
            return True
        if error.status not in (400, 409, 422):
            # Authentication, rate limits and service failures affect the
            # batch. Do not silently defer every row behind a broken token.
            raise
        with store.db:
            store.db.execute("UPDATE repositories SET repository_retry_after=? WHERE id=?",
                             (utc_iso(time.time() + REPOSITORY_REFRESH_SECONDS), row["id"]))
        return True
    except (ResponseTooLarge, ValueError, TypeError):
        # Keep both the last count and its original observation time. Do not
        # turn an oversized/malformed response into a zero-star count.
        with store.db:
            store.db.execute("UPDATE repositories SET repository_retry_after=? WHERE id=?",
                             (utc_iso(time.time() + REPOSITORY_REFRESH_SECONDS), row["id"]))
        return True
    checked_at = utc_iso()
    with store.db:
        store.db.execute("""UPDATE repositories SET full_name=?,payload=?,updated_at=?,
            stars_checked_at=?,repository_checked_at=?,repository_checked_scan=?,
            repository_retry_after=NULL,checked_scan=NULL WHERE id=?""",
            (repo["full_name"], json_text(repo), repo.get("updated_at") or repo.get("pushed_at") or repo.get("created_at") or "",
             checked_at, checked_at, store.scan, row["id"]))
        if is_excluded(repo, site.get("url") if site else repo.get("homepage"), config):
            retire("excluded")
        elif not repository_eligible(repo, admitted=True):
            retire("no-longer-eligible")
        elif site and owned_pages_url(site["url"], repo) is None:
            # A transfer/name change invalidates the old owner's cached Pages URL.
            store.db.execute("UPDATE repositories SET site=NULL,check_fingerprint=NULL WHERE id=?", (row["id"],))
    return True


def step_check(store: Store, client: HttpClient, config: dict) -> bool:
    row = store.next_check()
    if row is None:
        return False
    repo = json.loads(row["payload"])
    fingerprint = store.fingerprint(repo)
    if is_excluded(repo, None, config):
        with store.db:
            store.db.execute("UPDATE repositories SET checked_scan=?,site=NULL,admitted_at=NULL,outcome='excluded',check_fingerprint=NULL WHERE id=?", (store.scan, row["id"]))
        return True
    # Metadata is checked again on every full scan. Only network verification is cached.
    cache_days = config.get("verificationMaxAgeDays", 7)
    if row["outcome"] != "reachable":
        cache_days = min(cache_days, 1)
    cached_site = json.loads(row["site"]) if row["site"] else None
    if (row["check_fingerprint"] == fingerprint and cache_fresh(row["checked_at"], cache_days)
            and not is_excluded(repo, cached_site.get("url") if cached_site else None, config)):
        with store.db:
            store.db.execute("UPDATE repositories SET checked_scan=? WHERE id=?", (store.scan, row["id"]))
        refresh_attribution_state(store, row["id"], config)
        return True
    site = None
    outcome = "no-owned-pages-url"
    checked_at = utc_iso()
    if repository_eligible(repo, admitted=bool(row["admitted_at"])):
        try:
            readme = client.readme(repo)
        except (HttpFailure, ResponseTooLarge) as error:
            # These failures belong to this repository, not the batch. Keep
            # authentication, rate limits and service failures as global pauses.
            if isinstance(error, HttpFailure) and error.status not in (400, 409, 422, 451):
                raise
            with store.db:
                store.db.execute("""UPDATE repositories SET checked_scan=?,check_fingerprint=?,
                    checked_at=?,outcome='readme-unavailable' WHERE id=?""",
                    (store.scan, fingerprint, checked_at, row["id"]))
            # An unreadable README does not invalidate a previously verified
            # page. Its existing site and original checkedAt remain unchanged.
            # The normal unsuccessful-check cache expires within a day, so a
            # later scan can reconsider the repository without a tight loop.
            return True
        urls = candidate_urls(repo, readme, include_default=bool(game_evidence(repo, readme)))
        if urls:
            outcome = "pages-unavailable"
        for url in urls:
            if is_excluded(repo, url, config):
                outcome = "excluded"
                continue
            verified = client.page(url, repo)
            if verified is None:
                continue
            final_url, page_text = verified
            if is_excluded(repo, final_url, config):
                outcome = "excluded"
                continue
            signals = detect_signals(config["phrases"], readme, page_text)
            evidence = game_evidence(repo, readme, page_text)
            if not signals and not evidence:
                outcome = "no-current-playability-signals"
                continue
            site = {
                "id": str(repo["id"]), "repo": repo["full_name"], "name": repo["name"],
                "description": str(repo.get("description") or "")[:400], "url": final_url,
                "repoUrl": "https://github.com/" + repo["full_name"], "stars": repo["stargazers_count"],
                "starsCheckedAt": row["stars_checked_at"],
                "license": "MIT", "updatedAt": repo.get("updated_at") or repo.get("pushed_at") or repo.get("created_at"),
                "createdAt": repo.get("created_at"), "checkedAt": checked_at, "signals": signals,
                "gameEvidence": evidence,
            }
            outcome = "reachable"
            break
    else:
        outcome = "no-longer-eligible"
    with store.db:
        store.db.execute("UPDATE repositories SET checked_scan=?,check_fingerprint=?,checked_at=?,outcome=?,site=? WHERE id=?",
                         (store.scan, fingerprint, checked_at, outcome, json_text(site) if site else None, row["id"]))
        if site:
            if not row["admitted_at"]:
                store.db.execute("""UPDATE repositories SET admitted_at=?,repository_checked_scan=?,
                    repository_checked_at=? WHERE id=?""", (checked_at, store.scan, row["stars_checked_at"], row["id"]))
            store.observe_metadata(row["id"], repo, readme, checked_at)
        elif outcome in ("excluded", "no-longer-eligible"):
            store.db.execute("UPDATE repositories SET admitted_at=NULL WHERE id=?", (row["id"],))
    if site:
        refresh_attribution_state(store, row["id"], config)
    return True


def source_key(repo: dict) -> str:
    return hashlib.sha256(json_text({k:repo.get(k) for k in ("full_name","pushed_at","default_branch")}).encode()).hexdigest()


def step_metadata(store: Store, client: HttpClient) -> bool:
    row = store.next_metadata()
    if row is None:
        return False
    repo = json.loads(row["payload"])
    inputs = json.loads(row["metadata_inputs"] or '{}')
    snapshot = json.loads(row["metadata_snapshot"] or '{}') if row["metadata_source_key"] == source_key(repo) else {}
    prefix = "/repos/" + quote(repo["full_name"], safe="/")

    def checkpoint():
        with store.db:
            store.db.execute("""INSERT INTO game_metadata(repo_id,inputs,source_key,snapshot,status)
                VALUES(?,?,?,?,'pending') ON CONFLICT(repo_id) DO UPDATE SET
                snapshot=excluded.snapshot,source_key=excluded.source_key,status='pending'""",
                (row["id"],json_text(inputs),source_key(repo),json_text(snapshot)))
    warnings = list(snapshot.get("warnings", []))
    # Older checkpoints marked failed reads as done. Recover those units too,
    # while retaining successful immutable blobs across retries.
    failed_paths = {warning.split(":", 1)[1] for warning in warnings
                    if warning.startswith(("missing-source:", "unreadable-source:"))}
    if "done" in snapshot:
        snapshot["done"] = [path for path in snapshot["done"] if path not in failed_paths]
    status = "checked"
    try:
        if not snapshot.get("revision"):
            commits = client.github(prefix + "/commits", {"per_page":1})
            if not isinstance(commits, list) or not commits:
                raise CollectorError("No source revision available")
            if not isinstance(commits[0], dict):
                raise CollectorError("Invalid source commit")
            revision = commits[0].get("sha", "")
            tree = ((commits[0].get("commit") or {}).get("tree") or {}).get("sha", "")
            if not re.fullmatch(r"[a-f0-9]{40}", revision) or not re.fullmatch(r"[a-f0-9]{40}", tree):
                raise CollectorError("Invalid source revision")
            snapshot.update(revision=revision,tree=tree,files={},done=[])
            checkpoint()
        if "candidates" not in snapshot:
            tree = client.github(prefix + "/git/trees/" + snapshot["tree"], {"recursive":1})
            if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
                raise CollectorError("Invalid source tree")
            snapshot["candidates"] = candidate_files(tree["tree"])
            if tree.get("truncated") is not False:
                warnings.append("source-tree-truncated")
            snapshot["warnings"] = warnings
            checkpoint()
        for entry in snapshot["candidates"]:
            path, sha = entry["path"], entry.get("sha", "")
            if path in snapshot["done"]:
                continue
            if not re.fullmatch(r"[a-f0-9]{40}", sha):
                warnings.append("invalid-blob-sha:" + path)
                snapshot["done"].append(path)
                continue
            # At most 128 KiB of raw source cached for one repository.
            used = sum(len(text.encode("utf-8")) for text in snapshot["files"].values())
            if used + entry["size"] > 131072:
                warnings.append("source-byte-budget:" + path)
                snapshot["done"].append(path)
                continue
            try:
                blob = client.github(prefix + "/git/blobs/" + sha)
                if not isinstance(blob, dict):
                    raise ValueError("Unexpected blob response")
                if blob.get("encoding") != "base64" or not isinstance(blob.get("size"), int) or blob["size"] > MAX_FILE_BYTES:
                    raise ValueError("Non-text or oversized blob")
                content = base64.b64decode(''.join(blob.get("content", "").split()),validate=True)
                if len(content) > MAX_FILE_BYTES or len(content) != blob["size"]:
                    raise ValueError("Unexpected blob size")
                # Verify the API response actually describes the immutable blob requested.
                digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
                if digest != sha:
                    raise ValueError("Blob integrity mismatch")
                snapshot["files"][path] = content.decode("utf-8")
            except (ValueError, UnicodeError) as error:
                warnings = [w for w in warnings if w not in ("unreadable-source:" + path, "missing-source:" + path)]
                warnings.append("unreadable-source:" + path)
            except HttpFailure as error:
                if error.status not in (404,410):
                    raise
                warnings = [w for w in warnings if w not in ("unreadable-source:" + path, "missing-source:" + path)]
                warnings.append("missing-source:" + path)
            else:
                warnings = [w for w in warnings if w not in ("unreadable-source:" + path, "missing-source:" + path)]
                snapshot["done"].append(path)
            snapshot["warnings"] = warnings
            checkpoint()
    except BudgetExpired:
        checkpoint()
        raise
    except (CollectorError, ValueError, KeyError, TypeError):
        # A source inspection failure must not reject an already reachable game.
        # Do not store credentials, response bodies, or exception messages.
        warnings.append("source-inspection-unavailable")
        status = "partial"
    if warnings:
        status = "partial"
    files = snapshot.get("files", {})
    readme_path = next((p for p in files if p.lower().split('/')[-1] in ('readme','readme.md','readme.rst','readme.txt')), None)
    readme = files[readme_path] if readme_path else inputs.get("readme", "")
    tags = classify(repo, readme, readme_path=readme_path or "$readme", files=files,
                    revision=snapshot.get("revision"), readme_revision=snapshot.get("revision") if readme_path else None,
                    observed_at=utc_iso(), source_status=status, warnings=warnings)
    if tags["warnings"]:
        status = "partial"
        tags["sourceStatus"] = "partial"
    with store.db:
        store.db.execute("""INSERT INTO game_metadata(repo_id,inputs,source_key,snapshot,payload,version,status,checked_at,retry_after)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(repo_id) DO UPDATE SET inputs=excluded.inputs,
            source_key=excluded.source_key,snapshot=excluded.snapshot,payload=excluded.payload,
            version=excluded.version,status=excluded.status,checked_at=excluded.checked_at,retry_after=excluded.retry_after""",
            (row["id"],json_text(inputs),source_key(repo),json_text(snapshot),json_text(tags),TAG_VERSION,status,utc_iso(),
             utc_iso(time.time()+86400) if status == "partial" else None))
    return True


def attribution_fingerprint(repo: dict) -> str:
    return hashlib.sha256(json_text({"repo": repo["full_name"], "pushed_at": repo.get("pushed_at"), "scope": ATTRIBUTION_SCOPE}).encode()).hexdigest()


def refresh_attribution_state(store: Store, identifier: str, config: dict):
    row = store.db.execute("SELECT * FROM repositories WHERE id=?", (identifier,)).fetchone()
    if row["site"] is None:
        return
    fingerprint = attribution_fingerprint(json.loads(row["payload"]))
    if (row["attribution_status"] == "checked" and row["attribution_fingerprint"] == fingerprint
            and cache_fresh(row["attribution_checked_at"], config.get("attributionMaxAgeDays", 7))):
        return
    with store.db:
        store.db.execute("UPDATE repositories SET attribution_status='pending',attribution=NULL,attribution_checked_at=NULL WHERE id=?", (identifier,))


def step_attribution(store: Store, client: HttpClient) -> bool:
    row = store.next_attribution()
    if row is None:
        return False
    repo = json.loads(row["payload"])
    try:
        result = client.attribution(repo)
        status = "checked"
    except HttpFailure as error:
        if error.status not in (403, 404, 409, 410):
            raise
        result = {"agents": [], "agentEvidence": []}
        status = "unavailable"
    with store.db:
        store.db.execute("UPDATE repositories SET attribution=?,attribution_status=?,attribution_checked_at=?,attribution_fingerprint=? WHERE id=?",
                         (json_text(result), status, utc_iso(), attribution_fingerprint(repo), row["id"]))
    return True


def manifest_metadata(store: Store, config: dict) -> dict:
    store.apply_exclusions(config)
    complete = store.finish_if_complete()
    counts = store.counts()
    search_complete = not counts["pendingPartitions"] and not counts["unresolvedPartitions"]
    queries = search_queries(config)
    return {
        "schemaVersion": SCHEMA_VERSION, "query": config["baseQuery"], "searchQueries": queries, "terms": config["phrases"],
        "searchUrl": search_url(queries[0]), "generatedAt": utc_iso(),
        "scanStartedAt": store.get("startedAt"), "scanFinishedAt": store.get("finishedAt"),
        "matchedRepositories": counts["discoveredRepositories"] if search_complete else None,
        **counts, "complete": complete,
        "reportedMatchesByQuery": list(store.get("reportedMatchesByQuery", {}).values()),
        "coverageNote": "Search results change during collection. Previously admitted repositories remain eligible after gaining stars and are refreshed independently. Prior verified sites remain available while a refresh is incomplete; carriedForwardSites counts those not yet rechecked. Stars are last observed counts, not live values; starsCheckedAt is null when the observation time is unknown. HTML reachability and phrase signals do not prove playability.",
        "attributionScope": ATTRIBUTION_SCOPE,
    }


def export(store: Store, config: dict, output: Path) -> dict:
    from export_catalog import export_catalog
    metadata = manifest_metadata(store, config)
    return export_catalog(store.sites(), metadata, output)


def step_collection(store: Store, client: HttpClient, config: dict) -> bool:
    """Validate backlog first; reserve small slices for tags and further discovery.

    Persisting the turn keeps repeated short runs from starving any phase.
    Each successful site check also publishes cheap, declared metadata immediately.
    """
    turn = store.get("workTurn", 0)
    with store.db:
        store.put("workTurn", turn + 1)
    lanes = (("check",) * 8 + ("metadata", "discover", "stars", "attribution"))
    actions = {"check": lambda: step_check(store, client, config),
               "metadata": lambda: step_metadata(store, client),
               "discover": lambda: step_partition(store, client, config),
               "stars": lambda: step_repository_refresh(store, client, config),
               "attribution": lambda: step_attribution(store, client)}
    order = [lanes[turn % len(lanes)], "check", "metadata", "discover", "stars", "attribution"]
    for lane in dict.fromkeys(order):
        if actions[lane]():
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(".cache/catalog.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("site/data"))
    parser.add_argument("--config", type=Path, default=Path("config/search.json"))
    parser.add_argument("--max-seconds", type=float, default=1200, help="Checkpoint before this runtime budget is exhausted")
    parser.add_argument("--refresh", action="store_true", help="Start a full new scan; reuse recent page checks and independently refresh admitted repositories")
    parser.add_argument("--export-only", action="store_true", help="Export existing SQLite state without network calls")
    parser.add_argument("--retry-unresolved", action="store_true", help="Retry unresolved one-second partitions from a previous run")
    parser.add_argument("--require-complete", action="store_true", help="Return exit code 2 if the exported scan is still partial")
    args = parser.parse_args(argv)
    if args.max_seconds < 30:
        parser.error("--max-seconds must be at least 30")
    store = None
    config = None
    initialized = False
    exit_code = 0
    try:
        config = load_config(args.config)
        store = Store(args.state)
        store.start(config, refresh=args.refresh, export_only=args.export_only)
        initialized = True
        store.apply_exclusions(config)
        if args.retry_unresolved:
            with store.db:
                store.db.execute("UPDATE partitions SET status='pending',page=1,seen_ids='[]',items_json='[]',expected_count=NULL,reason=NULL WHERE status='unresolved'")
        if not args.export_only:
            client = HttpClient(Budget(args.max_seconds - 10), os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
            last_report = time.monotonic()
            while True:
                client.budget.check()
                # Commit each unit of work before moving on. A killed Actions
                # runner never turns an unfinished page into a completed one.
                progressed = step_collection(store, client, config)
                if not progressed:
                    break
                if time.monotonic() - last_report >= 30:
                    print(json_text({"progress": store.counts()}), flush=True)
                    last_report = time.monotonic()
    except BudgetExpired as error:
        print(str(error), file=sys.stderr)
    except (CollectorError, sqlite3.Error, json.JSONDecodeError, OSError) as error:
        print(f"Collection paused: {error}", file=sys.stderr)
        exit_code = 1
    finally:
        if store is not None and initialized:
            try:
                manifest = export(store, config, args.output)
                print(json_text({"output": str(args.output), "complete": manifest.get("complete"), **store.counts()}), flush=True)
                if args.require_complete and not manifest.get("complete") and exit_code == 0:
                    exit_code = 2
            except (OSError, ValueError, ImportError, sqlite3.Error) as error:
                print(f"Could not export the catalog: {error}", file=sys.stderr)
                exit_code = 1
            finally:
                store.db.close()
        elif store is not None:
            store.db.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
