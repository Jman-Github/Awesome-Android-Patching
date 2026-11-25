"""Check the public repository links in the README and remove confirmed 404s."""
from __future__ import annotations

import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import SplitResult, quote, urlsplit
from urllib.request import Request, urlopen

LIST_ITEM = re.compile(r"^(?P<indent> *)-[ \t]+")
MARKDOWN_LINK = re.compile(
    r"(?<!!)\[(?:!\[(?P<badge_label>[^\]\r\n]*)\]\([^\r\n]*?\)|(?P<label>[^\[\]\r\n]+))\]"
    r"\([ \t]*(?:<(?P<angle_url>https://[^\s<>]+)>|(?P<url>https://[^\s<>()]+))"
    r"""(?:[ \t]+(?:"[^"\r\n]*"|'[^'\r\n]*'|\([^)\r\n]*\)))?[ \t]*\)"""
    r"|<(?P<autolink>https://[^\s<>]+)>"
)
REPO_PATH = re.compile(r"(?:/[A-Za-z0-9_.-]+){2,}")
TWO_SEGMENT_HOSTS = {"github.com", "codeberg.org", "git.reseam.app", "gitea.com"}
GITHUB_SITE_PATHS = {"apps", "collections", "marketplace", "organizations", "orgs", "sponsors", "topics", "users"}
GITLAB_SITE_PATHS = {"dashboard", "explore", "groups", "help", "users"}
FORGEJO_SITE_PATHS = {"admin", "api", "explore", "user"}


def valid_repo_path(path: str) -> bool:
    return bool(REPO_PATH.fullmatch(path)) and not {".", ".."}.intersection(path.split("/"))


def link_url(link: re.Match[str]) -> str:
    return link["url"] or link["angle_url"] or link["autolink"]


def split_web_link(url: str) -> SplitResult | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.netloc.lower() != parsed.hostname:
        return None
    return parsed


def repository_url(link: str, catalog_urls: set[str]) -> str | None:
    parsed = split_web_link(link)
    if parsed is None:
        return None

    host = parsed.hostname
    path = parsed.path.rstrip("/")
    if host in TWO_SEGMENT_HOSTS:
        segments = path.split("/")
        site_paths = GITHUB_SITE_PATHS if host == "github.com" else FORGEJO_SITE_PATHS
        if len(segments) < 3 or segments[1].lower() in site_paths:
            return None
        path = "/".join(segments[:3])
    elif host == "gitlab.com":
        path = path.split("/-/", 1)[0]
        segments = path.split("/")
        if len(segments) < 3 or segments[1].lower() in GITLAB_SITE_PATHS:
            return None
    else:
        # A catalog URL identifies repositories on other self-hosted services.
        while path:
            repo = f"https://{host}{path.removesuffix('.git')}"
            if repo.lower() in catalog_urls:
                return repo
            path = path.rsplit("/", 1)[0]
        return None

    path = path.removesuffix(".git")
    return f"https://{host}{path}" if valid_repo_path(path) else None


def listed_repositories(lines: list[str], catalog_urls: set[str]) -> set[str]:
    return {
        repo
        for line in lines if LIST_ITEM.match(line)
        for link in MARKDOWN_LINK.finditer(line)
        if (repo := repository_url(link_url(link), catalog_urls)) is not None
    }


def response_status(url: str) -> int | None:
    parsed = urlsplit(url)
    endpoint = url
    if parsed.hostname == "gitlab.com":
        project = quote(parsed.path.lstrip("/"), safe="")
        endpoint = f"https://gitlab.com/api/v4/projects/{project}"
    request = Request(endpoint, headers={"User-Agent": "awesome-android-patching-link-check"})
    try:
        with urlopen(request, timeout=12) as response:
            return response.status
    except HTTPError as exc:
        exc.close()
        return exc.code
    except (OSError, HTTPException):
        return None


def find_not_found(repositories: set[str]) -> set[str]:
    # Check each repository once, even when several sections link to it.
    urls = {url.lower(): url for url in sorted(repositories)}
    results = Counter()
    missing = set()
    print(f"Checking {len(urls)} public repository URLs for HTTP 404.", flush=True)

    def check(url: str) -> int | None:
        status = response_status(url)
        time.sleep(0.5)
        return status

    with ThreadPoolExecutor(max_workers=6) as pool:
        for key, status in zip(urls, pool.map(check, urls.values())):
            if status == 404:
                missing.add(key)
                print(f"HTTP 404: {urls[key]}")
            elif status is None:
                results["request failed"] += 1
            elif not 200 <= status < 300:
                results[f"HTTP {status}"] += 1

    print(f"Checked {len(urls)} repositories. Found {len(missing)} with HTTP 404.")
    if results:
        counts = ", ".join(f"{status}: {count}" for status, count in sorted(results.items()))
        print(f"Kept repositories with other responses ({counts}).")
    return missing


def prune_not_found(lines: list[str], missing: set[str], catalog_urls: set[str]) -> list[str]:
    kept = []
    parents: list[tuple[int, int, bool]] = []
    for line in lines:
        bullet = LIST_ITEM.match(line)
        if bullet is None:
            if line.strip() and not line[0].isspace():
                parents.clear()
            kept.append(line)
            continue

        indent = len(bullet["indent"])
        while parents and indent <= parents[-1][0]:
            parents.pop()
        new_indent = indent
        if parents:
            parent_indent, parent_new_indent, parent_removed = parents[-1]
            new_indent = parent_new_indent if parent_removed else parent_new_indent + indent - parent_indent

        links = list(MARKDOWN_LINK.finditer(line))
        dead_links = []
        for link in links:
            repo = repository_url(link_url(link), catalog_urls)
            if repo is not None and repo.lower() in missing:
                dead_links.append(link)

        remove_row = bool(dead_links) and len(dead_links) == len(links)
        parents.append((indent, new_indent, remove_row))
        if remove_row:
            continue

        if dead_links:
            name = ""
            primary = links[0]
            if primary in dead_links and primary.start() == bullet.end():
                name = primary["badge_label"] or primary["label"] or ""
            # Keep surviving links and retain the project's name when its main link is dead.
            for link in reversed(dead_links):
                start = link.start()
                while start > bullet.end() and line[start - 1] in " \t":
                    start -= 1
                line = line[:start] + line[link.end():]
            line = line[:bullet.end()] + (f"{name} " if name else "") + line[bullet.end():].lstrip(" \t")

        kept.append(line[indent - new_indent:])
    return kept
