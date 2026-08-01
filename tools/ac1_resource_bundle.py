#!/usr/bin/env python3
"""Inspect decompressed AC1 Scimitar resource-directory/data stream pairs."""

from __future__ import annotations

import argparse
import json
import re
import struct
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Resource:
    index: int
    directory_id: int
    offset: int
    size: int
    type_id: int
    declared_length: int
    name: str
    entry_id: int | None
    data_offset: int | None


def _format(endian: str, code: str) -> str:
    return ("<" if endian == "little" else ">") + code


def _u16(data: bytes, offset: int, endian: str) -> int:
    return struct.unpack_from(_format(endian, "H"), data, offset)[0]


def _u32(data: bytes, offset: int, endian: str) -> int:
    return struct.unpack_from(_format(endian, "I"), data, offset)[0]


def parse_bundle(
    directory_path: Path,
    data_path: Path,
    endian: str,
    *,
    allow_truncated: bool = False,
) -> list[Resource]:
    directory = directory_path.read_bytes()
    data = data_path.read_bytes()
    if len(directory) < 2:
        raise ValueError("resource directory is truncated")

    count = _u16(directory, 0, endian)
    table_end = 2 + count * 8
    if table_end > len(directory):
        raise ValueError(
            f"resource directory needs {table_end} bytes for {count} entries, "
            f"but only has {len(directory)}"
        )

    resources: list[Resource] = []
    data_offset = 0
    for index in range(count):
        record_offset = 2 + index * 8
        directory_id = _u32(directory, record_offset, endian)
        size = _u32(directory, record_offset + 4, endian)
        if data_offset + size > len(data):
            if allow_truncated:
                break
            raise ValueError(
                f"resource #{index} exceeds data stream "
                f"(0x{data_offset:X}+0x{size:X} > 0x{len(data):X})"
            )

        resource_data = data[data_offset : data_offset + size]
        type_id = _u32(resource_data, 0, endian) if size >= 4 else 0
        declared_length = _u32(resource_data, 4, endian) if size >= 8 else 0
        name = ""
        entry_id: int | None = None
        content_offset: int | None = None

        if size >= 12:
            name_length = _u32(resource_data, 8, endian)
            cursor = 12
            if name_length <= size - cursor:
                name = resource_data[cursor : cursor + name_length].decode(
                    "utf-8", errors="replace"
                )
                cursor += name_length
                if cursor < size:
                    extra_header = resource_data[cursor]
                    cursor += 1
                    if extra_header == 1 and cursor + 7 <= size:
                        cursor += 3
                        extra_count = _u32(resource_data, cursor, endian)
                        cursor += 4
                        if extra_count <= (size - cursor) // 12:
                            cursor += extra_count * 12
                        else:
                            cursor = size
                    elif extra_header != 0:
                        cursor = size

                    # AC1 predates the extra one-byte entry-id count used by
                    # later Scimitar resource headers.
                    if cursor + 8 <= size:
                        entry_id = _u32(resource_data, cursor, endian)
                        repeated_type = _u32(resource_data, cursor + 4, endian)
                        if repeated_type == type_id:
                            content_offset = data_offset + cursor + 8

        resources.append(
            Resource(
                index=index,
                directory_id=directory_id,
                offset=data_offset,
                size=size,
                type_id=type_id,
                declared_length=declared_length,
                name=name,
                entry_id=entry_id,
                data_offset=content_offset,
            )
        )
        data_offset += size

    if not allow_truncated and data_offset != len(data):
        raise ValueError(
            f"resource sizes cover 0x{data_offset:X} bytes, "
            f"but data stream has 0x{len(data):X}"
        )
    return resources


def safe_name(name: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return result or "unnamed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path, help="decompressed 0.dat")
    parser.add_argument("data", type=Path, help="decompressed 1.dat")
    parser.add_argument("--endian", choices=("little", "big"), required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--match")
    parser.add_argument("--type")
    parser.add_argument("--extract-dir", type=Path)
    parser.add_argument(
        "--bao-only",
        action="store_true",
        help="when extracting, strip the resource envelope before BAO data",
    )
    args = parser.parse_args()

    resources = parse_bundle(args.directory, args.data, args.endian)
    type_filter = int(args.type, 0) if args.type else None
    match = args.match.casefold() if args.match else None
    selected = [
        resource
        for resource in resources
        if (type_filter is None or resource.type_id == type_filter)
        and (match is None or match in resource.name.casefold())
    ]

    if args.extract_dir:
        args.extract_dir.mkdir(parents=True, exist_ok=True)
        data = args.data.read_bytes()
        for resource in selected:
            output = args.extract_dir / (
                f"{resource.index:05d}_{resource.type_id:08X}_"
                f"{safe_name(resource.name)}.bin"
            )
            payload = data[
                resource.offset : resource.offset + resource.size
            ]
            if args.bao_only:
                bao_offset = payload.find(b"\x01\x1B\x01\x00")
                if bao_offset < 0:
                    raise ValueError(
                        f"{resource.name}: BAO signature not found"
                    )
                payload = payload[bao_offset:]
            output.write_bytes(payload)

    if args.json:
        print(json.dumps([asdict(resource) for resource in selected], ensure_ascii=False))
    else:
        print(f"Resources: {len(resources)}; selected: {len(selected)}")
        for resource in selected:
            print(
                f"{resource.index:5d}  0x{resource.offset:08X}  "
                f"{resource.size:9d}  type=0x{resource.type_id:08X}  "
                f"id=0x{(resource.entry_id or 0):08X}  {resource.name}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
