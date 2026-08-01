#!/usr/bin/env python3
"""Inspect early Ubisoft Scimitar .forge archives used by Assassin's Creed 1."""

from __future__ import annotations

import argparse
import json
import re
import struct
from dataclasses import asdict, dataclass
from pathlib import Path


SIGNATURE = b"scimitar"


@dataclass(frozen=True)
class ForgeEntry:
    index: int
    name: str
    offset: int
    size: int
    record_offset: int
    name_record_offset: int


@dataclass(frozen=True)
class ForgeArchive:
    path: str
    size: int
    directory_offset: int
    file_data_offset: int
    index_table_offset: int
    filename_directory_offset: int
    raw_data_table_offset: int
    entries: list[ForgeEntry]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def parse_forge(path: Path) -> ForgeArchive:
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(21)
        if len(header) != 21 or header[:8] != SIGNATURE:
            raise ValueError(f"{path}: unsupported FORGE signature")

        directory_offset = _u64(header, 13)
        if directory_offset >= size:
            raise ValueError(f"{path}: invalid directory offset 0x{directory_offset:X}")

        stream.seek(directory_offset)
        directory_header = stream.read(40)
        if len(directory_header) != 40:
            raise ValueError(f"{path}: truncated file data header I")

        file_count = _u32(directory_header, 0)
        file_data_offset = _u64(directory_header, 32)

        if not (0 < file_count < 1_000_000):
            raise ValueError(f"{path}: implausible file count {file_count}")
        if file_data_offset >= size:
            raise ValueError(
                f"{path}: invalid file data offset 0x{file_data_offset:X}"
            )

        table_count = _u32(directory_header, 28)
        if not (0 < table_count < 1024):
            raise ValueError(f"{path}: implausible entry table count {table_count}")

        raw_entries_by_index: dict[int, tuple[int, int, int]] = {}
        names_by_index: dict[int, tuple[str, int]] = {}
        table_offset = file_data_offset
        index_table_offset = 0
        filename_directory_offset = 0
        raw_data_table_offset = 0
        for table_number in range(table_count):
            if table_offset >= size:
                raise ValueError(
                    f"{path}: invalid entry table offset 0x{table_offset:X}"
                )
            stream.seek(table_offset)
            table_header = stream.read(48)
            if len(table_header) != 48:
                raise ValueError(f"{path}: truncated entry table header")
            entry_count = _u32(table_header, 0)
            entry_ptr = _u64(table_header, 8)
            next_table_ptr = _u64(table_header, 16)
            start_index = _u32(table_header, 24)
            metadata_ptr = _u64(table_header, 32)
            directory_ptr = _u64(table_header, 40)
            if table_number == 0:
                index_table_offset = entry_ptr
                filename_directory_offset = metadata_ptr
                raw_data_table_offset = directory_ptr

            if entry_count:
                if entry_ptr >= size or metadata_ptr >= size:
                    raise ValueError(
                        f"{path}: invalid entry/metadata table pointer"
                    )
                stream.seek(entry_ptr)
                for local_index in range(entry_count):
                    index = start_index + local_index
                    record_offset = stream.tell()
                    record = stream.read(16)
                    if len(record) != 16:
                        raise ValueError(f"{path}: truncated entry #{index}")
                    file_offset = _u64(record, 0)
                    file_size = _u32(record, 12)
                    if file_offset + 440 + file_size > size:
                        raise ValueError(
                            f"{path}: entry #{index} outside archive "
                            f"(0x{file_offset:X}+0x{file_size:X})"
                        )
                    raw_entries_by_index[index] = (
                        file_offset,
                        file_size,
                        record_offset,
                    )

                stream.seek(metadata_ptr)
                for local_index in range(entry_count):
                    index = start_index + local_index
                    name_record_offset = stream.tell()
                    name_record = stream.read(188)
                    if len(name_record) != 188:
                        raise ValueError(
                            f"{path}: truncated filename entry #{index}"
                        )
                    name_bytes = name_record[44:172].split(b"\0", 1)[0]
                    name = name_bytes.decode("utf-8", errors="replace")
                    names_by_index[index] = (name, name_record_offset)

            if table_number + 1 < table_count:
                if next_table_ptr == 0xFFFFFFFFFFFFFFFF:
                    raise ValueError(f"{path}: entry table chain ended early")
                table_offset = next_table_ptr

        if len(raw_entries_by_index) != file_count:
            raise ValueError(
                f"{path}: total file count {file_count} does not match "
                f"entry table count {len(raw_entries_by_index)}"
            )

    entries = [
        ForgeEntry(
            index=index,
            name=names_by_index[index][0],
            offset=raw_entries_by_index[index][0],
            size=raw_entries_by_index[index][1],
            record_offset=raw_entries_by_index[index][2],
            name_record_offset=names_by_index[index][1],
        )
        for index in range(file_count)
    ]
    return ForgeArchive(
        path=str(path),
        size=size,
        directory_offset=directory_offset,
        file_data_offset=file_data_offset,
        index_table_offset=index_table_offset,
        filename_directory_offset=filename_directory_offset,
        raw_data_table_offset=raw_data_table_offset,
        entries=entries,
    )


def extract_entries(
    archive_path: Path,
    archive: ForgeArchive,
    output_dir: Path,
    match: str | None = None,
    index: int | None = None,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    match_lower = match.lower() if match else None
    extracted = 0

    with archive_path.open("rb") as stream:
        for entry in archive.entries:
            if index is not None and entry.index != index:
                continue
            if match_lower and match_lower not in entry.name.lower():
                continue

            stream.seek(entry.offset)
            wrapper = stream.read(440)
            if len(wrapper) != 440 or wrapper[:8] != b"FILEDATA":
                raise ValueError(
                    f"{archive_path}: entry #{entry.index} does not use "
                    "the AC1 FILEDATA wrapper"
                )
            payload = stream.read(entry.size)
            if len(payload) != entry.size:
                raise ValueError(
                    f"{archive_path}: truncated payload for entry #{entry.index}"
                )

            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", entry.name).strip("_")
            if not safe_name:
                safe_name = f"entry_{entry.index:05d}"
            output_path = output_dir / f"{entry.index:05d}_{safe_name}.bin"
            output_path.write_bytes(payload)
            extracted += 1
    return extracted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--extract-dir", type=Path)
    parser.add_argument("--match")
    parser.add_argument("--index", type=int)
    args = parser.parse_args()

    archive = parse_forge(args.archive)
    if args.extract_dir:
        extracted = extract_entries(
            args.archive,
            archive,
            args.extract_dir,
            match=args.match,
            index=args.index,
        )
        print(f"Extracted {extracted} entries to {args.extract_dir}")
        return 0
    if args.json:
        print(json.dumps(asdict(archive), indent=2, ensure_ascii=False))
        return 0

    print(f"Archive: {archive.path}")
    print(f"Size: {archive.size}")
    print(f"Files: {len(archive.entries)}")
    print(f"Directory: 0x{archive.directory_offset:X}")
    print(f"File data: 0x{archive.file_data_offset:X}")
    print(f"Index: 0x{archive.index_table_offset:X}")
    print(f"Names: 0x{archive.filename_directory_offset:X}")
    print(f"Raw data: 0x{archive.raw_data_table_offset:X}")
    for entry in archive.entries:
        print(
            f"{entry.index:5d}  0x{entry.offset:010X}  {entry.size:10d}  "
            f"{entry.name}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
