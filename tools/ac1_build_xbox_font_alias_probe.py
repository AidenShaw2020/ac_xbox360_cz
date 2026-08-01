#!/usr/bin/env python3
"""Build a same-size AC1 Xbox font mapping probe.

The three incomplete retail GUI fonts have sparse, big-endian Unicode lookup
pages following each little-endian glyph record table. Missing characters map
to glyph zero (the Ubisoft icon). This probe changes only the U+010D and U+011B
lookup cells so they resolve to the existing lowercase c and e glyph records.
No resource, serialized object, directory, or decompressed stream changes size.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import tempfile
from pathlib import Path

from ac1_build_gui_forge import (
    replace_forge_entry,
    wrapper_source_chunks,
    xmemlzx_wrapper,
)
from ac1_forge import parse_forge
from ac1_resource_bundle import parse_bundle


FONT_TYPE = 0x3CDF1895
TARGET_FONTS = (
    "AnimusText Bold_360",
    "Animus Title",
    "AnimusSmallText 360",
)
PIXMAP_MARKER = b"PixmapFont"
GLYPH_RECORD_SIZE = 30
ALIASES = {
    0x010D: ord("c"),  # č -> c
    0x011B: ord("e"),  # ě -> e
}


def patch_font(blob: bytes, name: str) -> tuple[bytes, list[dict[str, int]]]:
    output = bytearray(blob)
    marker_offsets: list[int] = []
    cursor = 0
    while True:
        marker = blob.find(PIXMAP_MARKER, cursor)
        if marker < 0:
            break
        marker_offsets.append(marker)
        cursor = marker + len(PIXMAP_MARKER)
    if len(marker_offsets) != 5:
        raise ValueError(f"{name}: expected five Xbox PixmapFont objects")

    changes: list[dict[str, int]] = []
    for locale, marker in enumerate(marker_offsets):
        table_header = marker + len(PIXMAP_MARKER)
        glyph_count = int.from_bytes(blob[table_header : table_header + 2], "little")
        records = table_header + 2
        glyph_indices: dict[int, int] = {}
        for index in range(glyph_count):
            record = records + index * GLYPH_RECORD_SIZE
            codepoint = int.from_bytes(blob[record : record + 2], "little")
            glyph_indices[codepoint] = index

        page_zero = records + glyph_count * GLYPH_RECORD_SIZE
        # Page zero is a direct 256-entry big-endian glyph-index table.
        for codepoint in (0x20, 0x2E, 0x41, 0x61):
            expected = glyph_indices[codepoint]
            actual = int.from_bytes(
                blob[
                    page_zero + codepoint * 2 : page_zero + codepoint * 2 + 2
                ],
                "big",
            )
            if actual != expected:
                raise ValueError(
                    f"{name} locale {locale}: Unicode page-zero validation failed"
                )

        # Sparse page one follows page zero. The serializer overlaps the
        # big-endian 0x0001 page ID with the first (unused) lookup cell, so the
        # actual 16-bit glyph cells begin at the second marker byte.
        page_one_marker = page_zero + 512
        if blob[page_one_marker : page_one_marker + 2] != b"\x00\x01":
            raise ValueError(
                f"{name} locale {locale}: Unicode page-one marker is missing"
            )
        page_one = page_one_marker + 1
        for codepoint, source_codepoint in ALIASES.items():
            source_index = glyph_indices[source_codepoint]
            cell = page_one + (codepoint & 0xFF) * 2
            old_index = int.from_bytes(blob[cell : cell + 2], "big")
            output[cell : cell + 2] = source_index.to_bytes(2, "big")
            changes.append(
                {
                    "locale": locale,
                    "codepoint": codepoint,
                    "old_glyph_index": old_index,
                    "new_glyph_index": source_index,
                    "byte_offset": cell,
                }
            )

    if len(output) != len(blob):
        raise AssertionError(f"{name}: font size changed")
    return bytes(output), changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xbox-directory", required=True, type=Path)
    parser.add_argument("--xbox-data", required=True, type=Path)
    parser.add_argument("--source-forge", required=True, type=Path)
    parser.add_argument("--output-forge", required=True, type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--compress-script", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    resources = parse_bundle(args.xbox_directory, args.xbox_data, "big")
    source_data = args.xbox_data.read_bytes()
    rebuilt_data = bytearray()
    report_changes: dict[str, list[dict[str, int]]] = {}
    found: set[str] = set()

    for resource in resources:
        blob = source_data[resource.offset : resource.offset + resource.size]
        if resource.type_id == FONT_TYPE and resource.name in TARGET_FONTS:
            blob, changes = patch_font(blob, resource.name)
            report_changes[resource.name] = changes
            found.add(resource.name)
        rebuilt_data += blob

    if found != set(TARGET_FONTS):
        raise ValueError(f"font resources missing: {sorted(set(TARGET_FONTS) - found)}")
    if len(rebuilt_data) != len(source_data):
        raise AssertionError("decompressed resource stream size changed")

    archive = parse_forge(args.source_forge)
    bootstrap = next(
        entry for entry in archive.entries if entry.name == "Game Bootstrap Settings"
    )
    with args.source_forge.open("rb") as stream:
        stream.seek(bootstrap.offset + 440)
        source_raw = stream.read(bootstrap.size)
    source_chunks = wrapper_source_chunks(source_raw)
    if len(source_chunks) != 2:
        raise ValueError("expected two Game Bootstrap Settings raw wrappers")

    args.output_forge.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="ac1_font_alias_", dir=args.output_forge.parent
    ) as temporary:
        root = Path(temporary)
        raw_payload = xmemlzx_wrapper(
            args.xbox_directory.read_bytes(),
            args.quickbms,
            args.compress_script,
            root,
            "directory",
            source_chunks[0],
        ) + xmemlzx_wrapper(
            bytes(rebuilt_data),
            args.quickbms,
            args.compress_script,
            root,
            "data",
            source_chunks[1],
        )

    old_size, new_size = replace_forge_entry(
        args.source_forge,
        args.output_forge,
        "Game Bootstrap Settings",
        raw_payload,
    )
    report = {
        "mode": "same-size-xbox-font-unicode-alias-probe",
        "aliases": {f"U+{key:04X}": f"U+{value:04X}" for key, value in ALIASES.items()},
        "changes": report_changes,
        "decompressed_data_size": len(rebuilt_data),
        "original_raw_payload_size": old_size,
        "rebuilt_raw_payload_size": new_size,
        "output_forge_size": args.output_forge.stat().st_size,
        "output_forge_sha256": hashlib.sha256(
            args.output_forge.read_bytes()
        ).hexdigest().upper(),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
