"""Select full PR CI for authors without write access to the base repository."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")
INTERNAL_PERMISSIONS = frozenset({"admin", "write"})
EXTERNAL_PERMISSIONS = frozenset({"read", "none"})


def external_author(repository: str, author: str, token: str) -> bool:
    """Use GitHub's effective repository permission, including team grants."""
    parts = repository.split("/")
    if len(parts) != 2 or not all(IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError("invalid base repository")
    if not IDENTIFIER.fullmatch(author):
        raise ValueError("invalid PR author")
    if not token:
        raise ValueError("GITHUB_TOKEN is required to determine PR author permission")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/collaborators/{author}/permission",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload: object = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise RuntimeError("cannot determine PR author permission") from exc
    permission = (
        cast("dict[str, object]", payload).get("permission") if isinstance(payload, dict) else None
    )
    if permission in INTERNAL_PERMISSIONS:
        return False
    if permission in EXTERNAL_PERMISSIONS:
        return True
    raise ValueError(f"unexpected repository permission: {permission!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--author", default="")
    args = parser.parse_args()
    external = (
        external_author(args.repository, args.author, os.environ.get("GITHUB_TOKEN", ""))
        if args.event == "pull_request"
        else False
    )
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        raise ValueError("GITHUB_OUTPUT is required")
    with Path(output).open("a", encoding="utf-8") as stream:
        stream.write(f"external={str(external).lower()}\n")
    print(f"external PR author: {str(external).lower()}")


if __name__ == "__main__":
    main()
