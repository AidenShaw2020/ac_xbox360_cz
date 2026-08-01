#!/usr/bin/env python3
"""List class-2 AC1 BAO headers and their embedded class-3 audio resources."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

from ac1_resource_bundle import parse_bundle


BAO_SIGNATURE = b"\x01\x1B\x01\x00"


def u32(data: bytes, offset: int, endian: str) -> int:
    prefix = "<" if endian == "little" else ">"
    return struct.unpack_from(f"{prefix}I", data, offset)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("data", type=Path)
    parser.add_argument(
        "--endian", choices=("little", "big"), default="little"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    resources = parse_bundle(args.directory, args.data, args.endian)
    data = args.data.read_bytes()
    by_name = {resource.name: resource for resource in resources}
    rows: list[dict[str, object]] = []

    for resource in resources:
        if not resource.name.startswith("BAO_0x2"):
            continue
        blob = data[resource.offset : resource.offset + resource.size]
        offsets = [
            offset
            for offset in range(len(blob) - len(BAO_SIGNATURE) + 1)
            if blob.startswith(BAO_SIGNATURE, offset)
            and offset + 0xA4 <= len(blob)
        ]
        for variant, bao in enumerate(offsets, 1):
            section_end = (
                offsets[variant]
                if variant < len(offsets)
                else len(blob)
            )
            audio_id = u32(blob, bao + 0x44, args.endian)
            memory = by_name.get(f"BAO_0x{audio_id:08x}")
            sample_rate = u32(blob, bao + 0x70, args.endian)
            samples = u32(blob, bao + 0x78, args.endian)
            rows.append(
                {
                    "index": resource.index,
                    "header": resource.name,
                    "variant": variant,
                    "audio_index": memory.index if memory else None,
                    "audio_id": f"{audio_id:08X}",
                    "codec": u32(blob, bao + 0x8C, args.endian),
                    "sample_rate": sample_rate,
                    "samples": samples,
                    "duration": samples / sample_rate if sample_rate else None,
                    "audio_bytes": memory.size if memory else None,
                    "section_bytes": section_end - bao,
                    "codec_extra": blob[
                        bao + 0xA4 : section_end
                    ].hex().upper(),
                }
            )

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    print(
        "index\theader\tvariant\taudio_index\taudio_id\tcodec"
        "\tduration\taudio_bytes"
    )
    for row in rows:
        duration = row["duration"]
        print(
            f"{row['index']}\t{row['header']}\t{row['variant']}"
            f"\t{row['audio_index']}"
            f"\t{row['audio_id']}\t{row['codec']}"
            f"\t{duration:.3f}\t{row['audio_bytes']}"
        )


if __name__ == "__main__":
    main()
