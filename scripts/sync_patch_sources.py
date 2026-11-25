#!/usr/bin/env python3
"""Add patch sources published by Patch Bundle Registry to the README."""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import sys
import time
from http.client import HTTPException
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from check_repo_links import (
    MARKDOWN_LINK,
    find_not_found,
    link_url,
    listed_repositories,
    prune_not_found,
    split_web_link,
    valid_repo_path,
)
from update_repo_activity import (
    DEFAULT_ACTIVE_DAYS,
    DEFAULT_PARTIALLY_DAYS,
    classify_repo,
    reorder_section_lines,
    status_badge,
)

API_BASE = "https://patch-bundles.api.jmancentral.com"
PAGE_SIZE = 100
PATCHES_HEADING = "## 🩹 Patches"
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
    id url enabled
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
    for delay in (2, 5, None):
        try:
            with urlopen(request, timeout=25) as response:
                result = json.load(response)
        except HTTPError as exc:
            if delay is None or exc.code not in (502, 503, 504):
                raise
            print(f"Registry returned HTTP {exc.code}. Retrying in {delay} seconds.", flush=True)
            exc.close()
            time.sleep(delay)
            continue

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


def repository_key(host: str, path: str) -> tuple[str, str]:
    key = (host, path.removesuffix(".git").lower())
    return REPO_ALIASES.get(key, key)


def readme_repository_links(readme: str) -> set[tuple[str, str]]:
    repositories = set()
    for link in MARKDOWN_LINK.finditer(readme):
        parsed = split_web_link(link_url(link))
        if parsed is None:
            continue
        segments = parsed.path.strip("/").split("/")
        # Release and other subpage links also count as links to the repository.
        for end in range(2, len(segments) + 1):
            path = "/" + "/".join(segments[:end])
            repositories.add(repository_key(parsed.hostname or "", path))
    return repositories


def catalog_repositories(sources: list[dict]) -> dict[str, dict]:
    repositories = {}
    for source in sources:
        if not source["enabled"] or not source["formats"]:
            continue
        url = urlsplit(source["url"])
        host = url.hostname or ""
        path = url.path.rstrip("/").removesuffix(".git")
        if (
            url.scheme != "https"
            or url.netloc.lower() != host
            or not REPO_HOST.fullmatch(host)
            or not valid_repo_path(path)
            or url.query
            or url.fragment
        ):
            raise ValueError(f"Invalid repository URL for registry source {source['id']}")
        repositories[f"https://{host}{path}"] = source
    return repositories


def missing_sources(repositories: dict[str, dict], readme: str, dead: set[str]) -> dict[str, dict]:
    seen = readme_repository_links(readme)
    missing = {}
    for repo_url, source in repositories.items():
        if repo_url.lower() in dead:
            continue
        url = urlsplit(repo_url)
        key = repository_key(url.hostname or "", url.path)
        if key not in seen:
            seen.add(key)
            missing[repo_url] = source
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


def render_entries(repositories: dict[str, dict], now: dt.datetime) -> list[str]:
    entries = []
    for repo_url, source in repositories.items():
        parsed = urlsplit(repo_url)
        slug = parsed.path.strip("/")
        namespace, name = slug.rsplit("/", 1)
        label = f"{namespace}/{name}" if name.casefold() in GENERIC_REPO_NAMES else name
        formats = {bundle["bundle_type"] for bundle in source["current"] or source["formats"]}
        families = dict.fromkeys(FAMILIES[bundle] for bundle in FAMILIES if bundle in formats)
        if families:
            label += f" (uses {', '.join(families)})"
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
        line = f"- [![{label}]({badge})]({repo_url}) {status_badge(status)}"
        entries.append((label.casefold(), repo_url.lower(), line))
    return [line for _, _, line in sorted(entries)]


def patch_section(lines: list[str]) -> tuple[int, int]:
    headings = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == PATCHES_HEADING]
    if len(headings) != 1:
        raise ValueError("Expected exactly one Patches heading")
    first = headings[0] + 1
    end = next((i for i in range(first, len(lines)) if lines[i].startswith("## ")), len(lines))
    return first, end


def adopt_registry_entries(lines: list[str]) -> list[str]:
    section_start, section_end = patch_section(lines)
    opening = "<!-- patch-registry:start -->"
    closing = "<!-- patch-registry:end -->"
    markers = [
        (i, line.strip())
        for i, line in enumerate(lines[section_start:section_end], start=section_start)
        if line.strip() in (opening, closing)
    ]
    if not markers:
        return lines
    if [marker for _, marker in markers] != [opening, closing]:
        raise ValueError("Invalid markers for the old registry section")
    start, end = (i for i, _ in markers)
    before = lines[section_start:start]
    while before and not before[-1].strip():
        before.pop()

    seen = readme_repository_links("".join(lines[:start] + lines[end + 1:]))
    adopted = []
    for line in lines[start + 1:end]:
        if not line.strip() or line.strip() == "### From Patch Bundle Registry":
            continue
        link = MARKDOWN_LINK.search(line)
        url = split_web_link(link_url(link)) if link else None
        if line.startswith("- ") and url is not None:
            key = repository_key(url.hostname or "", url.path.rstrip("/"))
            if key in seen:
                continue
            seen.add(key)
        adopted.append(line)

    # A blank before the first bullet is still needed if the old block was the only list.
    if adopted and before and not any(line.startswith("- ") for line in before):
        adopted.insert(0, "\r\n" if before[-1].endswith("\r\n") else "\n")
    return lines[:section_start] + before + adopted + lines[end + 1:]


def sync(readme: str) -> str:
    newline = "\r\n" if "\r\n" in readme else "\n"
    lines = readme.splitlines(keepends=True)
    patch_section(lines)

    try:
        sources = fetch_sources()
    except (OSError, HTTPException) as exc:
        print(f"Could not fetch registry sources ({exc}). Checking existing repositories.", file=sys.stderr)
        sources = []
    catalog = catalog_repositories(sources)
    known_urls = {url.lower() for url in catalog}
    repositories = listed_repositories(lines, known_urls) | catalog.keys()
    dead = find_not_found(repositories)
    lines = adopt_registry_entries(prune_not_found(lines, dead, known_urls))

    sources = missing_sources(catalog, "".join(lines), dead)
    entries = render_entries(sources, dt.datetime.now(dt.timezone.utc))
    print(f"New patch sources to add: {len(entries)}")
    if not entries and "".join(lines) == readme:
        return readme

    start, end = patch_section(lines)
    insert_at = end
    while insert_at > start and not lines[insert_at - 1].strip():
        insert_at -= 1
    if entries and not lines[insert_at - 1].endswith(("\r", "\n")):
        lines[insert_at - 1] += newline
    new_lines = [line + newline for line in entries]
    body = reorder_section_lines(lines[start:insert_at] + new_lines + lines[insert_at:end])
    return "".join(lines[:start] + body + lines[end:])


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
