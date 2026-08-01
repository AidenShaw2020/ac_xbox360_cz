#!/usr/bin/env python3
"""Replace one fixed-width resource name inside an Xbox AC1 FORGE entry."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from ac1_build_all_mgb_archive import decode_xbox_prefix
from ac1_build_complete_text import (
    forge_entry_payload,
    replace_forge_entries_inplace,
)
from ac1_build_gui_forge import wrapper_source_chunks, xmemlzx_wrapper
from ac1_build_masyaf_text import (
    CHUNK_SIZE,
    changed_chunks,
    serialize_wrapper,
    wrapper_ranges,
)
from ac1_resource_bundle import parse_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-forge", required=True, type=Path)
    parser.add_argument("--entry-name", required=True)
    parser.add_argument("--old-name", required=True)
    parser.add_argument("--new-name", required=True)
    parser.add_argument(
        "--data-offset",
        type=lambda value: int(value, 0),
        help=(
            "patch the exact byte offset in the decompressed data stream "
            "instead of locating a resource header by name"
        ),
    )
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--compress-script", required=True, type=Path)
    parser.add_argument("--output-forge", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    old_bytes = args.old_name.encode("utf-8")
    new_bytes = args.new_name.encode("utf-8")
    if len(old_bytes) != len(new_bytes):
        raise ValueError(
            "resource names must have equal UTF-8 byte lengths: "
            f"{len(old_bytes)} != {len(new_bytes)}"
        )

    source_raw = forge_entry_payload(args.source_forge, args.entry_name)
    with tempfile.TemporaryDirectory(prefix="ac1_resource_name_") as temporary_name:
        temporary = Path(temporary_name)
        decoded, decoder_returncode, decoder_output = decode_xbox_prefix(
            source_raw, args.decoder, temporary, "source"
        )
        directory_path = decoded / "0.dat"
        data_path = decoded / "1.dat"
        original_data = data_path.read_bytes()
        resources = parse_bundle(directory_path, data_path, "big")
        if args.data_offset is None:
            matches = [
                resource for resource in resources
                if resource.name == args.old_name
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"expected one resource named {args.old_name!r}, "
                    f"found {len(matches)}"
                )
            resource = matches[0]
            name_offset = resource.offset + 12
        else:
            name_offset = args.data_offset
            containing = [
                resource for resource in resources
                if resource.offset <= name_offset
                and name_offset + len(old_bytes) <= resource.offset + resource.size
            ]
            if len(containing) != 1:
                raise ValueError(
                    f"offset 0x{name_offset:X} belongs to {len(containing)} resources"
                )
            resource = containing[0]
        if original_data[name_offset : name_offset + len(old_bytes)] != old_bytes:
            raise AssertionError("resource-name bytes differ from parsed name")

        patched_data = bytearray(original_data)
        patched_data[name_offset : name_offset + len(new_bytes)] = new_bytes
        patched_data_bytes = bytes(patched_data)
        modified_chunks = changed_chunks(original_data, patched_data_bytes)
        if len(modified_chunks) != 1:
            raise AssertionError(
                f"fixed-width resource rename touched {len(modified_chunks)} chunks"
            )

        wrappers = wrapper_source_chunks(source_raw)
        ranges = wrapper_ranges(source_raw)
        if len(wrappers) != 2 or len(ranges) != 2:
            raise ValueError("expected directory and data wrappers")
        data_chunks = list(wrappers[1])
        plain_changed = b"".join(
            patched_data_bytes[
                index * CHUNK_SIZE : (index + 1) * CHUNK_SIZE
            ]
            for index in modified_chunks
        )
        encoded_wrapper = xmemlzx_wrapper(
            plain_changed,
            args.quickbms,
            args.compress_script,
            temporary,
            "encoded_resource_name",
            [data_chunks[index] for index in modified_chunks],
            preserve_source_stored_chunks=False,
        )
        encoded_chunks = wrapper_source_chunks(encoded_wrapper)
        if len(encoded_chunks) != 1:
            raise AssertionError("temporary compression wrapper mismatch")
        for index, replacement in zip(modified_chunks, encoded_chunks[0]):
            data_chunks[index] = replacement

        directory_start, directory_end = ranges[0]
        data_start, _data_end = ranges[1]
        rebuilt_raw = (
            source_raw[directory_start:directory_end]
            + serialize_wrapper(
                source_raw[data_start : data_start + 17], data_chunks
            )
        )
        verified, verify_returncode, verify_output = decode_xbox_prefix(
            rebuilt_raw, args.decoder, temporary, "verify"
        )
        verified_data = (verified / "1.dat").read_bytes()
        if verified_data != patched_data_bytes:
            raise AssertionError("verified decoded data differs")
        if verified_data[name_offset : name_offset + len(new_bytes)] != new_bytes:
            raise AssertionError("replacement bytes were not found after verification")
        if args.data_offset is None:
            checked_resources = parse_bundle(
                verified / "0.dat", verified / "1.dat", "big"
            )
            renamed = [
                item for item in checked_resources
                if item.name == args.new_name
            ]
            if len(renamed) != 1:
                raise AssertionError(
                    "renamed resource was not found after verification"
                )

        replace_report = replace_forge_entries_inplace(
            args.source_forge,
            args.output_forge,
            {args.entry_name: rebuilt_raw},
        )

    report = {
        "source": str(args.source_forge),
        "output": str(args.output_forge),
        "entry": args.entry_name,
        "resource_index": resource.index,
        "resource_id": f"0x{resource.directory_id:08X}",
        "data_offset": name_offset,
        "old_name": args.old_name,
        "new_name": args.new_name,
        "utf8_bytes": len(new_bytes),
        "modified_chunks": modified_chunks,
        "raw_size_before": len(source_raw),
        "raw_size_after": len(rebuilt_raw),
        "replace": replace_report,
        "decoder_returncode": decoder_returncode,
        "decoder_output": decoder_output,
        "verify_returncode": verify_returncode,
        "verify_output": verify_output,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
