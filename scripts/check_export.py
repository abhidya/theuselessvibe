#!/usr/bin/env python3
"""Check that the static catalog is internally consistent before deployment."""
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit


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
    assert sites == sorted(sites, key=lambda site: (site["updatedAt"], site["id"]), reverse=True), "Catalog must be newest-updated first"
    index = read(manifest["indexKey"])
    assert len(index) == len(sites), "Filter index count mismatch"
    for position, site in enumerate(sites):
        url = urlsplit(site["url"])
        assert url.scheme == "https" and url.hostname and url.hostname.endswith(".github.io"), "Unexpected website origin"
        assert not url.username and not url.password and url.port in (None, 443), "Unexpected website authority"
        assert site["license"] == "MIT" and site["stars"] in (0, 1), "Site no longer matches query metadata"
        assert index[position]["id"] == site["id"] and index[position]["index"] == position, "Filter index points to wrong site"
    print(f"Catalog checked: {len(sites)} sites; collection complete={manifest['complete']}")


if __name__ == "__main__":
    check(sys.argv[1] if len(sys.argv) > 1 else "site/data")
