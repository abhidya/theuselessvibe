#!/usr/bin/env python3
"""Check that the static catalog is internally consistent before deployment."""
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime
from export_catalog import popularity_key


def check(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["schemaVersion"] == 1, "Unsupported catalog schema"
    assert isinstance(manifest["complete"], bool), "Missing collection coverage"

    def read(key):
        file = (directory / key).resolve()
        assert file.is_relative_to(directory), "Catalog path escaped data directory"
        return json.loads(file.read_text())

    sites = []
    for shard in manifest["shards"]:
        values = read(shard["key"])
        assert len(values) == shard["count"], "Shard count mismatch"
        sites.extend(values)
    assert len(sites) == manifest["eligibleSites"], "Manifest count mismatch"
    assert len({site["id"] for site in sites}) == len(sites), "Duplicate site IDs"
    popularity = manifest.get("sort") == "stars-desc"
    assert manifest.get("sort") in (None, "stars-desc"), "Unsupported catalog ordering"
    order = popularity_key if popularity else lambda site: (site["updatedAt"], site["id"])
    assert sites == sorted(sites, key=order, reverse=True), "Catalog ordering mismatch"
    index = read(manifest["indexKey"])
    assert len(index) == len(sites), "Filter index count mismatch"
    for position, site in enumerate(sites):
        url = urlsplit(site["url"])
        assert url.scheme == "https" and url.hostname and url.hostname.endswith(".github.io"), "Unexpected website origin"
        assert not url.username and not url.password and url.port in (None, 443), "Unexpected website authority"
        stars = site.get("stars")
        assert site["license"] == "MIT", "Site no longer matches license metadata"
        assert (stars is None and popularity) or (isinstance(stars, int) and not isinstance(stars, bool)
            and stars >= 0 and (popularity or stars < 2)), "Invalid observed star count"
        assert index[position]["id"] == site["id"] and index[position]["index"] == position, "Filter index points to wrong site"
        if popularity:
            assert "stars" in site and "starsCheckedAt" in site, "Missing star observation metadata"
            assert index[position]["stars"] == stars, "Filter index star count mismatch"
            assert index[position]["starsCheckedAt"] == site["starsCheckedAt"], "Filter index star timestamp mismatch"
            timestamp = site["starsCheckedAt"]
            assert timestamp is None or (isinstance(timestamp, str) and
                datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo is not None), "Invalid star observation timestamp"
            assert stars is not None or timestamp is None, "Unknown star count cannot have an observation timestamp"
    print(f"Catalog checked: {len(sites)} sites; collection complete={manifest['complete']}")


if __name__ == "__main__":
    check(sys.argv[1] if len(sys.argv) > 1 else "site/data")
