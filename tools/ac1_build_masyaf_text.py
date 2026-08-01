#!/usr/bin/env python3
"""Patch the active Masyaf pause menu without rebuilding unrelated LZX chunks.

The Masyaf cell contains a later XMem/LZX block that the available desktop
decoder cannot decode.  The console itself accepts that original block.  This
builder therefore recompresses only the chunks touched by MemoryPaused_MGB and
keeps every other compressed chunk byte-for-byte identical to the Xbox source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from ac1_build_complete_text import (
    forge_entry_payload,
    replace_forge_entries_inplace,
    sha256_path,
)
from ac1_build_gui_forge import (
    RAW_SIGNATURE,
    wrapper_source_chunks,
    xmemlzx_wrapper,
)
from ac1_complete_text import patch_memory_paused
from ac1_resource_bundle import Resource, parse_bundle


ENTRY_NAME = "Cell05460_DataBlock"
RESOURCE_NAME = "MemoryPaused_MGB"
CHUNK_SIZE = 0x8000


def partial_resource_map(
    directory: bytes, data: bytes
) -> dict[str, Resource]:
    """Parse every resource whose bytes are present in a decoded prefix."""
    count = struct.unpack_from(">H", directory, 0)[0]
    if 2 + count * 8 > len(directory):
        raise ValueError("truncated Xbox resource directory")
    result: dict[str, Resource] = {}
    data_offset = 0
    for index in range(count):
        record = 2 + index * 8
        directory_id, size = struct.unpack_from(">II", directory, record)
        if data_offset + size > len(data):
            break
        blob = data[data_offset : data_offset + size]
        type_id = struct.unpack_from(">I", blob, 0)[0] if size >= 4 else 0
        declared_length = (
            struct.unpack_from(">I", blob, 4)[0] if size >= 8 else 0
        )
        name = ""
        entry_id = None
        content_offset = None
        if size >= 12:
            name_length = struct.unpack_from(">I", blob, 8)[0]
            cursor = 12
            if name_length <= size - cursor:
                name = blob[cursor : cursor + name_length].decode(
                    "utf-8", errors="replace"
                )
                cursor += name_length
                if cursor < size:
                    extra_header = blob[cursor]
                    cursor += 1
                    if extra_header == 1 and cursor + 7 <= size:
                        cursor += 3
                        extra_count = struct.unpack_from(">I", blob, cursor)[0]
                        cursor += 4 + extra_count * 12
                    elif extra_header != 0:
                        cursor = size
                    if cursor + 8 <= size:
                        entry_id = struct.unpack_from(">I", blob, cursor)[0]
                        repeated_type = struct.unpack_from(
                            ">I", blob, cursor + 4
                        )[0]
                        if repeated_type == type_id:
                            content_offset = data_offset + cursor + 8
        if name:
            result.setdefault(
                name,
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
                ),
            )
        data_offset += size
    return result


def wrapper_ranges(raw: bytes) -> list[tuple[int, int]]:
    """Return byte ranges for the concatenated raw compression wrappers."""
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(raw):
        start = cursor
        if raw[cursor : cursor + 8] != RAW_SIGNATURE:
            raise ValueError(f"invalid raw wrapper at 0x{cursor:X}")
        count = struct.unpack_from(">H", raw, cursor + 15)[0]
        table = cursor + 17
        payload = table + count * 4
        compressed_sizes = [
            struct.unpack_from(">H", raw, table + index * 4 + 2)[0]
            for index in range(count)
        ]
        cursor = payload + sum(4 + size for size in compressed_sizes)
        if cursor > len(raw):
            raise ValueError("raw wrapper exceeds payload")
        ranges.append((start, cursor))
    return ranges


def serialize_wrapper(
    source_header: bytes, chunks: list[tuple[int, int, bytes, bytes]]
) -> bytes:
    if len(source_header) != 17 or source_header[:8] != RAW_SIGNATURE:
        raise ValueError("invalid source wrapper header")
    header = bytearray(source_header)
    struct.pack_into(">H", header, 15, len(chunks))
    output = header
    for plain_size, compressed_size, _field, stored in chunks:
        if len(stored) != compressed_size:
            raise ValueError("compressed chunk size mismatch")
        output += struct.pack(">HH", plain_size, compressed_size)
    for _plain_size, _compressed_size, field, stored in chunks:
        if len(field) != 4:
            raise ValueError("invalid opaque chunk field")
        output += field
        output += stored
    return bytes(output)


def changed_chunks(before: bytes, after: bytes) -> list[int]:
    if len(before) != len(after):
        raise ValueError("decoded stream size changed")
    result = []
    for index, offset in enumerate(range(0, len(before), CHUNK_SIZE)):
        if before[offset : offset + CHUNK_SIZE] != after[
            offset : offset + CHUNK_SIZE
        ]:
            result.append(index)
    return result


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-forge", required=True, type=Path)
    parser.add_argument("--xbox-bundle", required=True, type=Path)
    parser.add_argument("--pc-bundle", required=True, type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--compress-script", required=True, type=Path)
    parser.add_argument("--decoder", required=True, type=Path)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    xbox_directory = (args.xbox_bundle / "0.dat").read_bytes()
    xbox_data = (args.xbox_bundle / "1.dat").read_bytes()
    xbox_resources = partial_resource_map(xbox_directory, xbox_data)
    if RESOURCE_NAME not in xbox_resources:
        raise ValueError(f"Xbox resource not found in decoded prefix: {RESOURCE_NAME}")
    xbox_resource = xbox_resources[RESOURCE_NAME]
    xbox_blob = xbox_data[
        xbox_resource.offset : xbox_resource.offset + xbox_resource.size
    ]

    pc_resources = {
        resource.name: resource
        for resource in parse_bundle(
            args.pc_bundle / "0.dat",
            args.pc_bundle / "1.dat",
            "little",
        )
        if resource.name
    }
    if RESOURCE_NAME not in pc_resources:
        raise ValueError(f"PC resource not found: {RESOURCE_NAME}")
    pc_resource = pc_resources[RESOURCE_NAME]
    pc_data = (args.pc_bundle / "1.dat").read_bytes()
    pc_blob = pc_data[pc_resource.offset : pc_resource.offset + pc_resource.size]

    patched_blob, text_report = patch_memory_paused(xbox_blob, pc_blob)
    if len(patched_blob) != len(xbox_blob):
        raise AssertionError("MemoryPaused_MGB size changed")
    rebuilt_prefix = bytearray(xbox_data)
    rebuilt_prefix[
        xbox_resource.offset : xbox_resource.offset + xbox_resource.size
    ] = patched_blob
    rebuilt_prefix_bytes = bytes(rebuilt_prefix)
    modified_indices = changed_chunks(xbox_data, rebuilt_prefix_bytes)
    if not modified_indices:
        raise AssertionError("patch did not change any data chunks")

    source_raw = forge_entry_payload(args.source_forge, ENTRY_NAME)
    source_wrappers = wrapper_source_chunks(source_raw)
    ranges = wrapper_ranges(source_raw)
    if len(source_wrappers) != 2 or len(ranges) != 2:
        raise ValueError("expected directory and data wrappers")
    data_chunks = list(source_wrappers[1])
    if max(modified_indices) >= len(data_chunks):
        raise ValueError("modified chunk lies outside raw data wrapper")

    plain_changed = b"".join(
        rebuilt_prefix_bytes[
            index * CHUNK_SIZE : (index + 1) * CHUNK_SIZE
        ]
        for index in modified_indices
    )
    source_changed = [data_chunks[index] for index in modified_indices]
    with tempfile.TemporaryDirectory(
        prefix="ac1_masyaf_text_", dir=args.output
    ) as temporary:
        encoded_wrapper = xmemlzx_wrapper(
            plain_changed,
            args.quickbms,
            args.compress_script,
            Path(temporary),
            "masyaf_memory_paused",
            source_changed,
        )
    encoded_chunks = wrapper_source_chunks(encoded_wrapper)
    if len(encoded_chunks) != 1:
        raise AssertionError("unexpected temporary wrapper count")
    if len(encoded_chunks[0]) != len(modified_indices):
        raise AssertionError("unexpected temporary chunk count")
    for index, replacement in zip(modified_indices, encoded_chunks[0]):
        data_chunks[index] = replacement

    directory_start, directory_end = ranges[0]
    data_start, _data_end = ranges[1]
    rebuilt_raw = (
        source_raw[directory_start:directory_end]
        + serialize_wrapper(source_raw[data_start : data_start + 17], data_chunks)
    )
    rebuilt_wrappers = wrapper_source_chunks(rebuilt_raw)
    for wrapper_index, (old_chunks, new_chunks) in enumerate(
        zip(source_wrappers, rebuilt_wrappers)
    ):
        for chunk_index, (old, new) in enumerate(zip(old_chunks, new_chunks)):
            if wrapper_index == 1 and chunk_index in modified_indices:
                continue
            if old != new:
                raise AssertionError(
                    f"untouched raw chunk changed: wrapper {wrapper_index}, "
                    f"chunk {chunk_index}"
                )

    output_forge = args.output / "Data360_Masyaf.forge"
    forge_report = replace_forge_entries_inplace(
        args.source_forge, output_forge, {ENTRY_NAME: rebuilt_raw}
    )

    # The available decoder predictably stops later in this cell.  It still
    # decodes the complete patched menu prefix, which is what we verify here.
    verify_root = args.output / "verify_decode"
    if verify_root.exists():
        shutil.rmtree(verify_root)
    verify_root.mkdir()
    verify_raw = verify_root / "cell5460.bin"
    verify_raw.write_bytes(forge_entry_payload(output_forge, ENTRY_NAME))
    decoded_destination = verify_root / "decoded"
    completed = subprocess.run(
        [str(args.decoder), str(verify_raw), str(decoded_destination)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    decoded = decoded_destination / verify_raw.stem
    decoded_directory = (decoded / "0.dat").read_bytes()
    decoded_data = (decoded / "1.dat").read_bytes()
    if decoded_directory != xbox_directory:
        raise AssertionError("rebuilt resource directory differs")
    required = xbox_resource.offset + xbox_resource.size
    if len(decoded_data) < required:
        raise AssertionError("decoder stopped before patched resource")
    if decoded_data[: len(rebuilt_prefix_bytes)] != rebuilt_prefix_bytes:
        raise AssertionError("decoded patched prefix differs")

    report = {
        "mode": "xbox-native-masyaf-partial-chunk-czech",
        "resource": {
            "name": RESOURCE_NAME,
            "index": xbox_resource.index,
            "offset": xbox_resource.offset,
            "size": xbox_resource.size,
            "before_sha256": sha256_bytes(xbox_blob),
            "after_sha256": sha256_bytes(patched_blob),
            "text": text_report,
        },
        "changed_data_chunks": modified_indices,
        "preserved_data_chunks": len(data_chunks) - len(modified_indices),
        "raw_payload": {
            "before_size": len(source_raw),
            "after_size": len(rebuilt_raw),
        },
        "forge_replacement": forge_report,
        "file": {
            "size": output_forge.stat().st_size,
            "sha256": sha256_path(output_forge),
        },
        "verification": {
            "decoder_returncode": completed.returncode,
            "decoded_prefix_size": len(decoded_data),
            "patched_prefix_byte_exact": True,
            "decoder_output": completed.stdout.strip(),
        },
    }
    (args.output / "build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
