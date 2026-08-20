"""Picks the version the next image should be published under.

The major line is declared by hand in ``pyproject.toml``; the minor is assigned
here from what is already on Docker Hub, so an ordinary merge to master never
has to touch the version and never collides with a published tag.

Docker Hub is the source of truth rather than the repository: it is the thing
the tag would collide with, it needs no commit back to master (so two merges
landing together cannot race), and a run that pushed an image but failed
afterwards is simply taken into account by the next one.

Usage in CI::

    python3 .github/scripts/next_version.py mitetenov/supportbot >> "$GITHUB_OUTPUT"
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path

#: X.Y.Z and nothing else — a suffixed tag like "2.4.0-rc1" is not a release
#: this scheme has published and must not be allowed to move the counter.
VERSION_TAG = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

TAGS_URL = "https://hub.docker.com/v2/repositories/{repository}/tags?page_size=100"
REQUEST_TIMEOUT_SECONDS = 30
MAX_PAGES = 50


def major_from_pyproject(path: Path) -> int:
    """The major component of the version declared in pyproject.toml."""
    with open(path, "rb") as handle:
        version = str(tomllib.load(handle)["project"]["version"])
    head = version.split(".")[0]
    if not head.isdigit():
        raise ValueError(f"cannot read a major version out of {version!r} in {path}")
    return int(head)


def next_version(major: int, published: Iterable[str]) -> str:
    """The next unused ``major.minor.0`` given the tags already published.

    Comparison is numeric: as text "2.9.0" sorts above "2.10.0", which would
    hand back a version that is already taken.
    """
    minors = [
        int(match.group(2))
        for tag in published
        if (match := VERSION_TAG.match(tag.strip())) and int(match.group(1)) == major
    ]
    return f"{major}.{max(minors) + 1 if minors else 0}.0"


def published_tags(repository: str) -> list[str]:
    """Every tag on the Docker Hub repository, following pagination.

    Raises rather than returning a short list: treating an unreachable registry
    as "nothing is published" would hand back a version that already exists and
    overwrite the image it points at.
    """
    tags: list[str] = []
    url: str | None = TAGS_URL.format(repository=repository)

    for _ in range(MAX_PAGES):
        if url is None:
            return tags
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
        tags.extend(str(result["name"]) for result in payload.get("results", []))
        url = payload.get("next")

    raise RuntimeError(f"{repository} has more tag pages than expected ({MAX_PAGES})")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <docker-hub-repository>", file=sys.stderr)
        return 2

    repository = argv[1]
    major = major_from_pyproject(Path("pyproject.toml"))

    try:
        tags = published_tags(repository)
    except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as error:
        print(f"::error::could not list tags of {repository}: {error}", file=sys.stderr)
        return 1

    version = next_version(major, tags)
    print(f"::notice::publishing {repository}:{version}", file=sys.stderr)
    print(f"version={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
