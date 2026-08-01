#!/usr/bin/env python3
"""Build a broad AC1 Czech-dub probe for the Masyaf audio header bank.

The PC Czech language streams are already encoded as codecs supported by the
Xbox 360 AC1 engine (primarily Ubisoft IMA ADPCM, with a few Ogg streams).
This tool therefore preserves the Xbox class-5 BAO wrapper, replaces only its
audio payload, and converts the first/English class-2 BAO metadata object from
little-endian PC values to big-endian Xbox values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ac1_build_gui_forge import replace_forge_entry, stored_wrapper
from ac1_forge import ForgeArchive, ForgeEntry, parse_forge
from ac1_resource_bundle import parse_bundle


BAO_SIGNATURE = bytes.fromhex("011B0100")
BAO_RESOURCE_TYPE = 0xD8295DCB
FILEDATA_SIZE_OFFSET = 395
BAO_STREAM_HEADER_SIZE = 40
PC_LANGUAGE_PREFIX = "Czech_BAO_0x"
XBOX_LANGUAGE_PREFIX = "English_BAO_0x"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def id_from_name(name: str) -> int | None:
    marker = "_BAO_0x"
    if marker not in name:
        return None
    try:
        return int(name.rsplit("0x", 1)[1], 16)
    except ValueError:
        return None


def entry_payload(path: Path, entry: ForgeEntry) -> bytes:
    with path.open("rb") as stream:
        stream.seek(entry.offset + 440)
        payload = stream.read(entry.size)
    if len(payload) != entry.size:
        raise ValueError(f"{path}: truncated {entry.name}")
    return payload


def entries_by_audio_id(archive: ForgeArchive, prefix: str) -> dict[int, ForgeEntry]:
    result: dict[int, ForgeEntry] = {}
    for entry in archive.entries:
        if not entry.name.startswith(prefix):
            continue
        audio_id = id_from_name(entry.name)
        if audio_id is None or audio_id in result:
            raise ValueError(f"invalid or duplicate language BAO: {entry.name}")
        result[audio_id] = entry
    return result


def batch_replace_forge_entries(
    source: Path,
    output: Path,
    replacements: dict[int, bytes],
) -> None:
    archive = parse_forge(source)
    by_index = {entry.index: entry for entry in archive.entries}
    unknown = set(replacements) - set(by_index)
    if unknown:
        raise ValueError(f"unknown FORGE entry indexes: {sorted(unknown)[:10]}")

    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    with source.open("rb") as source_stream, output.open("r+b") as output_stream:
        output_stream.seek(0, 2)
        for index in sorted(replacements):
            entry = by_index[index]
            payload = replacements[index]
            source_stream.seek(entry.offset)
            wrapper = bytearray(source_stream.read(440))
            if len(wrapper) != 440 or wrapper[:8] != b"FILEDATA":
                raise ValueError(f"{entry.name}: unsupported FILEDATA wrapper")
            struct.pack_into("<I", wrapper, FILEDATA_SIZE_OFFSET, len(payload))

            new_offset = output_stream.tell()
            alignment = (-new_offset) & 0x7FF
            if alignment:
                output_stream.write(b"\0" * alignment)
                new_offset += alignment
            output_stream.write(wrapper)
            output_stream.write(payload)

            output_stream.seek(entry.record_offset)
            output_stream.write(struct.pack("<Q", new_offset))
            output_stream.seek(entry.record_offset + 12)
            output_stream.write(struct.pack("<I", len(payload)))
            output_stream.seek(0, 2)

    check = parse_forge(output)
    for index, payload in replacements.items():
        if check.entries[index].size != len(payload):
            raise AssertionError(f"FORGE verification failed for entry #{index}")


def u32(data: bytes, offset: int, endian: str) -> int:
    return struct.unpack_from("<I" if endian == "little" else ">I", data, offset)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pc-directory", required=True, type=Path)
    parser.add_argument("--pc-data", required=True, type=Path)
    parser.add_argument("--xbox-directory", required=True, type=Path)
    parser.add_argument("--xbox-data", required=True, type=Path)
    parser.add_argument("--pc-language-forge", required=True, type=Path)
    parser.add_argument("--xbox-language-forge", required=True, type=Path)
    parser.add_argument("--xbox-masyaf-forge", required=True, type=Path)
    parser.add_argument("--output-language-forge", required=True, type=Path)
    parser.add_argument("--output-masyaf-forge", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    pc_resources = parse_bundle(args.pc_directory, args.pc_data, "little")
    xbox_resources = parse_bundle(args.xbox_directory, args.xbox_data, "big")
    pc_by_name = {
        resource.name: resource
        for resource in pc_resources
        if resource.type_id == BAO_RESOURCE_TYPE
    }
    xbox_by_name = {
        resource.name: resource
        for resource in xbox_resources
        if resource.type_id == BAO_RESOURCE_TYPE
    }

    pc_language_archive = parse_forge(args.pc_language_forge)
    xbox_language_archive = parse_forge(args.xbox_language_forge)
    pc_streams = entries_by_audio_id(pc_language_archive, PC_LANGUAGE_PREFIX)
    xbox_streams = entries_by_audio_id(xbox_language_archive, XBOX_LANGUAGE_PREFIX)

    pc_data = args.pc_data.read_bytes()
    xbox_data = bytearray(args.xbox_data.read_bytes())
    patched_headers: list[dict[str, int | str]] = []
    selected_ids: set[int] = set()

    for name, pc_resource in pc_by_name.items():
        if not name.startswith("BAO_0x2") or name not in xbox_by_name:
            continue
        xbox_resource = xbox_by_name[name]
        pc_blob = pc_data[
            pc_resource.offset : pc_resource.offset + pc_resource.size
        ]
        xbox_blob = xbox_data[
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
            raise ValueError(f"{name}: PC/Xbox stream ID differs")
        codec = u32(pc_blob, pc_bao + 0x8C, "little")
        if codec not in (3, 4):
            continue

        # The serialized audio metadata body uses the same field layout on
        # both platforms.  Convert each complete dword, keeping the Xbox base
        # header and the remaining four language objects untouched.
        absolute = xbox_resource.offset + xbox_bao
        for field_offset in range(0x28, 0xA4, 4):
            value = u32(pc_blob, pc_bao + field_offset, "little")
            struct.pack_into(">I", xbox_data, absolute + field_offset, value)

        stream_size = u32(pc_blob, pc_bao + 0x30, "little")
        samples = u32(pc_blob, pc_bao + 0x78, "little")
        patched_headers.append(
            {
                "resource": name,
                "audio_id": f"{audio_id:08X}",
                "codec": codec,
                "stream_size": stream_size,
                "samples": samples,
            }
        )
        selected_ids.add(audio_id)

    if not selected_ids:
        raise ValueError("no matching Czech language headers were selected")

    replacements: dict[int, bytes] = {}
    for audio_id in sorted(selected_ids):
        pc_entry = pc_streams[audio_id]
        xbox_entry = xbox_streams[audio_id]
        pc_payload = entry_payload(args.pc_language_forge, pc_entry)
        xbox_payload = entry_payload(args.xbox_language_forge, xbox_entry)
        if len(pc_payload) < BAO_STREAM_HEADER_SIZE:
            raise ValueError(f"{pc_entry.name}: truncated PC stream BAO")
        if len(xbox_payload) < BAO_STREAM_HEADER_SIZE:
            raise ValueError(f"{xbox_entry.name}: truncated Xbox stream BAO")
        new_payload = (
            xbox_payload[:BAO_STREAM_HEADER_SIZE]
            + pc_payload[BAO_STREAM_HEADER_SIZE:]
        )
        replacements[xbox_entry.index] = new_payload

    batch_replace_forge_entries(
        args.xbox_language_forge, args.output_language_forge, replacements
    )

    rebuilt_raw = stored_wrapper(args.xbox_directory.read_bytes()) + stored_wrapper(
        bytes(xbox_data)
    )
    replace_forge_entry(
        args.xbox_masyaf_forge,
        args.output_masyaf_forge,
        "Masyaf",
        rebuilt_raw,
    )

    codec_counts: dict[str, int] = {}
    for item in patched_headers:
        key = str(item["codec"])
        codec_counts[key] = codec_counts.get(key, 0) + 1
    report = {
        "scope": "Masyaf main BAO bank",
        "patched_header_count": len(patched_headers),
        "patched_stream_count": len(replacements),
        "codec_counts": codec_counts,
        "first_headers": patched_headers[:20],
        "language_forge_size": args.output_language_forge.stat().st_size,
        "masyaf_forge_size": args.output_masyaf_forge.stat().st_size,
        "language_forge_sha256": sha256_file(args.output_language_forge),
        "masyaf_forge_sha256": sha256_file(args.output_masyaf_forge),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
