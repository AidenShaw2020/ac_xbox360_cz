#!/usr/bin/env python3
"""List BAO resource IDs referenced by another BAO resource in bundle order."""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

from ac1_resource_bundle import parse_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("data", type=Path)
    parser.add_argument("source", nargs="?")
    parser.add_argument(
        "--source-blob",
        type=Path,
        help="scan this standalone BAO blob instead of a named resource",
    )
    parser.add_argument("--endian", choices=("little", "big"), required=True)
    args = parser.parse_args()

    resources = parse_bundle(args.directory, args.data, args.endian)
    by_name = {resource.name: resource for resource in resources}
    data = args.data.read_bytes()
    if args.source_blob is not None:
        blob = args.source_blob.read_bytes()
        source_label = args.source_blob.name
    else:
        if args.source is None:
            raise ValueError("source resource or --source-blob is required")
        source = by_name.get(args.source)
        if source is None:
            raise ValueError(f"resource not found: {args.source}")
        blob = data[source.offset : source.offset + source.size]
        source_label = args.source
    byteorder = "<" if args.endian == "little" else ">"
    known: dict[bytes, set[str]] = {}
    for resource in resources:
        if not resource.name.startswith("BAO_0x"):
            continue
        identifiers = {
            ("name", int(resource.name[6:], 16)),
            ("directory", resource.directory_id),
        }
        if resource.entry_id is not None:
            identifiers.add(("entry", resource.entry_id))
        resource_blob = data[
            resource.offset : resource.offset + resource.size
        ]
        signature = b"\x01\x1B\x01\x00"
        cursor = 0
        while True:
            bao_offset = resource_blob.find(signature, cursor)
            if bao_offset < 0:
                break
            if bao_offset + 0x48 <= len(resource_blob):
                bao_type = struct.unpack_from(
                    f"{byteorder}I", resource_blob, bao_offset + 0x20
                )[0]
                if bao_type >> 28 != 2:
                    cursor = bao_offset + len(signature)
                    continue
                codec = struct.unpack_from(
                    f"{byteorder}I", resource_blob, bao_offset + 0x8C
                )[0]
                if codec not in (3, 4):
                    cursor = bao_offset + len(signature)
                    continue
                audio_id = struct.unpack_from(
                    f"{byteorder}I", resource_blob, bao_offset + 0x44
                )[0]
                identifiers.add(("audio", audio_id))
            cursor = bao_offset + len(signature)
        for kind, value in identifiers:
            key = struct.pack(f"{byteorder}I", value)
            known.setdefault(key, set()).add(
                f"{resource.name} ({kind}=0x{value:08X})"
            )

    matches: list[tuple[int, str]] = []
    for offset in range(max(0, len(blob) - 3)):
        names = known.get(blob[offset : offset + 4])
        if names is not None:
            for name in sorted(names):
                matches.append((offset, name))

    print(f"{source_label}: {len(matches)} references")
    for offset, name in matches:
        print(f"0x{offset:08X}\t{name}")


if __name__ == "__main__":
    main()
