#!/usr/bin/env python3
"""Restore selected AC1 XMem chunks from a saved original FORGE prefix."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

from ac1_build_complete_text import replace_forge_entries_inplace
from ac1_build_gui_forge import RAW_SIGNATURE, wrapper_source_chunks
from ac1_build_masyaf_text import serialize_wrapper, wrapper_ranges
from ac1_forge import parse_forge


Chunk = tuple[int, int, bytes, bytes]


def forge_entry_payload(path: Path, name: str) -> tuple[int, bytes]:
    archive = parse_forge(path)
    matches = [entry for entry in archive.entries if entry.name == name]
    if len(matches) != 1:
        raise ValueError(f"expected one FORGE entry named {name!r}")
    entry = matches[0]
    with path.open("rb") as stream:
        stream.seek(entry.offset + 440)
        payload = stream.read(entry.size)
    if len(payload) != entry.size:
        raise ValueError(f"{name}: truncated payload")
    return entry.offset, payload


def partial_wrapper(
    data: bytes, offset: int, last_chunk: int
) -> tuple[list[Chunk], int]:
    if data[offset : offset + 8] != RAW_SIGNATURE:
        raise ValueError(f"invalid wrapper signature at 0x{offset:X}")
    count = struct.unpack_from(">H", data, offset + 15)[0]
    if last_chunk >= count:
        raise ValueError(f"wrapper has only {count} chunks")
    table = offset + 17
    data_cursor = table + count * 4
    chunks: list[Chunk] = []
    for index in range(last_chunk + 1):
        plain_size, stored_size = struct.unpack_from(
            ">HH", data, table + index * 4
        )
        end = data_cursor + 4 + stored_size
        if end > len(data):
            raise ValueError(
                f"saved prefix ends inside chunk {index} at 0x{end:X}"
            )
        chunks.append(
            (
                plain_size,
                stored_size,
                data[data_cursor : data_cursor + 4],
                data[data_cursor + 4 : end],
            )
        )
        data_cursor = end
    return chunks, data_cursor


def complete_wrapper_end(data: bytes, offset: int) -> int:
    count = struct.unpack_from(">H", data, offset + 15)[0]
    _chunks, end = partial_wrapper(data, offset, count - 1)
    return end


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-forge", required=True, type=Path)
    parser.add_argument("--original-head", required=True, type=Path)
    parser.add_argument("--entry-name", required=True)
    parser.add_argument("--data-chunk", required=True, action="append", type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    entry_offset, current_raw = forge_entry_payload(
        args.current_forge, args.entry_name
    )
    current_wrappers = wrapper_source_chunks(current_raw)
    current_ranges = wrapper_ranges(current_raw)
    if len(current_wrappers) != 2 or len(current_ranges) != 2:
        raise ValueError("expected directory and data wrappers")

    head = args.original_head.read_bytes()
    raw_start = entry_offset + 440
    if raw_start >= len(head):
        raise ValueError("saved prefix does not reach selected entry")
    original_prefix = head[raw_start:]
    directory_end = complete_wrapper_end(original_prefix, 0)
    last_chunk = max(args.data_chunk)
    original_chunks, _ = partial_wrapper(
        original_prefix, directory_end, last_chunk
    )

    rebuilt_chunks = list(current_wrappers[1])
    restored: dict[str, object] = {}
    for index in sorted(set(args.data_chunk)):
        before = rebuilt_chunks[index]
        after = original_chunks[index]
        rebuilt_chunks[index] = after
        restored[str(index)] = {
            "current_stored_size": before[1],
            "original_stored_size": after[1],
            "stored_bytes_changed": before[3] != after[3],
        }

    directory_start, directory_stop = current_ranges[0]
    data_start, _data_stop = current_ranges[1]
    rebuilt_raw = (
        current_raw[directory_start:directory_stop]
        + serialize_wrapper(
            current_raw[data_start : data_start + 17], rebuilt_chunks
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    forge_report = replace_forge_entries_inplace(
        args.current_forge,
        args.output,
        {args.entry_name: rebuilt_raw},
    )
    rebuilt_head = args.output.read_bytes()[: len(head)]
    if rebuilt_head != head:
        mismatch = next(
            index
            for index, (left, right) in enumerate(zip(rebuilt_head, head))
            if left != right
        )
        raise AssertionError(
            f"rebuilt prefix differs from saved original at 0x{mismatch:X}"
        )

    report = {
        "entry": args.entry_name,
        "restored_data_chunks": restored,
        "raw_before_size": len(current_raw),
        "raw_after_size": len(rebuilt_raw),
        "forge": forge_report,
        "saved_head_sha256": hashlib.sha256(head).hexdigest().upper(),
        "rebuilt_head_byte_exact": True,
        "file": {
            "size": args.output.stat().st_size,
            "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest().upper(),
        },
    }
    report_path = args.output.with_suffix(args.output.suffix + ".report.json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
