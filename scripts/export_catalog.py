#!/usr/bin/env python3
"""Publish an immutable, sharded catalog from the collector's SQLite results.

The collector calls export_catalog(sites, metadata, output_dir). SQLite remains
its source of truth. This module writes only JSON files for GitHub Pages.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import Counter
from pathlib import Path
from tempfile import NamedTemporaryFile
from game_metadata import FIELDS

SHARD_SIZE = 256
MAX_SHARD_BYTES = 3_500_000  # Browser adapter accepts at most 4 MiB per detail shard.
GENERATION_PATTERN = re.compile(r"^[a-f0-9]{20}$")


def _encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _atomic_write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".catalog-", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def popularity_key(site: dict) -> tuple:
    stars = site.get("stars")
    count = stars if isinstance(stars, int) and not isinstance(stars, bool) and stars >= 0 else -1
    return count, site.get("updatedAt") or "", str(site["id"])


def export_catalog(sites: list[dict], metadata: dict, output_dir: Path) -> dict:
    """Write index, shards, then manifest, returning the completed manifest.

    Full sites retain the collector's schema. The compact index stores the
    first 160 description characters and exact signal terms. Multiple selected
    terms are interpreted as OR by the browser adapter, with agent/source as AND. Agent IDs are lowercase.
    When output_dir contains an earlier export, its generation is retained for
    clients with cached JSON. Fresh CI checkouts rely on the browser adapter's
    one-time manifest refresh when an old generation is no longer available.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Rank globally before sharding/pagination. Unknown counts follow known
    # zeroes; update time and ID make equal-count ordering deterministic.
    ordered = []
    for source in sites:
        site = dict(source)
        if popularity_key(site)[0] == -1:
            site["stars"] = None
        site.setdefault("starsCheckedAt", None)
        if site["stars"] is None:
            site["starsCheckedAt"] = None
        ordered.append(site)
    ordered.sort(key=popularity_key, reverse=True)
    ids = [str(site["id"]) for site in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("Catalog site IDs must be unique")
    for site in ordered:
        if not isinstance(site.get("id"), str):
            raise ValueError("Catalog site IDs must be strings")
    index = []
    agent_counts: Counter = Counter()
    term_counts: Counter = Counter()
    for position, site in enumerate(ordered):
        agents = sorted(set(str(agent).lower() for agent in site.get("agents", [])))
        terms = sorted(set(str(signal["term"]).lower() for signal in site.get("signals", []) if signal.get("term")))
        agent_counts.update(agents)
        term_counts.update(terms)
        index.append({
            "index": position,
            "id": site["id"],
            "repo": site["repo"],
            "name": site["name"],
            "description": (site.get("description") or "")[:160],
            "agents": agents,
            "metadata": {"schemaVersion": 1, **{f: site.get("metadata", {}).get(f, []) for f in FIELDS},
                         "playerCount": site.get("metadata", {}).get("playerCount"),
                         "sourceStatus": site.get("metadata", {}).get("sourceStatus", "pending")},
            "createdAt": site.get("createdAt"), "updatedAt": site.get("updatedAt"), "checkedAt": site.get("checkedAt"),
            "stars": site["stars"], "starsCheckedAt": site["starsCheckedAt"],
            "terms": terms,
            "pageTerms": sorted(set(str(signal["term"]).lower() for signal in site.get("signals", []) if signal.get("term") and signal.get("source") == "page")),
            "readmeTerms": sorted(set(str(signal["term"]).lower() for signal in site.get("signals", []) if signal.get("term") and signal.get("source") == "readme")),
        })
    # Include metadata so a refreshed, unchanged dataset still has a coherent
    # generation with its new collection timestamp and coverage information.
    generation = hashlib.sha256(_encode({"sites": ordered, "metadata": metadata})).hexdigest()[:20]
    generation_dir = output_dir / generation
    generation_dir.mkdir(parents=True, exist_ok=True)
    shards = []
    pending, size = [], 3
    batches = []
    for site in ordered:
        encoded_size = len(_encode(site)) + 1
        if encoded_size + 3 > MAX_SHARD_BYTES:
            raise ValueError("A catalog record exceeds the detail-shard byte budget")
        if pending and (len(pending) == SHARD_SIZE or size + encoded_size > MAX_SHARD_BYTES):
            batches.append(pending)
            pending, size = [], 3
        pending.append(site)
        size += encoded_size
    if pending:
        batches.append(pending)
    for number, values in enumerate(batches):
        key = f"{generation}/{number:04d}.json"
        _atomic_write(output_dir / key, _encode(values))
        shards.append({"key": key, "count": len(values)})
    index_key = f"{generation}/index.json"
    _atomic_write(output_dir / index_key, _encode(index))
    configured_terms = metadata.get("terms", metadata.get("phrases", []))
    configured_terms = [term.lower() for term in configured_terms if isinstance(term, str)]
    manifest = {
        "schemaVersion": 1,
        "sort": "stars-desc",
        "generation": generation,
        "query": metadata.get("query", metadata.get("baseQuery", "")),
        "searchUrl": metadata["searchUrl"],
        "generatedAt": metadata["generatedAt"],
        "matchedRepositories": metadata.get("matchedRepositories"),
        "scannedRepositories": metadata["scannedRepositories"],
        "eligibleSites": len(ordered),
        "complete": bool(metadata.get("complete", False)),
        "shardSize": SHARD_SIZE,
        "indexKey": index_key,
        "shards": shards,
        "agentCounts": {"codex": 0, "claude": 0, **dict(sorted(agent_counts.items()))},
        "terms": sorted(set(configured_terms) | set(term_counts)),
        "termCounts": dict(sorted(term_counts.items())),
    }
    for key in ("searchQueries", "unresolvedPartitions", "excludedRepositories", "coverageNote", "pendingMetadataChecks", "metadataCheckedSites", "metadataPartialSites", "pendingAttributionChecks", "unavailableAttributionChecks", "pendingStarRefreshes", "pendingScanStarRefreshes", "unavailableStarRefreshes", "carriedForwardSites", "currentScanEligibleSites", "pendingPartitions", "pendingRepositoryChecks", "discoveredRepositories", "reportedMatchesByQuery", "scanStartedAt", "scanFinishedAt", "attributionScope"):
        if key in metadata:
            manifest[key] = metadata[key]
    previous_generation = None
    manifest_path = output_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            previous_generation = json.loads(manifest_path.read_text(encoding="utf-8")).get("generation")
        except (ValueError, OSError):
            pass
    _atomic_write(manifest_path, _encode(manifest))
    # Remove only directories named by this exporter's generation format.
    # Preserve unrelated files and one prior generation for in-flight browsers.
    keep = {generation, previous_generation}
    for child in output_dir.iterdir():
        if child.is_dir() and not child.is_symlink() and GENERATION_PATTERN.fullmatch(child.name) and child.name not in keep:
            shutil.rmtree(child)
    return manifest
