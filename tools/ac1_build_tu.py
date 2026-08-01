#!/usr/bin/env python3
"""Rebuild the official Assassin's Creed TU1 with a Data360.forge overlay."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath

import stfs_rebuild_replace as rebuild


DEFAULT_TIMESTAMP = bytes.fromhex("5ceb8e7f5ceb8e7f")
EXPECTED_TITLE_ID = 0x555307D4
EXPECTED_MEDIA_ID = 0x59A9DD10
EXPECTED_BASE_VERSION = 0x00000005
EXPECTED_TUPD_TARGET = 0x00000105


def entries_for(builder, payload: dict[str, bytes]):
    entries = []
    indexes: dict[str, int] = {}
    directories: set[PurePosixPath] = set()
    for text in payload:
        path = PurePosixPath(text)
        directories.update(parent for parent in path.parents if str(parent) != ".")

    for directory in sorted(
        directories, key=lambda value: (len(value.parts), value.as_posix().casefold())
    ):
        parent_text = directory.parent.as_posix()
        parent = -1 if parent_text == "." else indexes[parent_text]
        indexes[directory.as_posix()] = len(entries)
        entries.append(
            builder.BuildEntry(directory.name, True, parent, None, DEFAULT_TIMESTAMP)
        )

    # Keep the executable delta first, matching the official AC1 TU and the
    # known-working expanded title updates used by the Xbox 360 kernel.  An
    # alphabetic rebuild placed default.xexp behind hundreds of megabytes of
    # added data; Aurora listed that package, but the game did not mount any of
    # its overlay files.
    ordered_files = sorted(
        payload.items(),
        key=lambda item: (
            item[0].casefold() != "default.xexp",
            item[0].casefold(),
        ),
    )
    for text, data in ordered_files:
        path = PurePosixPath(text)
        parent_text = path.parent.as_posix()
        parent = -1 if parent_text == "." else indexes[parent_text]
        entries.append(
            builder.BuildEntry(path.name, False, parent, data, DEFAULT_TIMESTAMP)
        )
    return entries


def set_utf16be(header: bytearray, offset: int, size: int, value: str) -> None:
    encoded = (value + "\0").encode("utf-16be")
    if len(encoded) > size:
        raise ValueError(f"metadata text is too long: {value}")
    header[offset : offset + size] = encoded.ljust(size, b"\0")


def u32be(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--default-xexp", required=True, type=Path)
    parser.add_argument("--data360", required=True, type=Path)
    parser.add_argument("--masyaf", type=Path)
    parser.add_argument("--language", type=Path)
    parser.add_argument("--builder", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--display-name", default="Assassin's Creed TU1 CZ GUI test v1"
    )
    args = parser.parse_args()

    template = args.template.read_bytes()
    builder = rebuild.load_builder(args.builder)
    if template[:4] not in (b"LIVE", b"PIRS") or len(template) < builder.HEADER_BASE:
        raise ValueError("invalid STFS Title Update template")
    if u32be(template, 0x344) != 0x000B0000:
        raise ValueError("template is not a Title Update")
    if u32be(template, 0x354) != EXPECTED_MEDIA_ID:
        raise ValueError("template Media ID does not match Assassin's Creed")
    if u32be(template, 0x35C) != EXPECTED_BASE_VERSION:
        raise ValueError("template base version is unexpected")
    if u32be(template, 0x360) != EXPECTED_TITLE_ID:
        raise ValueError("template Title ID does not match Assassin's Creed")
    if template[0x971A:0x971E] != b"TUPD":
        raise ValueError("template has no TUPD metadata")
    if u32be(template, 0x9722) != EXPECTED_TUPD_TARGET:
        raise ValueError("template TUPD target version is unexpected")

    payload = {
        "Data360.forge": args.data360.read_bytes(),
        "default.xexp": args.default_xexp.read_bytes(),
    }
    if args.masyaf:
        payload["Data360_Masyaf.forge"] = args.masyaf.read_bytes()
    if args.language:
        payload["Data360_StreamedSoundsEng.forge"] = args.language.read_bytes()
    entries = entries_for(builder, payload)
    content, top_hash, allocated, table_blocks, next_map = rebuild.build_content_full(
        builder, entries
    )

    header = bytearray(template[: builder.HEADER_BASE])
    header[0x34C:0x354] = len(content).to_bytes(8, "big")
    header[0x37C:0x37E] = table_blocks.to_bytes(2, "little")
    header[0x37E:0x381] = builder.write_u24le(0)
    header[0x381:0x395] = top_hash
    header[0x395:0x399] = allocated.to_bytes(4, "big")
    header[0x399:0x39D] = (0).to_bytes(4, "big")
    set_utf16be(header, 0x411, 0x100, args.display_name)
    set_utf16be(
        header,
        0xD11,
        0x100,
        "České GUI z PC, xboxové ovládání; první strukturálně bezpečný test",
    )
    header[0x32C:0x340] = builder.sha1(bytes(header[0x344 : builder.HEADER_BASE]))
    package = bytes(header) + content

    parsed = builder.LocalStfs(package)
    files = {
        parsed.path(entry).replace("\\", "/"): entry
        for entry in parsed.entries
        if not entry.is_directory
    }
    if set(files) != set(payload):
        raise AssertionError(f"payload paths differ: {sorted(files)}")
    for path, expected in payload.items():
        if parsed.read_file(files[path]) != expected:
            raise AssertionError(f"payload round-trip failed: {path}")
    checks = rebuild.validate_hashes_full(builder, package, parsed, next_map)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(package)
    print(f"output={args.output}")
    print(
        f"size={len(package)} "
        f"sha256={hashlib.sha256(package).hexdigest().upper()}"
    )
    print(f"files={sorted(files)} checks={checks}")


if __name__ == "__main__":
    main()
