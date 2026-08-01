#!/usr/bin/env python3
"""Build an AC1 Xbox Map Menu archive with a Czech Press START string."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from ac1_build_gui_forge import (
    replace_forge_entry,
    wrapper_source_chunks,
    xmemlzx_wrapper,
)
from ac1_forge import parse_forge
from ac1_resource_bundle import parse_bundle


ENTRY_NAME = "Map_Menu"
RESOURCE_NAME = "PressStart_MGB"
SOURCE_TEXT = "< Press the START button >"
TARGET_TEXTS = {
    "cp1250-promoted": "<Stisknìte tlaèítko START>",
    "unicode": "<Stiskněte tlačítko START>",
    # U+011B/U+010D are zero-advance, preposed caron markers in the matching
    # font probe. Omitting the two decorative angle brackets leaves room for
    # both markers while preserving the original 26 UTF-16 code units exactly.
    "combining-caron": "Stiskněete tlačcítko START",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xbox-directory", required=True, type=Path)
    parser.add_argument("--xbox-data", required=True, type=Path)
    parser.add_argument("--source-forge", required=True, type=Path)
    parser.add_argument("--output-forge", required=True, type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--compress-script", required=True, type=Path)
    parser.add_argument(
        "--target-mode", choices=tuple(TARGET_TEXTS), default="cp1250-promoted"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    resources = parse_bundle(args.xbox_directory, args.xbox_data, "big")
    target = next(resource for resource in resources if resource.name == RESOURCE_NAME)
    data = bytearray(args.xbox_data.read_bytes())
    source = SOURCE_TEXT.encode("utf-16le")
    target_text = TARGET_TEXTS[args.target_mode]
    replacement = target_text.encode("utf-16le")
    if len(source) != len(replacement):
        raise AssertionError("Press START probe must preserve byte length")

    resource_blob = bytes(data[target.offset : target.offset + target.size])
    if resource_blob.count(source) != 1:
        raise ValueError("expected one English Press START string in PressStart_MGB")
    position = resource_blob.index(source)
    absolute = target.offset + position
    data[absolute : absolute + len(source)] = replacement

    archive = parse_forge(args.source_forge)
    entry = next(item for item in archive.entries if item.name == ENTRY_NAME)
    with args.source_forge.open("rb") as stream:
        stream.seek(entry.offset + 440)
        source_raw = stream.read(entry.size)
    source_chunks = wrapper_source_chunks(source_raw)
    if len(source_chunks) != 2:
        raise ValueError("expected two Map_Menu raw wrappers")

    args.output_forge.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="ac1_map_menu_", dir=args.output_forge.parent
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
            bytes(data),
            args.quickbms,
            args.compress_script,
            root,
            "data",
            source_chunks[1],
        )

    old_size, new_size = replace_forge_entry(
        args.source_forge,
        args.output_forge,
        ENTRY_NAME,
        raw_payload,
    )
    report = {
        "mode": "inplace-same-length-press-start",
        "resource": RESOURCE_NAME,
        "source": SOURCE_TEXT,
        "target_mode": args.target_mode,
        "target": target_text,
        "resource_offset": target.offset,
        "string_offset_in_resource": position,
        "decompressed_data_size": len(data),
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
