#!/usr/bin/env python3
"""Add patch sources published by Patch Bundle Registry to the README."""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from update_repo_activity import (
    DEFAULT_ACTIVE_DAYS,
    DEFAULT_PARTIALLY_DAYS,
    PATCH_REGISTRY_END,
    PATCH_REGISTRY_START,
    STATUS_ORDER,
    classify_repo,
    iter_repo_lines,
    status_badge,
    status_from_badge,
)

API_BASE = "https://patch-bundles.api.jmancentral.com"
PAGE_SIZE = 100
PATCHES_HEADING = "## 🩹 Patches"
README_LINK = re.compile(r"\]\((https://[^\s)]+)\)")
REPO_PATH = re.compile(r"(?:/[A-Za-z0-9_.-]+){2,}")
REPO_HOST = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)+")
REPO_ALIASES = {
    ("github.com", "/revanced/revanced-patches"): ("gitlab.com", "/revanced/revanced-patches"),
    ("github.com", "/imxeren/mix-patches"): ("gitlab.com", "/imxeren/mix-patches"),
}
GENERIC_REPO_NAMES = {"patches", "custom-patches", "revanced-patches", "morphe-patches", "reseam-patches"}
FAMILIES = {
    "ReVanced:v21": "ReVanced",
    "ReVanced:v22": "ReVanced",
    "Morphe:V1": "Morphe",
    "Reseam": "Reseam",
}
CATALOG_SOURCES = """
query CatalogSources($after: Int!, $limit: Int!) {
  catalog_generation
  source_aggregate { aggregate { count } }
  source(where: {id: {_gt: $after}}, order_by: {id: asc}, limit: $limit) {
    id url enabled unavailable_reason
    source_metadatum { repo_pushed_at is_repo_archived }
    current: bundles(where: {is_latest: {_eq: true}}, distinct_on: [bundle_type],
                     order_by: {bundle_type: asc}, limit: 8) { bundle_type }
    formats: bundles(distinct_on: [bundle_type], order_by: {bundle_type: asc},
                     limit: 8) { bundle_type }
  }
}
"""


def fetch_source_page(after: int) -> dict:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "awesome-android-patching-registry-sync",
    }
    payload = {"query": CATALOG_SOURCES, "variables": {"after": after, "limit": PAGE_SIZE}}
    request = Request(f"{API_BASE}/graphql", data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urlopen(request, timeout=25) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("Invalid registry response")
    if result.get("errors"):
        raise ValueError(f"Registry GraphQL error after source ID {after}: {result['errors']}")
    return result["data"]


def fetch_sources() -> list[dict]:
    sources = []
    after = 0
    total = None
    generation = None
    while total is None or len(sources) < total:
        page = fetch_source_page(after)
        rows = page["source"]
        page_total = page["source_aggregate"]["aggregate"]["count"]
        page_generation = page["catalog_generation"]
        if not isinstance(page_generation, str) or not page_generation:
            raise ValueError("Registry did not return a catalog generation")
        if generation is None:
            generation = page_generation
        elif page_generation != generation:
            raise ValueError("Registry updated during the sync. Run again against a single catalog generation.")
        if total is None:
            total = page_total
        if (
            type(page_total) is not int
            or total <= 0
            or page_total != total
            or not isinstance(rows, list)
            or len(rows) != min(PAGE_SIZE, total - len(sources))
        ):
            raise ValueError(f"Incomplete registry page after source ID {after}")
        for source in rows:
            source_id = source["id"]
            if (
                type(source_id) is not int
                or source_id <= after
                or type(source["enabled"]) is not bool
                or not isinstance(source["current"], list)
                or not isinstance(source["formats"], list)
                or not isinstance(source["url"], str)
            ):
                raise ValueError(f"Invalid or out-of-order registry source after ID {after}")
            after = source_id
        sources.extend(rows)
    return sources


def readme_repository_links(readme: str) -> set[tuple[str, str]]:
    repositories = set()
    for link in README_LINK.findall(readme):
        parsed = urlsplit(link)
        if not parsed.hostname:
            continue
        segments = parsed.path.strip("/").split("/")
        # Release and other subpage links also count as links to the repository.
        for end in range(2, len(segments) + 1):
            path = "/" + "/".join(segments[:end]).removesuffix(".git").lower()
            key = (parsed.hostname, path)
            repositories.add(REPO_ALIASES.get(key, key))
    return repositories


def missing_sources(sources: list[dict], curated: str) -> list[dict]:
    seen = readme_repository_links(curated)
    missing = []
    for source in sources:
        if not source["enabled"] or source.get("unavailable_reason") or not source["formats"]:
            continue
        url = urlsplit(source["url"])
        host = url.hostname or ""
        path = url.path.rstrip("/").removesuffix(".git")
        if (
            url.scheme != "https"
            or url.netloc.lower() != host
            or not REPO_HOST.fullmatch(host)
            or not REPO_PATH.fullmatch(path)
            or {".", ".."}.intersection(path.split("/"))
            or url.query
            or url.fragment
        ):
            raise ValueError(f"Invalid repository URL for registry source {source['id']}")
        key = (host, path.lower())
        key = REPO_ALIASES.get(key, key)
        if key not in seen:
            seen.add(key)
            missing.append(source)
    return missing


def last_commit_badge(host: str, slug: str, label: str) -> str:
    params = {"label": label}
    if host == "github.com":
        route = f"github/last-commit/{slug}"
        params["logo"] = "github"
    elif host == "gitlab.com":
        # A subgroup path is a single project parameter in Shields.
        project = quote(slug, safe="/" if slug.count("/") == 1 else "")
        route = f"gitlab/last-commit/{project}"
        params["logo"] = "gitlab"
    elif host in {"codeberg.org", "git.reseam.app", "gitea.com"}:
        route = f"gitea/last-commit/{slug}"
        params.update(gitea_url=f"https://{host}", logo="gitea")
    else:
        route = f"badge/{quote(label.replace('-', '--').replace('_', '__'), safe='')}-gray"
        params = {}
    query = urlencode(params, quote_via=quote)
    return f"https://img.shields.io/{route}" + (f"?{query}" if query else "")


def render_entries(
    sources: list[dict], now: dt.datetime, github_statuses: dict[str, str],
) -> list[str]:
    entries = []
    for source in sources:
        parsed = urlsplit(source["url"])
        slug = parsed.path.strip("/").removesuffix(".git")
        namespace, name = slug.rsplit("/", 1)
        label = f"{namespace}/{name}" if name.casefold() in GENERIC_REPO_NAMES else name
        formats = {bundle["bundle_type"] for bundle in source["current"] or source["formats"]}
        families = dict.fromkeys(FAMILIES[bundle] for bundle in FAMILIES if bundle in formats)
        if families:
            label += f" (uses {', '.join(families)})"
        # The daily activity workflow refreshes existing GitHub badges directly.
        status = github_statuses.get(slug.casefold()) if parsed.hostname == "github.com" else None
        if status is None:
            metadata = source.get("source_metadatum") or {}
            status = classify_repo(
                {
                    "full_name": slug,
                    "pushed_at": metadata.get("repo_pushed_at"),
                    "archived": bool(metadata.get("is_repo_archived")),
                },
                now,
                DEFAULT_ACTIVE_DAYS,
                DEFAULT_PARTIALLY_DAYS,
            ).status
        badge = last_commit_badge(parsed.hostname or "", slug, label)
        repo_url = f"https://{parsed.hostname}/{slug}"
        line = f"- [![{label}]({badge})]({repo_url}) {status_badge(status)}"
        entries.append((STATUS_ORDER.index(status), label.casefold(), repo_url.lower(), line))
    return [line for _, _, _, line in sorted(entries)]


def sync(readme: str) -> str:
    newline = "\r\n" if "\r\n" in readme else "\n"
    lines = readme.splitlines(keepends=True)
    markers = (PATCHES_HEADING, PATCH_REGISTRY_START, PATCH_REGISTRY_END)
    positions = {marker: [] for marker in markers}
    for i, line in enumerate(lines):
        marker = line.rstrip("\r\n")
        if marker in positions:
            positions[marker].append(i)
    if any(len(indices) != 1 for indices in positions.values()):
        raise ValueError("Expected exactly one Patches heading and one pair of registry markers")
    heading, start, end = (positions[marker][0] for marker in markers)
    next_section = next((i for i in range(heading + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    if not heading < start < end < next_section:
        raise ValueError("Registry markers must be in order within the Patches section")

    curated = "".join(lines[:start] + lines[end + 1:])
    sources = missing_sources(fetch_sources(), curated)
    github_statuses = {
        slug.casefold(): status
        for _, line, slug in iter_repo_lines(lines[start + 1:end])
        if (status := status_from_badge(line)) is not None
    }
    entries = render_entries(sources, dt.datetime.now(dt.timezone.utc), github_statuses)
    generated = ["", "### From Patch Bundle Registry", "", *entries, ""] if entries else []
    print(f"Registry sources listed in the generated section: {len(entries)}")
    return "".join(lines[:start + 1]) + "".join(f"{line}{newline}" for line in generated) + "".join(lines[end:])


def replace_readme(path: Path, expected: bytes, updated: str) -> None:
    mode = path.stat().st_mode
    with NamedTemporaryFile(dir=path.parent, prefix=".patch-sources-", delete=False) as file:
        scratch = Path(file.name)
        try:
            file.write(updated.encode("utf-8"))
            file.close()
            scratch.chmod(mode)
            if path.read_bytes() != expected:
                raise ValueError("README changed during the sync. Run again to include the latest edits.")
            scratch.replace(path)
        finally:
            scratch.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--dry-run", action="store_true", help="Show the README diff without writing changes")
    args = parser.parse_args()
    try:
        original_bytes = args.readme.read_bytes()
        original = original_bytes.decode("utf-8")
        updated = sync(original)
        if updated == original:
            print("No changes to the README")
        elif args.dry_run:
            diff = difflib.unified_diff(
                original.splitlines(),
                updated.splitlines(),
                fromfile="README.md (current)",
                tofile="README.md (proposed)",
                lineterm="",
            )
            print("\n".join(diff))
        else:
            replace_readme(args.readme, original_bytes, updated)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Registry sync failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
