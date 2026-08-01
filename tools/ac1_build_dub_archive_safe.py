#!/usr/bin/env python3
"""Port PC Czech AC1 speech into one Xbox area without moving its FORGE entry.

The Xbox resource bundle keeps the original resource layout.  Only class-2 BAO
audio metadata dwords are converted from the matching PC Czech objects and only
the affected XMem/LZX chunks are recompressed.  Czech language stream bodies
are placed under the original Xbox class-5 BAO headers in the language FORGE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import tempfile
from pathlib import Path

from ac1_build_all_mgb_archive import decode_xbox_prefix
from ac1_build_complete_text import (
    forge_entry_payload,
    replace_forge_entries,
    replace_forge_entries_inplace,
)
from ac1_build_dub_masyaf_probe import (
    BAO_RESOURCE_TYPE,
    BAO_SIGNATURE,
    BAO_STREAM_HEADER_SIZE,
    PC_LANGUAGE_PREFIX,
    XBOX_LANGUAGE_PREFIX,
    batch_replace_forge_entries,
    entries_by_audio_id,
    entry_payload,
    id_from_name,
    u32,
)
from ac1_build_gui_forge import wrapper_source_chunks, xmemlzx_wrapper
from ac1_build_masyaf_text import (
    CHUNK_SIZE,
    changed_chunks,
    partial_resource_map,
    serialize_wrapper,
    wrapper_ranges,
)
from ac1_forge import parse_forge
from ac1_resource_bundle import parse_bundle


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def slot_capacity(path: Path, entry_name: str) -> int:
    archive = parse_forge(path)
    ordered = sorted(archive.entries, key=lambda item: item.offset)
    for index, entry in enumerate(ordered):
        if entry.name != entry_name:
            continue
        following = (
            ordered[index + 1].offset
            if index + 1 < len(ordered)
            else path.stat().st_size
        )
        return following - entry.offset - 440
    raise ValueError(f"FORGE entry not found: {entry_name}")


def encode_selected_chunks(
    *,
    data: bytes,
    indices: list[int],
    source_chunks: list[tuple[int, int, bytes, bytes]],
    quickbms: Path,
    compress_script: Path,
    temporary: Path,
    tag: str,
) -> list[tuple[int, int, bytes, bytes]]:
    plain = b"".join(
        data[index * CHUNK_SIZE : (index + 1) * CHUNK_SIZE]
        for index in indices
    )
    selected_source = [source_chunks[index] for index in indices]
    wrapper = xmemlzx_wrapper(
        plain,
        quickbms,
        compress_script,
        temporary,
        tag,
        selected_source,
        preserve_source_stored_chunks=True,
    )
    encoded = wrapper_source_chunks(wrapper)
    if len(encoded) != 1 or len(encoded[0]) != len(indices):
        raise AssertionError(f"{tag}: encoded chunk count mismatch")
    return encoded[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pc-directory", required=True, type=Path)
    parser.add_argument("--pc-data", required=True, type=Path)
    parser.add_argument("--pc-language-forge", required=True, type=Path)
    parser.add_argument("--xbox-language-forge", required=True, type=Path)
    parser.add_argument("--xbox-area-forge", required=True, type=Path)
    parser.add_argument("--entry-name", required=True)
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--compress-script", required=True, type=Path)
    parser.add_argument("--output-language-forge", required=True, type=Path)
    parser.add_argument("--output-area-forge", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--allow-relocation",
        action="store_true",
        help="append the compressed entry when it cannot fit its original slot",
    )
    parser.add_argument(
        "--full-recompress-data",
        action="store_true",
        help="recompress the complete data stream to preserve LZX state",
    )
    parser.add_argument(
        "--store-changed-chunks",
        action="store_true",
        help="store only changed chunks uncompressed and preserve all others",
    )
    parser.add_argument(
        "--include-codec4",
        action="store_true",
        help="also carry the few PC Ogg streams (codec 4)",
    )
    args = parser.parse_args()

    source_raw = forge_entry_payload(args.xbox_area_forge, args.entry_name)
    source_wrappers = wrapper_source_chunks(source_raw)
    ranges = wrapper_ranges(source_raw)
    if len(source_wrappers) != 2 or len(ranges) != 2:
        raise ValueError(f"{args.entry_name}: expected two compression wrappers")

    with tempfile.TemporaryDirectory(prefix="ac1_dub_safe_") as temporary_name:
        temporary = Path(temporary_name)
        xbox_decoded, decoder_returncode, decoder_output = decode_xbox_prefix(
            source_raw, args.decoder, temporary, f"xbox_{args.entry_name}"
        )
        xbox_directory = (xbox_decoded / "0.dat").read_bytes()
        xbox_data_original = (xbox_decoded / "1.dat").read_bytes()

        pc_resources = {
            resource.name: resource
            for resource in parse_bundle(
                args.pc_directory, args.pc_data, "little"
            )
            if resource.type_id == BAO_RESOURCE_TYPE
        }
        xbox_resources = {
            resource.name: resource
            for resource in parse_bundle(
                xbox_decoded / "0.dat",
                xbox_decoded / "1.dat",
                "big",
            )
            if resource.type_id == BAO_RESOURCE_TYPE
        }

        pc_language_archive = parse_forge(args.pc_language_forge)
        xbox_language_archive = parse_forge(args.xbox_language_forge)
        pc_streams = entries_by_audio_id(
            pc_language_archive, PC_LANGUAGE_PREFIX
        )
        xbox_streams = entries_by_audio_id(
            xbox_language_archive, XBOX_LANGUAGE_PREFIX
        )

        pc_data = args.pc_data.read_bytes()
        xbox_data = bytearray(xbox_data_original)
        selected_ids: set[int] = set()
        patched_headers: list[dict[str, int | str]] = []
        skipped_codec4 = 0

        for name, xbox_resource in xbox_resources.items():
            if not name.startswith("BAO_0x2"):
                continue
            pc_resource = pc_resources.get(name)
            if pc_resource is None:
                continue
            pc_blob = pc_data[
                pc_resource.offset : pc_resource.offset + pc_resource.size
            ]
            xbox_blob = xbox_data_original[
                xbox_resource.offset : xbox_resource.offset + xbox_resource.size
            ]
            pc_bao = pc_blob.find(BAO_SIGNATURE)
            xbox_bao = xbox_blob.find(BAO_SIGNATURE)
            if pc_bao < 0 or xbox_bao < 0:
                continue
            if pc_bao + 0xA4 > len(pc_blob) or xbox_bao + 0xA4 > len(xbox_blob):
                continue
            if u32(pc_blob, pc_bao + 0x20, "little") >> 28 != 2:
                continue
            if u32(xbox_blob, xbox_bao + 0x20, "big") >> 28 != 2:
                continue
            if u32(pc_blob, pc_bao + 0x2C, "little") != 1:
                continue
            if u32(xbox_blob, xbox_bao + 0x2C, "big") != 1:
                continue

            audio_id = u32(pc_blob, pc_bao + 0x44, "little")
            if audio_id not in pc_streams or audio_id not in xbox_streams:
                continue
            if u32(xbox_blob, xbox_bao + 0x44, "big") != audio_id:
                raise ValueError(f"{name}: PC/Xbox audio ID differs")
            codec = u32(pc_blob, pc_bao + 0x8C, "little")
            if codec == 4 and not args.include_codec4:
                skipped_codec4 += 1
                continue
            if codec not in (3, 4):
                continue

            absolute = xbox_resource.offset + xbox_bao
            for field_offset in range(0x28, 0xA4, 4):
                struct.pack_into(
                    ">I",
                    xbox_data,
                    absolute + field_offset,
                    u32(pc_blob, pc_bao + field_offset, "little"),
                )
            selected_ids.add(audio_id)
            patched_headers.append(
                {
                    "resource": name,
                    "audio_id": f"{audio_id:08X}",
                    "codec": codec,
                    "stream_size": u32(
                        pc_blob, pc_bao + 0x30, "little"
                    ),
                    "samples": u32(pc_blob, pc_bao + 0x78, "little"),
                }
            )

        if not selected_ids:
            raise ValueError("no matching Czech speech headers were selected")

        patched_data = bytes(xbox_data)
        modified_indices = changed_chunks(xbox_data_original, patched_data)
        data_chunks = list(source_wrappers[1])
        if max(modified_indices) >= len(data_chunks):
            raise ValueError("modified metadata lies outside the data wrapper")
        directory_start, directory_end = ranges[0]
        data_start, _data_end = ranges[1]
        if args.store_changed_chunks:
            for index in modified_indices:
                plain = patched_data[
                    index * CHUNK_SIZE : (index + 1) * CHUNK_SIZE
                ]
                original = data_chunks[index]
                data_chunks[index] = (
                    len(plain),
                    len(plain),
                    original[2],
                    plain,
                )
            rebuilt_data_wrapper = serialize_wrapper(
                source_raw[data_start : data_start + 17], data_chunks
            )
            recompressed_indices = []
        elif args.full_recompress_data:
            contextual_wrapper = xmemlzx_wrapper(
                patched_data,
                args.quickbms,
                args.compress_script,
                temporary,
                "complete_audio_metadata_stream",
                data_chunks,
                preserve_source_stored_chunks=True,
            )
            contextual_chunks = wrapper_source_chunks(contextual_wrapper)
            if (
                len(contextual_chunks) != 1
                or len(contextual_chunks[0]) != len(data_chunks)
            ):
                raise AssertionError("contextual LZX chunk count mismatch")
            for index in modified_indices:
                data_chunks[index] = contextual_chunks[0][index]
            rebuilt_data_wrapper = serialize_wrapper(
                source_raw[data_start : data_start + 17], data_chunks
            )
            recompressed_indices = modified_indices
        else:
            encoded_changed = encode_selected_chunks(
                data=patched_data,
                indices=modified_indices,
                source_chunks=data_chunks,
                quickbms=args.quickbms,
                compress_script=args.compress_script,
                temporary=temporary,
                tag="changed_audio_metadata",
            )
            for index, replacement in zip(modified_indices, encoded_changed):
                data_chunks[index] = replacement
            rebuilt_data_wrapper = serialize_wrapper(
                source_raw[data_start : data_start + 17], data_chunks
            )
            recompressed_indices = modified_indices
        rebuilt_raw = (
            source_raw[directory_start:directory_end] + rebuilt_data_wrapper
        )

        capacity = slot_capacity(args.xbox_area_forge, args.entry_name)
        optimized_indices: list[int] = []
        if (
            len(rebuilt_raw) > capacity
            and not args.allow_relocation
            and not args.full_recompress_data
        ):
            unchanged_indices = [
                index
                for index in range(len(source_wrappers[1]))
                if index not in set(modified_indices)
            ]
            encoded_unchanged = encode_selected_chunks(
                data=patched_data,
                indices=unchanged_indices,
                source_chunks=list(source_wrappers[1]),
                quickbms=args.quickbms,
                compress_script=args.compress_script,
                temporary=temporary,
                tag="unchanged_audio_fit_candidates",
            )
            candidates = []
            for index, replacement in zip(
                unchanged_indices, encoded_unchanged
            ):
                original = source_wrappers[1][index]
                saving = original[1] - replacement[1]
                if saving > 0:
                    candidates.append((saving, index, replacement))
            candidates.sort(reverse=True)
            bytes_to_save = len(rebuilt_raw) - capacity
            saved = 0
            for _saving, index, replacement in candidates:
                data_chunks[index] = replacement
                optimized_indices.append(index)
                saved += _saving
                if saved >= bytes_to_save:
                    break
            rebuilt_raw = (
                source_raw[directory_start:directory_end]
                + serialize_wrapper(
                    source_raw[data_start : data_start + 17],
                    data_chunks,
                )
            )
        if len(rebuilt_raw) > capacity and not args.allow_relocation:
            raise ValueError(
                f"{args.entry_name}: rebuilt payload {len(rebuilt_raw)} "
                f"does not fit in {capacity} bytes"
            )

        verified, verify_returncode, verify_output = decode_xbox_prefix(
            rebuilt_raw, args.decoder, temporary, "verify_rebuilt_audio"
        )
        verified_directory = (verified / "0.dat").read_bytes()
        verified_data = (verified / "1.dat").read_bytes()
        if verified_directory != xbox_directory:
            raise AssertionError("rebuilt audio directory did not decode exactly")
        if verified_data != patched_data:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            (args.report.parent / "debug_rebuilt_raw.bin").write_bytes(
                rebuilt_raw
            )
            if args.full_recompress_data:
                (
                    args.report.parent / "debug_contextual_data_wrapper.bin"
                ).write_bytes(contextual_wrapper)
            mismatch = next(
                (
                    index
                    for index, (left, right) in enumerate(
                        zip(verified_data, patched_data)
                    )
                    if left != right
                ),
                min(len(verified_data), len(patched_data)),
            )
            raise AssertionError(
                "rebuilt audio data did not decode exactly: "
                f"decoded={len(verified_data)}, expected={len(patched_data)}, "
                f"first_mismatch=0x{mismatch:X}, decoder={verify_output!r}"
            )

        replacements: dict[int, bytes] = {}
        for audio_id in sorted(selected_ids):
            pc_entry = pc_streams[audio_id]
            xbox_entry = xbox_streams[audio_id]
            pc_payload = entry_payload(args.pc_language_forge, pc_entry)
            xbox_payload = entry_payload(
                args.xbox_language_forge, xbox_entry
            )
            if (
                len(pc_payload) < BAO_STREAM_HEADER_SIZE
                or len(xbox_payload) < BAO_STREAM_HEADER_SIZE
            ):
                raise ValueError(f"truncated language BAO 0x{audio_id:08X}")
            replacements[xbox_entry.index] = (
                xbox_payload[:BAO_STREAM_HEADER_SIZE]
                + pc_payload[BAO_STREAM_HEADER_SIZE:]
            )

        batch_replace_forge_entries(
            args.xbox_language_forge,
            args.output_language_forge,
            replacements,
        )
        if len(rebuilt_raw) <= capacity:
            replacement_mode = "in-place"
            area_replace_report = replace_forge_entries_inplace(
                args.xbox_area_forge,
                args.output_area_forge,
                {args.entry_name: rebuilt_raw},
            )
        else:
            replacement_mode = "relocated-compressed"
            area_replace_report = replace_forge_entries(
                args.xbox_area_forge,
                args.output_area_forge,
                {args.entry_name: rebuilt_raw},
            )

    codec_counts: dict[str, int] = {}
    for item in patched_headers:
        key = str(item["codec"])
        codec_counts[key] = codec_counts.get(key, 0) + 1
    report = {
        "entry": args.entry_name,
        "patched_header_count": len(patched_headers),
        "patched_stream_count": len(replacements),
        "codec_counts": codec_counts,
        "skipped_codec4": skipped_codec4,
        "changed_data_chunks": modified_indices,
        "optimized_unchanged_chunks": optimized_indices,
        "recompressed_data_chunks": recompressed_indices,
        "raw_before_size": len(source_raw),
        "raw_after_size": len(rebuilt_raw),
        "slot_capacity": capacity,
        "replacement_mode": replacement_mode,
        "area_replace": area_replace_report,
        "decoder_returncode": decoder_returncode,
        "decoder_output": decoder_output,
        "verify_decoder_returncode": verify_returncode,
        "verify_decoder_output": verify_output,
        "first_headers": patched_headers[:20],
        "language_forge_size": args.output_language_forge.stat().st_size,
        "area_forge_size": args.output_area_forge.stat().st_size,
        "language_forge_sha256": sha256_path(
            args.output_language_forge
        ),
        "area_forge_sha256": sha256_path(args.output_area_forge),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
