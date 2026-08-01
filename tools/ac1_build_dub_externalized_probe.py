#!/usr/bin/env python3
"""Externalize embedded PC Czech AC1 voices into Xbox streamed BAO entries."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from ac1_build_all_mgb_archive import decode_xbox_prefix
from ac1_build_complete_text import (
    forge_entry_payload,
    replace_forge_entries_inplace,
)
from ac1_build_dub_embedded_safe import patch_header, resource_blob, sha256_path
from ac1_build_dub_masyaf_probe import (
    BAO_RESOURCE_TYPE,
    BAO_SIGNATURE,
    BAO_STREAM_HEADER_SIZE,
    XBOX_LANGUAGE_PREFIX,
    batch_replace_forge_entries,
    entries_by_audio_id,
    entry_payload,
    u32,
)
from ac1_build_gui_forge import wrapper_source_chunks
from ac1_build_masyaf_text import (
    CHUNK_SIZE,
    changed_chunks,
    serialize_wrapper,
    wrapper_ranges,
)
from ac1_forge import parse_forge
from ac1_resource_bundle import parse_bundle
from ac1_ubi_ima import encode_ubi_ima_mono


RAW_TABLE_RECORD_SIZE = 164
NAME_RECORD_SIZE = 188
INDEX_RECORD_SIZE = 16
FILEDATA_WRAPPER_SIZE = 440


def externalize_header(
    xbox_blob: bytes,
    xbox_bao: int,
    pc_blob: bytes,
    pc_bao: int,
    stream_id: int,
) -> bytes:
    patched = bytearray(
        patch_header(xbox_blob, xbox_bao, pc_blob, pc_bao)
    )
    stream_size = u32(pc_blob, pc_bao + 0x30, "little")
    overrides = {
        0x38: stream_size,
        0x3C: 0,
        0x40: 1,
        0x44: stream_id,
        0x50: 1,
        0x88: 0,
    }
    for offset, value in overrides.items():
        struct.pack_into(">I", patched, xbox_bao + offset, value)
    return bytes(patched)


def allocate_stream_id(memory_id: int, used_ids: set[int]) -> int:
    """Map a class-3 memory BAO to an unused class-5 stream BAO ID."""
    candidate = 0x50000000 | (memory_id & 0x0FFFFFFF)
    start = candidate
    while candidate in used_ids:
        candidate = 0x50000000 | ((candidate + 1) & 0x0FFFFFFF)
        if candidate == start:
            raise ValueError("no free class-5 BAO ID")
    used_ids.add(candidate)
    return candidate


def unique_pair(name: str, audio_id: int) -> bytes:
    return hashlib.sha256(
        name.encode("ascii") + struct.pack("<I", audio_id)
    ).digest()[:8]


def add_language_entries(
    source: Path,
    output: Path,
    additions: list[tuple[str, int, bytes]],
) -> dict[str, object]:
    archive = parse_forge(source)
    if not additions:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, output)
        return {"added": 0}
    data = source.read_bytes()
    existing_names = {entry.name for entry in archive.entries}
    existing_ids = {
        struct.unpack_from(
            "<I",
            data,
            entry.record_offset + 8,
        )[0]
        for entry in archive.entries
    }
    for name, audio_id, _payload in additions:
        if name in existing_names or audio_id in existing_ids:
            raise ValueError(f"duplicate new language BAO: {name}")

    template = next(
        entry
        for entry in archive.entries
        if entry.name.startswith(XBOX_LANGUAGE_PREFIX)
    )
    template_wrapper = bytearray(
        data[
            template.offset : template.offset + FILEDATA_WRAPPER_SIZE
        ]
    )
    template_name = bytearray(
        data[
            template.name_record_offset :
            template.name_record_offset + NAME_RECORD_SIZE
        ]
    )
    global_offset = struct.unpack_from("<I", data, 13)[0]
    table_count = struct.unpack_from("<I", data, global_offset + 28)[0]
    first_table_offset = struct.unpack_from(
        "<Q", data, global_offset + 32
    )[0]
    first_count = struct.unpack_from("<I", data, first_table_offset)[0]
    first_metadata_offset = struct.unpack_from(
        "<Q", data, first_table_offset + 32
    )[0]
    first_directory_offset = struct.unpack_from(
        "<Q", data, first_table_offset + 40
    )[0]
    second_table_offset = struct.unpack_from(
        "<Q", data, first_table_offset + 16
    )[0]
    if table_count < 2 or second_table_offset == 0xFFFFFFFFFFFFFFFF:
        raise ValueError("language FORGE has no reserved overflow table")
    second_count = struct.unpack_from("<I", data, second_table_offset)[0]
    second_entry_offset = struct.unpack_from(
        "<Q", data, second_table_offset + 8
    )[0]
    second_start_index = struct.unpack_from(
        "<I", data, second_table_offset + 24
    )[0]
    second_end_index = struct.unpack_from(
        "<I", data, second_table_offset + 28
    )[0]
    second_metadata_offset = struct.unpack_from(
        "<Q", data, second_table_offset + 32
    )[0]
    if first_count + second_count != len(archive.entries):
        raise ValueError("language entry-table counts differ")
    if second_start_index != first_count:
        raise ValueError("reserved language overflow start differs")
    second_capacity = second_end_index - second_start_index + 1
    if second_count + len(additions) > second_capacity:
        raise ValueError("reserved language overflow table is too small")

    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    new_index_records = bytearray()
    new_name_records = bytearray()
    added_report: list[dict[str, object]] = []
    with output.open("r+b") as stream:
        stream.seek(0, 2)
        for ordinal, (name, audio_id, payload) in enumerate(additions):
            alignment = (-stream.tell()) & 0x7FF
            if alignment:
                stream.write(b"\0" * alignment)
            offset = stream.tell()
            wrapper = bytearray(template_wrapper)
            wrapper[8:136] = b"\0" * 128
            name_bytes = name.encode("ascii")
            if len(name_bytes) >= 128:
                raise ValueError(f"FORGE name too long: {name}")
            wrapper[8 : 8 + len(name_bytes)] = name_bytes
            pair = unique_pair(name, audio_id)
            struct.pack_into("<I", wrapper, 391, audio_id)
            struct.pack_into("<I", wrapper, 395, len(payload))
            wrapper[399:407] = pair
            stream.write(wrapper)
            stream.write(payload)

            new_index_records += struct.pack(
                "<QII", offset, audio_id, len(payload)
            )
            index = len(archive.entries) + ordinal
            name_record = bytearray(template_name)
            struct.pack_into("<I", name_record, 0, len(payload))
            name_record[4:12] = pair
            struct.pack_into("<I", name_record, 28, index + 1)
            struct.pack_into("<I", name_record, 32, index - 1)
            if ordinal + 1 == len(additions):
                struct.pack_into("<i", name_record, 28, -1)
            name_record[44:172] = b"\0" * 128
            name_record[44 : 44 + len(name_bytes)] = name_bytes
            new_name_records += name_record
            added_report.append(
                {
                    "name": name,
                    "audio_id": f"{audio_id:08X}",
                    "offset": offset,
                    "size": len(payload),
                }
            )

        new_count = len(archive.entries) + len(additions)
        stream.seek(
            second_entry_offset + second_count * INDEX_RECORD_SIZE
        )
        stream.write(new_index_records)
        stream.seek(
            second_metadata_offset + second_count * NAME_RECORD_SIZE
        )
        stream.write(new_name_records)
        stream.seek(global_offset)
        stream.write(struct.pack("<I", new_count))
        stream.seek(second_table_offset)
        stream.write(
            struct.pack("<I", second_count + len(additions))
        )
        if second_count:
            previous_metadata_offset = (
                second_metadata_offset
                + (second_count - 1) * NAME_RECORD_SIZE
            )
        else:
            previous_metadata_offset = (
                first_metadata_offset
                + (first_count - 1) * NAME_RECORD_SIZE
            )
        stream.seek(previous_metadata_offset + 28)
        stream.write(struct.pack("<I", len(archive.entries)))
        stream.seek(first_directory_offset)
        stream.write(struct.pack("<I", new_count - 1))

    checked = parse_forge(output)
    if len(checked.entries) != len(archive.entries) + len(additions):
        raise AssertionError("expanded FORGE entry count differs")
    checked_names = {entry.name for entry in checked.entries}
    for name, _audio_id, payload in additions:
        if name not in checked_names:
            raise AssertionError(f"expanded FORGE lost {name}")
        entry = next(item for item in checked.entries if item.name == name)
        if entry.size != len(payload):
            raise AssertionError(f"expanded FORGE size differs for {name}")
    return {
        "added": len(additions),
        "old_count": len(archive.entries),
        "new_count": len(checked.entries),
        "entries": added_report,
        "global_offset": global_offset,
        "first_table_offset": first_table_offset,
        "second_table_offset": second_table_offset,
        "second_entry_offset": second_entry_offset,
        "second_metadata_offset": second_metadata_offset,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pc-directory", required=True, type=Path)
    parser.add_argument("--pc-data", required=True, type=Path)
    parser.add_argument("--xbox-language-forge", required=True, type=Path)
    parser.add_argument("--xbox-area-forge", required=True, type=Path)
    parser.add_argument("--entry-name", required=True)
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--output-language-forge", required=True, type=Path)
    parser.add_argument("--output-area-forge", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--ffmpeg",
        type=Path,
        help="ffmpeg executable, required for embedded Ogg conversion",
    )
    parser.add_argument(
        "--verify-directory",
        type=Path,
        help="write standalone header/stream pairs for vgmstream checks",
    )
    parser.add_argument(
        "--max-tracks",
        type=int,
        help="limit the probe to the first N embedded IMA tracks",
    )
    parser.add_argument(
        "--include-resource",
        action="append",
        default=[],
        help="only convert this class-2 BAO resource (repeatable)",
    )
    parser.add_argument(
        "--reuse-stream",
        action="append",
        default=[],
        metavar="RESOURCE=HEX_ID",
        help="reuse an existing class-5 language stream slot",
    )
    args = parser.parse_args()

    source_raw = forge_entry_payload(args.xbox_area_forge, args.entry_name)
    with tempfile.TemporaryDirectory(
        prefix="ac1_dub_external_"
    ) as temporary_name:
        temporary = Path(temporary_name)
        xbox_decoded, decoder_returncode, decoder_output = decode_xbox_prefix(
            source_raw, args.decoder, temporary, f"xbox_{args.entry_name}"
        )
        xbox_directory = (xbox_decoded / "0.dat").read_bytes()
        xbox_data_original = (xbox_decoded / "1.dat").read_bytes()
        xbox_data = bytearray(xbox_data_original)
        pc_data = args.pc_data.read_bytes()
        pc_resources = {
            item.name: item
            for item in parse_bundle(
                args.pc_directory, args.pc_data, "little"
            )
        }
        xbox_resources = {
            item.name: item
            for item in parse_bundle(
                xbox_decoded / "0.dat",
                xbox_decoded / "1.dat",
                "big",
            )
        }
        language_archive = parse_forge(args.xbox_language_forge)
        language_streams = entries_by_audio_id(
            language_archive, XBOX_LANGUAGE_PREFIX
        )
        used_language_ids = set(language_streams)
        template_entry = next(iter(language_streams.values()))
        template_payload = entry_payload(
            args.xbox_language_forge, template_entry
        )
        ima_template_entry = language_streams.get(0x50EA91C8)
        ima_template_payload = (
            entry_payload(args.xbox_language_forge, ima_template_entry)
            if ima_template_entry is not None
            else None
        )

        additions: list[tuple[str, int, bytes]] = []
        replacement_payloads: dict[int, bytes] = {}
        headers: list[dict[str, object]] = []
        verification_files: list[tuple[str, bytes, str, bytes]] = []
        included_resources = set(args.include_resource)
        reused_streams: dict[str, int] = {}
        for specification in args.reuse_stream:
            resource_name, separator, value = specification.partition("=")
            if not separator:
                raise ValueError(
                    f"invalid --reuse-stream value: {specification}"
                )
            reused_streams[resource_name] = int(value, 16)
        for name, pc_resource in pc_resources.items():
            if not name.startswith("BAO_0x2"):
                continue
            if included_resources and name not in included_resources:
                continue
            xbox_resource = xbox_resources.get(name)
            if xbox_resource is None:
                continue
            pc_blob = resource_blob(pc_data, pc_resource)
            xbox_blob = resource_blob(xbox_data_original, xbox_resource)
            pc_bao = pc_blob.find(BAO_SIGNATURE)
            xbox_bao = xbox_blob.find(BAO_SIGNATURE)
            if (
                pc_bao < 0
                or xbox_bao < 0
                or pc_bao + 0xA4 > len(pc_blob)
                or xbox_bao + 0xA4 > len(xbox_blob)
            ):
                continue
            if u32(pc_blob, pc_bao + 0x20, "little") >> 28 != 2:
                continue
            if u32(pc_blob, pc_bao + 0x2C, "little") != 1:
                continue
            codec = u32(pc_blob, pc_bao + 0x8C, "little")
            if codec not in (3, 4):
                continue
            audio_id = u32(pc_blob, pc_bao + 0x44, "little")
            if audio_id in language_streams:
                continue
            memory_name = f"BAO_0x{audio_id:08x}"
            pc_memory_resource = pc_resources.get(memory_name)
            if pc_memory_resource is None:
                continue
            pc_memory = resource_blob(pc_data, pc_memory_resource)
            pc_memory_bao = pc_memory.find(BAO_SIGNATURE)
            if pc_memory_bao < 0:
                continue

            stream_id = reused_streams.get(name)
            if stream_id is None:
                stream_id = allocate_stream_id(
                    audio_id, used_language_ids
                )
            elif stream_id not in language_streams:
                raise ValueError(
                    f"{name}: reused stream {stream_id:08X} not found"
                )
            if codec == 3:
                patched = externalize_header(
                    xbox_blob,
                    xbox_bao,
                    pc_blob,
                    pc_bao,
                    stream_id,
                )
                codec_payload = pc_memory[
                    pc_memory_bao + BAO_STREAM_HEADER_SIZE :
                ]
                sample_count = u32(
                    pc_blob, pc_bao + 0x78, "little"
                )
                mode = "embedded-ima"
            else:
                if args.ffmpeg is None:
                    raise ValueError(
                        f"{name}: --ffmpeg is required for embedded Ogg"
                    )
                if ima_template_payload is None:
                    raise ValueError(
                        "no existing Ubisoft IMA payload template found"
                    )
                channels = u32(pc_blob, pc_bao + 0x6C, "little")
                sample_rate = u32(pc_blob, pc_bao + 0x70, "little")
                if channels != 1:
                    raise ValueError(
                        f"{name}: only mono embedded Ogg is supported"
                    )
                ogg_path = temporary / f"{name}.ogg"
                pcm_path = temporary / f"{name}.pcm"
                ogg_path.write_bytes(
                    pc_memory[
                        pc_memory_bao + BAO_STREAM_HEADER_SIZE :
                    ]
                )
                completed = subprocess.run(
                    [
                        str(args.ffmpeg),
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(ogg_path),
                        "-f",
                        "s16le",
                        "-acodec",
                        "pcm_s16le",
                        "-ac",
                        "1",
                        "-ar",
                        str(sample_rate),
                        str(pcm_path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    raise ValueError(
                        f"{name}: ffmpeg failed: {completed.stderr.strip()}"
                    )
                pcm = pcm_path.read_bytes()
                if len(pcm) % 2:
                    raise ValueError(f"{name}: odd PCM byte count")
                samples = list(
                    struct.unpack(f"<{len(pcm) // 2}h", pcm)
                )
                codec_payload = encode_ubi_ima_mono(
                    samples,
                    ima_template_payload[
                        BAO_STREAM_HEADER_SIZE :
                        BAO_STREAM_HEADER_SIZE + 0x1C
                    ],
                )
                sample_count = len(samples)
                patched_bytes = bytearray(xbox_blob)
                overrides = {
                    0x30: len(codec_payload),
                    0x38: len(codec_payload),
                    0x3C: 0,
                    0x40: 1,
                    0x44: stream_id,
                    0x50: 1,
                    0x5C: 0,
                    0x6C: 1,
                    0x70: sample_rate,
                    0x78: sample_count,
                    0x80: 0,
                    0x88: 0,
                    0x8C: 3,
                    0x9C: 0,
                }
                for offset, value in overrides.items():
                    struct.pack_into(
                        ">I",
                        patched_bytes,
                        xbox_bao + offset,
                        value,
                    )
                patched = bytes(patched_bytes)
                mode = "embedded-ogg-to-ima"
            xbox_data[
                xbox_resource.offset :
                xbox_resource.offset + xbox_resource.size
            ] = patched
            payload = (
                template_payload[:BAO_STREAM_HEADER_SIZE]
                + codec_payload
            )
            stream_name = f"English_BAO_0x{stream_id:08x}"
            reused = name in reused_streams
            if reused:
                replacement_payloads[
                    language_streams[stream_id].index
                ] = payload
            else:
                additions.append((stream_name, stream_id, payload))
            verification_files.append(
                (
                    f"{name}.bao",
                    patched[xbox_bao:],
                    stream_name,
                    payload,
                )
            )
            headers.append(
                {
                    "resource": name,
                    "memory_audio_id": f"{audio_id:08X}",
                    "stream_audio_id": f"{stream_id:08X}",
                    "stream_name": stream_name,
                    "payload_size": len(payload),
                    "samples": sample_count,
                    "mode": mode,
                    "reused_existing_stream": reused,
                }
            )
            if (
                args.max_tracks is not None
                and len(additions) + len(replacement_payloads)
                >= args.max_tracks
            ):
                break
        if not additions and not replacement_payloads:
            raise ValueError("no embedded audio tracks selected")
        if args.verify_directory is not None:
            args.verify_directory.mkdir(parents=True, exist_ok=True)
            for (
                header_name,
                header_payload,
                stream_name,
                stream_payload,
            ) in verification_files:
                (args.verify_directory / header_name).write_bytes(
                    header_payload
                )
                (args.verify_directory / stream_name).write_bytes(
                    stream_payload
                )

        patched_data = bytes(xbox_data)
        modified_indices = changed_chunks(
            xbox_data_original, patched_data
        )
        wrappers = wrapper_source_chunks(source_raw)
        ranges = wrapper_ranges(source_raw)
        if len(wrappers) != 2 or len(ranges) != 2:
            raise ValueError("expected two compression wrappers")
        data_chunks = list(wrappers[1])
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
        directory_start, directory_end = ranges[0]
        data_start, _data_end = ranges[1]
        rebuilt_raw = (
            source_raw[directory_start:directory_end]
            + serialize_wrapper(
                source_raw[data_start : data_start + 17], data_chunks
            )
        )
        verified, verify_returncode, verify_output = decode_xbox_prefix(
            rebuilt_raw, args.decoder, temporary, "verify_externalized"
        )
        if (verified / "0.dat").read_bytes() != xbox_directory:
            raise AssertionError("externalized directory decode differs")
        if (verified / "1.dat").read_bytes() != patched_data:
            raise AssertionError("externalized data decode differs")

        if replacement_payloads and additions:
            intermediate = temporary / "language_reused.forge"
            batch_replace_forge_entries(
                args.xbox_language_forge,
                intermediate,
                replacement_payloads,
            )
            language_report = add_language_entries(
                intermediate,
                args.output_language_forge,
                additions,
            )
            language_report["reused"] = len(replacement_payloads)
        elif replacement_payloads:
            batch_replace_forge_entries(
                args.xbox_language_forge,
                args.output_language_forge,
                replacement_payloads,
            )
            language_report = {
                "added": 0,
                "reused": len(replacement_payloads),
                "old_count": len(language_archive.entries),
                "new_count": len(language_archive.entries),
            }
        else:
            language_report = add_language_entries(
                args.xbox_language_forge,
                args.output_language_forge,
                additions,
            )
        # Header-only externalization keeps the decoded entry extent exactly
        # stable.  Retain its physical FORGE offset as some AC1 area metadata
        # addresses entries directly instead of following the archive index.
        area_report = replace_forge_entries_inplace(
            args.xbox_area_forge,
            args.output_area_forge,
            {args.entry_name: rebuilt_raw},
        )

    report = {
        "mode": "externalized-embedded-ima-probe",
        "entry": args.entry_name,
        "tracks": len(additions),
        "headers": headers,
        "changed_data_chunks": modified_indices,
        "raw_before": len(source_raw),
        "raw_after": len(rebuilt_raw),
        "language": language_report,
        "area_replace": area_report,
        "decoder_returncode": decoder_returncode,
        "decoder_output": decoder_output,
        "verify_decoder_returncode": verify_returncode,
        "verify_decoder_output": verify_output,
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
