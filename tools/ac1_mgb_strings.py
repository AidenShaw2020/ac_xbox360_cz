#!/usr/bin/env python3
"""Locate consecutive hash/length/UTF-16LE string tables in AC1 MGB resources."""

from __future__ import annotations

import argparse
import json
import struct
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class StringEntry:
    offset: int
    key: int
    value: str


@dataclass(frozen=True)
class StringChain:
    start: int
    end: int
    entries: list[StringEntry]


def _is_plausible(value: str) -> bool:
    if not value or len(value) > 8192:
        return False
    visible = 0
    for char in value:
        code = ord(char)
        if char in "\r\n\t":
            continue
        if code < 0x20 or 0xD800 <= code <= 0xDFFF:
            return False
        visible += 1
    return visible > 0


def read_entry(data: bytes, offset: int) -> tuple[StringEntry, int] | None:
    if offset + 8 > len(data):
        return None
    key, length = struct.unpack_from("<II", data, offset)
    if length == 0 or length > 8192:
        return None
    end = offset + 8 + length * 2
    if end > len(data):
        return None
    try:
        value = data[offset + 8 : end].decode("utf-16le")
    except UnicodeDecodeError:
        return None
    if not _is_plausible(value):
        return None
    return StringEntry(offset=offset, key=key, value=value), end


def find_chains(data: bytes, minimum_entries: int) -> list[StringChain]:
    chains: list[StringChain] = []
    covered_until = 0
    for start in range(len(data) - 8):
        if start < covered_until:
            continue
        result = read_entry(data, start)
        if result is None:
            continue
        entries: list[StringEntry] = []
        cursor = start
        while True:
            result = read_entry(data, cursor)
            if result is None:
                break
            entry, cursor = result
            entries.append(entry)
        if len(entries) >= minimum_entries:
            chains.append(StringChain(start=start, end=cursor, entries=entries))
            covered_until = cursor
    return chains


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--minimum", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dump-dir", type=Path)
    args = parser.parse_args()

    chains = find_chains(args.input.read_bytes(), args.minimum)
    if args.dump_dir:
        args.dump_dir.mkdir(parents=True, exist_ok=True)
        for index, chain in enumerate(chains):
            output = args.dump_dir / f"{args.input.stem}_{index:03d}.txt"
            with output.open("w", encoding="utf-8", newline="\n") as stream:
                for entry in chain.entries:
                    stream.write(f"[0x{entry.key:08X}]\n{entry.value}\n\n")

    if args.json:
        print(json.dumps([asdict(chain) for chain in chains], ensure_ascii=False))
    else:
        print(f"{args.input}: {len(chains)} chains")
        for index, chain in enumerate(chains):
            previews = " | ".join(
                entry.value.replace("\r", " ").replace("\n", " ")[:50]
                for entry in chain.entries[:3]
            )
            print(
                f"{index:3d}  0x{chain.start:08X}-0x{chain.end:08X}  "
                f"{len(chain.entries):5d}  {previews}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
