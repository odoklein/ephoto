#!/usr/bin/env python3
"""Rebuild certif-ephoto-control.zip from the plugin folder (stdlib only).

Usage:  py wordpress-plugin/build_zip.py

Entries are stored under ``certif-ephoto-control/`` with forward slashes
(WordPress rejects archives built with backslash separators). Dotfiles,
dot-directories and __pycache__ are excluded.
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_SLUG = "certif-ephoto-control"
SOURCE_DIR = HERE / PLUGIN_SLUG
OUTPUT_ZIP = HERE / f"{PLUGIN_SLUG}.zip"
EXCLUDED_DIRS = {"__pycache__", "node_modules"}


def iter_entries(source: Path):
    """Yield (path, archive name) pairs, directories first, sorted for reproducibility."""
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in EXCLUDED_DIRS
        )
        current = Path(dirpath)
        rel_dir = current.relative_to(source).as_posix()
        if rel_dir != ".":
            yield current, f"{PLUGIN_SLUG}/{rel_dir}/"
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = current / name
            yield path, f"{PLUGIN_SLUG}/{path.relative_to(source).as_posix()}"


def main() -> int:
    if not SOURCE_DIR.is_dir():
        print(f"Plugin folder not found: {SOURCE_DIR}", file=sys.stderr)
        return 1

    tmp_zip = OUTPUT_ZIP.with_name(OUTPUT_ZIP.name + ".tmp")
    names = []
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(zipfile.ZipInfo(f"{PLUGIN_SLUG}/"), b"")
        names.append(f"{PLUGIN_SLUG}/")
        for path, arcname in iter_entries(SOURCE_DIR):
            if "\\" in arcname:
                raise ValueError(f"Backslash in archive name: {arcname}")
            if arcname.endswith("/"):
                archive.writestr(zipfile.ZipInfo(arcname), b"")
            else:
                archive.write(path, arcname)
            names.append(arcname)
    os.replace(tmp_zip, OUTPUT_ZIP)

    with zipfile.ZipFile(OUTPUT_ZIP) as archive:
        bad = archive.testzip()
        if bad is not None:
            print(f"Corrupted entry: {bad}", file=sys.stderr)
            return 1
        for info in archive.infolist():
            print(f"{info.file_size:>8}  {info.filename}")

    print(f"\n{len(names)} entries -> {OUTPUT_ZIP}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
