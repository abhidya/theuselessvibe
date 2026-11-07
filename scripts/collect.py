#!/usr/bin/env python3
"""Discover owned GitHub Pages URLs, checkpoint in SQLite, export static data.

Search is not an atomic snapshot. We detect incomplete pages, changing totals,
and duplicate/missing results within a partition instead of calling those scans
complete. A completed scan means all observed partitions and checks finished;
it does not prove that every reachable page is a game or that it remains online.
"""

from __future__ import annotations

import argparse
import hashlib
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


SCHEMA_VERSION = 1
SEARCH_LIMIT = 1000
PAGE_SIZE = 100
MAX_README_BYTES = 1024 * 1024
MAX_PAGE_BYTES = 256 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
ATTRIBUTION_SCOPE = "Latest 30 commits and pull requests among the latest 100 issue records"
USER_AGENT = "TheUselessVibeCatalog/1.0 (+https://github.com/abhidya/theuselessvibe)"
PAGE_URL_PATTERN = re.compile(r"https://[^\s<>\"'`]+", re.IGNORECASE)


class CollectorError(Exception):
    """An actionable collection failure, without credentials in its message."""


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
    config = dict(config, baseQuery=base.strip(), phrases=normalized)
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
        parsed = urlsplit(url.strip())
        netloc = parsed.netloc.lower()
        if netloc.endswith(":443") and parsed.scheme.lower() == "https":
            netloc = netloc[:-4]
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/") or "/", "", ""))
    except ValueError:
        return url.strip()


def is_excluded(repo: dict, url: str | None, config: dict) -> bool:
    exclusions = config.get("exclusions", {})
    names = {value.strip().strip("/").casefold() for value in exclusions.get("repositories", [])}
    if str(repo.get("full_name") or "").casefold() in names:
        return True
    return bool(url and exclusion_url(url) in {exclusion_url(value) for value in exclusions.get("urls", [])})


def search_queries(config: dict) -> list[str]:
    return [f'{config["baseQuery"]} "{term}"' for term in config["phrases"]]


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
              "fork", "private", "license", "created_at", "updated_at", "pushed_at", "has_pages")
    result = {field: repo.get(field) for field in fields}
    result["owner"] = {"login": (repo.get("owner") or {}).get("login", "")}
    return result


def repository_eligible(repo: dict) -> bool:
    stars = repo.get("stargazers_count")
    return (
        repo.get("fork") is False
        and repo.get("private") is False
        and isinstance(stars, int) and not isinstance(stars, bool) and 0 <= stars < 2
        and (repo.get("license") or {}).get("spdx_id") == "MIT"
        and bool(repo.get("full_name")) and bool(repo.get("id"))
    )


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
        return urlunsplit(("https", parsed.hostname, path, parsed.query, ""))
    except (ValueError, UnicodeError):
        return None


def candidate_urls(repo: dict, readme: str = "") -> list[str]:
    explicit = [repo.get("homepage") or ""]
    for match in PAGE_URL_PATTERN.finditer(html.unescape(readme)):
        raw = match.group(0).rstrip(".,;:!\"'`")
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
            while raw.endswith(closing) and raw.count(closing) > raw.count(opening):
                raw = raw[:-1]
        explicit.append(raw)
    result = []
    for value in explicit:
        url = owned_pages_url(value, repo)
        if url is not None and url not in result:
            result.append(url)
    # Homepage first, then shallow pages ahead of screenshots/deep documentation.
    home = owned_pages_url(repo.get("homepage") or "", repo)
    return sorted(result, key=lambda url: (url != home, urlsplit(url).path.count("/"), len(url)))[:12]


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
                    if not allowed(target):
                        raise HttpFailure(400, "URL or redirect is outside the permitted host/path")
                    request = Request(target, headers=headers)
                    try:
                        response = self.opener.open(request, timeout=self.timeout)
                    except HTTPError as error:
                        response = error
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
                            raise CollectorError("GitHub response exceeded the configured size limit")
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
            CREATE TABLE IF NOT EXISTS query_matches(
                query_index INTEGER NOT NULL, repo_id TEXT NOT NULL, PRIMARY KEY(query_index, repo_id)
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(partitions)")}
        if "items_json" not in columns:
            self.db.execute("ALTER TABLE partitions ADD COLUMN items_json TEXT NOT NULL DEFAULT '[]'")
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
        return hashlib.sha256((self.get("configHash") + json_text(repo)).encode()).hexdigest()

    def add_repository(self, repo: dict, query_index: int):
        repo = compact_repository(repo)
        if not repo.get("id") or not repo.get("full_name"):
            raise CollectorError("GitHub returned a repository without an ID or name")
        identifier = str(repo["id"])
        self.db.execute("""INSERT INTO repositories(id,full_name,payload,updated_at,seen_scan)
            VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name,
            payload=excluded.payload, updated_at=excluded.updated_at, seen_scan=excluded.seen_scan,
            checked_scan=CASE WHEN repositories.payload != excluded.payload THEN NULL ELSE repositories.checked_scan END""",
            (identifier, repo["full_name"], json_text(repo), repo.get("updated_at") or repo.get("pushed_at") or repo.get("created_at") or "", self.scan))
        self.db.execute("INSERT OR IGNORE INTO query_matches VALUES(?,?)", (query_index, identifier))
        if not repository_eligible(repo):
            self.db.execute("UPDATE repositories SET site=NULL,outcome='no-longer-eligible',checked_scan=?,checked_at=? WHERE id=?", (self.scan, utc_iso(), identifier))

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
        return self.db.execute("SELECT * FROM repositories WHERE seen_scan=? AND (checked_scan IS NULL OR checked_scan!=?) ORDER BY updated_at DESC,id LIMIT 1", (self.scan, self.scan)).fetchone()

    def next_attribution(self):
        return self.db.execute("SELECT * FROM repositories WHERE seen_scan=? AND checked_scan=? AND site IS NOT NULL AND attribution_status='pending' ORDER BY updated_at DESC,id LIMIT 1", (self.scan, self.scan)).fetchone()

    def counts(self) -> dict:
        current = self.scan
        discovered = self.db.execute("SELECT COUNT(*) FROM repositories WHERE seen_scan=?", (current,)).fetchone()[0]
        checked = self.db.execute("SELECT COUNT(*) FROM repositories WHERE seen_scan=? AND checked_scan=?", (current, current)).fetchone()[0]
        current_eligible = self.db.execute("SELECT COUNT(*) FROM repositories WHERE seen_scan=? AND checked_scan=? AND site IS NOT NULL", (current, current)).fetchone()[0]
        eligible = self.db.execute("SELECT COUNT(*) FROM repositories WHERE site IS NOT NULL").fetchone()[0]
        pending_attr = self.db.execute("SELECT COUNT(*) FROM repositories WHERE seen_scan=? AND site IS NOT NULL AND attribution_status='pending'", (current,)).fetchone()[0]
        unavailable_attr = self.db.execute("SELECT COUNT(*) FROM repositories WHERE seen_scan=? AND site IS NOT NULL AND attribution_status='unavailable'", (current,)).fetchone()[0]
        pending_parts = self.db.execute("SELECT COUNT(*) FROM partitions WHERE status='pending'").fetchone()[0]
        unresolved = self.db.execute("SELECT COUNT(*) FROM partitions WHERE status='unresolved'").fetchone()[0]
        return dict(discoveredRepositories=discovered, scannedRepositories=checked, eligibleSites=eligible,
                    currentScanEligibleSites=current_eligible, carriedForwardSites=eligible-current_eligible,
                    pendingRepositoryChecks=discovered-checked, pendingAttributionChecks=pending_attr,
                    unavailableAttributionChecks=unavailable_attr, pendingPartitions=pending_parts, unresolvedPartitions=unresolved)

    def finish_if_complete(self):
        counts = self.counts()
        complete = not any(counts[key] for key in ("pendingPartitions", "unresolvedPartitions", "pendingRepositoryChecks", "pendingAttributionChecks"))
        if complete and self.get("finishedAt") is None:
            with self.db:
                self.put("finishedAt", utc_iso())
                # Retire cached records that disappeared, became private, were
                # deleted, or no longer match any configured search.
                self.db.execute("DELETE FROM repositories WHERE seen_scan!=?", (self.scan,))
        return complete

    def sites(self) -> list[dict]:
        result = []
        # Keep the previous verified catalog usable while a refresh accumulates.
        # New observations can remove entries immediately; unseen entries are
        # retired only when the entire replacement scan has completed.
        rows = self.db.execute("SELECT * FROM repositories WHERE site IS NOT NULL ORDER BY updated_at DESC,id")
        for row in rows:
            site = json.loads(row["site"])
            attribution = json.loads(row["attribution"]) if row["attribution"] else {"agents": [], "agentEvidence": []}
            site.update(attribution)
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
            self.db.executemany("UPDATE repositories SET site=NULL,outcome='excluded',checked_scan=?,check_fingerprint=NULL WHERE id=?", removals)


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
    collected_items = json.loads(partition["items_json"]) + [compact_repository(repo) for repo in items]
    final_page = partition["page"] >= max(1, math.ceil(expected / PAGE_SIZE))
    if final_page and len(seen) != expected:
        store.split_partition(partition, "Unique repository count did not match reported total")
        return True
    with store.db:
        # Do not publish candidates from a partition until all its pages agree.
        # A later split must not leave old, superseded page results in the union.
        if final_page:
            for repo in collected_items:
                store.add_repository(repo, partition["query_index"])
        store.db.execute("UPDATE partitions SET expected_count=?,seen_ids=?,items_json=?,page=?,status=? WHERE id=?",
                         (expected, json_text(sorted(seen)), "[]" if final_page else json_text(collected_items),
                          partition["page"] + 1, "done" if final_page else "pending", partition["id"]))
    return True


def cache_fresh(checked_at: str | None, days: float) -> bool:
    age = time.time() - parse_time(checked_at)
    return 0 <= age < days * 86400


def step_check(store: Store, client: HttpClient, config: dict) -> bool:
    row = store.next_check()
    if row is None:
        return False
    repo = json.loads(row["payload"])
    fingerprint = store.fingerprint(repo)
    if is_excluded(repo, None, config):
        with store.db:
            store.db.execute("UPDATE repositories SET checked_scan=?,site=NULL,outcome='excluded',check_fingerprint=NULL WHERE id=?", (store.scan, row["id"]))
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
    if repository_eligible(repo):
        readme = client.readme(repo)
        urls = candidate_urls(repo, readme)
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
            if not signals:
                outcome = "no-current-playability-signals"
                continue
            site = {
                "id": str(repo["id"]), "repo": repo["full_name"], "name": repo["name"],
                "description": str(repo.get("description") or "")[:400], "url": final_url,
                "repoUrl": "https://github.com/" + repo["full_name"], "stars": repo["stargazers_count"],
                "license": "MIT", "updatedAt": repo.get("updated_at") or repo.get("pushed_at") or repo.get("created_at"),
                "createdAt": repo.get("created_at"), "checkedAt": checked_at, "signals": signals,
            }
            outcome = "reachable"
            break
    else:
        outcome = "no-longer-eligible"
    with store.db:
        store.db.execute("UPDATE repositories SET checked_scan=?,check_fingerprint=?,checked_at=?,outcome=?,site=? WHERE id=?",
                         (store.scan, fingerprint, checked_at, outcome, json_text(site) if site else None, row["id"]))
    if site:
        refresh_attribution_state(store, row["id"], config)
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
        "coverageNote": "Search results change during collection. Prior verified sites remain available while a refresh is incomplete; carriedForwardSites counts those not yet rechecked. HTML reachability and phrase signals do not prove playability.",
        "attributionScope": ATTRIBUTION_SCOPE,
    }


def export(store: Store, config: dict, output: Path) -> dict:
    from export_catalog import export_catalog
    metadata = manifest_metadata(store, config)
    return export_catalog(store.sites(), metadata, output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(".cache/catalog.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("site/data"))
    parser.add_argument("--config", type=Path, default=Path("config/search.json"))
    parser.add_argument("--max-seconds", type=float, default=1200, help="Checkpoint before this runtime budget is exhausted")
    parser.add_argument("--refresh", action="store_true", help="Start a full new scan; reuse recent network checks and retire missing repositories when complete")
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
                progressed = step_check(store, client, config) or step_attribution(store, client) or step_partition(store, client, config)
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
