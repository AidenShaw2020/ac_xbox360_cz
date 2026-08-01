#!/usr/bin/env python3
"""Build an AC1 Xbox Data360.forge with Czech-capable Xbox GUI fonts.

The retail Xbox resource bundle contains five serialized locale copies of each
bitmap font.  AnimusTechno contains the complete Czech Latin Extended-A set,
while the other three GUI fonts omit several Czech glyphs.  This diagnostic
keeps every target resource's outer header and resource ID, but substitutes the
Xbox-native AnimusTechno serialized font bodies for the incomplete bodies.
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
SOURCE_FONT = "AnimusTechno Regular 22_360"
TARGET_FONTS = (
    "AnimusText Bold_360",
    "Animus Title",
    "AnimusSmallText 360",
)
FONT_BODY_MARKER = b"Magma Font"


def replace_font_body(target: bytes, source: bytes, name: str) -> bytes:
    target_start = target.find(FONT_BODY_MARKER)
    source_start = source.find(FONT_BODY_MARKER)
    if target_start < 0 or source_start < 0:
        raise ValueError(f"{name}: serialized font body was not found")
    if target.count(FONT_BODY_MARKER) != 5 or source.count(FONT_BODY_MARKER) != 5:
        raise ValueError(f"{name}: expected five Xbox locale font bodies")

    result = bytearray(target[:target_start] + source[source_start:])
    # Xbox resource wrapper declares the complete size after its 24-byte fixed
    # header. The target's name and resource ID remain untouched.
    struct.pack_into(">I", result, 4, len(result) - 24)
    if result[:4] != target[:4] or result[8:target_start] != target[8:target_start]:
        raise AssertionError(f"{name}: outer resource header changed")
    return bytes(result)


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
    fonts = {
        resource.name: resource
        for resource in resources
        if resource.type_id == FONT_TYPE
    }
    missing = [name for name in (SOURCE_FONT, *TARGET_FONTS) if name not in fonts]
    if missing:
        raise ValueError(f"missing Xbox font resources: {missing}")

    source_data = args.xbox_data.read_bytes()
    source_resource = fonts[SOURCE_FONT]
    source_blob = source_data[
        source_resource.offset : source_resource.offset + source_resource.size
    ]

    rebuilt_directory = bytearray(args.xbox_directory.read_bytes())
    rebuilt_data = bytearray()
    changes: list[dict[str, int | str]] = []
    for resource in resources:
        blob = source_data[resource.offset : resource.offset + resource.size]
        if resource.name in TARGET_FONTS:
            patched = replace_font_body(blob, source_blob, resource.name)
            struct.pack_into(
                ">I",
                rebuilt_directory,
                2 + resource.index * 8 + 4,
                len(patched),
            )
            changes.append(
                {
                    "resource": resource.name,
                    "old_size": len(blob),
                    "new_size": len(patched),
                }
            )
            blob = patched
        rebuilt_data += blob

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
        prefix="ac1_font_probe_", dir=args.output_forge.parent
    ) as temporary:
        root = Path(temporary)
        raw_payload = xmemlzx_wrapper(
            bytes(rebuilt_directory),
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
            force_stored_chunks={337, 338},
        )

    old_size, new_size = replace_forge_entry(
        args.source_forge,
        args.output_forge,
        "Game Bootstrap Settings",
        raw_payload,
    )
    report = {
        "mode": "xbox-native-czech-font-probe",
        "source_font": SOURCE_FONT,
        "changes": changes,
        "decompressed_directory_size": len(rebuilt_directory),
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
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
