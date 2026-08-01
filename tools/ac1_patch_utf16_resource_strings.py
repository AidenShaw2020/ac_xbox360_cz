#!/usr/bin/env python3
"""Patch fixed-width UTF-16LE fallback strings in one Xbox AC1 resource."""

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


def parse_replacement(value: str) -> tuple[str, str]:
    old, separator, new = value.partition("=")
    if not separator or not old:
        raise argparse.ArgumentTypeError(
            "replacement must use OLD=NEW syntax"
        )
    if len(new) > len(old):
        raise argparse.ArgumentTypeError(
            f"replacement is longer than its slot: {old!r} -> {new!r}"
        )
    return old, new


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-forge", required=True, type=Path)
    parser.add_argument("--entry-name", required=True)
    parser.add_argument("--resource-name", required=True)
    parser.add_argument(
        "--replace",
        action="append",
        required=True,
        type=parse_replacement,
        dest="replacements",
    )
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--compress-script", required=True, type=Path)
    parser.add_argument("--output-forge", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    source_raw = forge_entry_payload(args.source_forge, args.entry_name)
    with tempfile.TemporaryDirectory(prefix="ac1_utf16_fallback_") as temp_name:
        temporary = Path(temp_name)
        decoded, decoder_returncode, decoder_output = decode_xbox_prefix(
            source_raw, args.decoder, temporary, "source"
        )
        directory_path = decoded / "0.dat"
        data_path = decoded / "1.dat"
        original_data = data_path.read_bytes()
        matches = [
            resource
            for resource in parse_bundle(directory_path, data_path, "big")
            if resource.name == args.resource_name
        ]
        if len(matches) != 1:
            raise ValueError(
                f"expected one resource named {args.resource_name!r}, "
                f"found {len(matches)}"
            )
        resource = matches[0]
        resource_start = resource.offset
        resource_end = resource.offset + resource.size
        patched_resource = bytearray(
            original_data[resource_start:resource_end]
        )

        replacement_report: list[dict[str, object]] = []
        for old, new in args.replacements:
            old_bytes = old.encode("utf-16le")
            padded_new = new.ljust(len(old))
            new_bytes = padded_new.encode("utf-16le")
            if len(old_bytes) != len(new_bytes):
                raise AssertionError("UTF-16 fixed-width replacement differs")
            offsets: list[int] = []
            cursor = 0
            while True:
                found = patched_resource.find(old_bytes, cursor)
                if found < 0:
                    break
                patched_resource[found : found + len(old_bytes)] = new_bytes
                offsets.append(resource_start + found)
                cursor = found + len(old_bytes)
            replacement_report.append(
                {
                    "old": old,
                    "new": new,
                    "padded_new": padded_new,
                    "count": len(offsets),
                    "data_offsets": offsets,
                }
            )

        missing = [item["old"] for item in replacement_report if item["count"] == 0]
        if missing:
            raise ValueError(
                "replacement sources not found: " + ", ".join(missing)
            )
        patched_data = bytearray(original_data)
        patched_data[resource_start:resource_end] = patched_resource
        patched_data_bytes = bytes(patched_data)
        modified_chunks = changed_chunks(original_data, patched_data_bytes)
        if not modified_chunks:
            raise AssertionError("no decompressed chunks changed")

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
            "encoded_utf16_fallbacks",
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
        if (verified / "1.dat").read_bytes() != patched_data_bytes:
            raise AssertionError("verified decoded data differs")

        replace_report = replace_forge_entries_inplace(
            args.source_forge,
            args.output_forge,
            {args.entry_name: rebuilt_raw},
        )

    report = {
        "source": str(args.source_forge),
        "output": str(args.output_forge),
        "entry": args.entry_name,
        "resource": args.resource_name,
        "resource_index": resource.index,
        "resource_id": f"0x{resource.directory_id:08X}",
        "replacements": replacement_report,
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
