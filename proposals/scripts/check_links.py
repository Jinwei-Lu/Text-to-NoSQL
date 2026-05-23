#!/usr/bin/env python3
"""Validate markdown cross-reference links under a directory tree."""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
EXPLICIT_ANCHOR_PATTERN = re.compile(r'<a\s+id="([^"]+)"\s*></a>', re.IGNORECASE)
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
SKIP_URL_PREFIXES = ("http://", "https://", "mailto:", "#", "cursor://")


def slugify_heading(text: str) -> str:
    """Approximate GitHub heading anchor slug generation."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = text.strip().lower()
    normalized = unicodedata.normalize("NFKD", text)
    slug_chars: list[str] = []
    for char in normalized:
        if char.isalnum() or char in " -_":
            slug_chars.append(char)
        elif char.isspace():
            slug_chars.append("-")
    slug = "".join(slug_chars)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def collect_anchors(content: str) -> set[str]:
    anchors = set(EXPLICIT_ANCHOR_PATTERN.findall(content))
    for match in HEADING_PATTERN.finditer(content):
        heading_text = match.group(2).strip()
        if heading_text:
            anchors.add(slugify_heading(heading_text))
    return anchors


def split_link_target(target: str) -> tuple[str, str | None]:
    if "#" in target:
        path_part, fragment = target.split("#", 1)
        return path_part, fragment or None
    return target, None


def is_markdown_link(target: str) -> bool:
    if not target or target.startswith(SKIP_URL_PREFIXES):
        return False
    path_part, _ = split_link_target(target)
    return path_part.endswith(".md") or path_part == ""


def check_file(md_path: Path, root: Path) -> list[str]:
    errors: list[str] = []
    try:
        content = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{md_path.relative_to(root)}: unreadable: {exc}"]

    for match in LINK_PATTERN.finditer(content):
        target = match.group(1).strip()
        if not is_markdown_link(target):
            continue

        path_part, fragment = split_link_target(target)
        if path_part == "":
            target_path = md_path
            target_content = content
        else:
            target_path = (md_path.parent / path_part).resolve()
            if not target_path.is_file():
                rel_source = md_path.relative_to(root)
                errors.append(
                    f"{rel_source}: broken file link -> {target} "
                    f"(resolved {target_path})"
                )
                continue
            try:
                target_content = target_path.read_text(encoding="utf-8")
            except OSError as exc:
                rel_source = md_path.relative_to(root)
                errors.append(
                    f"{rel_source}: cannot read target {target_path}: {exc}"
                )
                continue

        if fragment is None:
            continue

        anchors = collect_anchors(target_content)
        if fragment not in anchors:
            rel_source = md_path.relative_to(root)
            rel_target = (
                md_path.relative_to(root)
                if target_path == md_path
                else target_path.relative_to(root)
            )
            errors.append(
                f"{rel_source}: missing anchor #{fragment} in {rel_target} "
                f"(link target {target})"
            )

    return errors


def iter_markdown_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.md"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check markdown file links and anchors under a root directory."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default="proposals",
        help="Root directory to scan (default: proposals/)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 1

    all_errors: list[str] = []
    for md_path in iter_markdown_files(root):
        all_errors.extend(check_file(md_path, root))

    if all_errors:
        print(f"Found {len(all_errors)} broken link(s):\n")
        for error in all_errors:
            print(error)
        return 1

    md_count = len(iter_markdown_files(root))
    print(f"OK: all links valid in {md_count} markdown file(s) under {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
