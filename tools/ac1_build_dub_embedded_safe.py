#!/usr/bin/env python3
"""Port embedded and streamed Czech AC1 speech for one Xbox resource bank.

Some AC1 scenes keep short voice clips as class-3 BAOs inside the area resource
bundle instead of Data360_StreamedSoundsEng.forge.  This builder converts the
matching class-2 metadata, grafts the PC Czech class-3 payload under the Xbox
big-endian BAO/resource envelope, rebuilds the variable-sized resource bundle,
and stores both bundle streams without LZX recompression.
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
from ac1_build_complete_text import forge_entry_payload, replace_forge_entries
from ac1_build_dub_masyaf_probe import (
    BAO_RESOURCE_TYPE,
    BAO_SIGNATURE,
    BAO_STREAM_HEADER_SIZE,
    PC_LANGUAGE_PREFIX,
    XBOX_LANGUAGE_PREFIX,
    batch_replace_forge_entries,
    entries_by_audio_id,
    entry_payload,
    u32,
)
from ac1_build_gui_forge import stored_wrapper
from ac1_forge import parse_forge
from ac1_resource_bundle import Resource, parse_bundle


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def resource_blob(data: bytes, resource: Resource) -> bytes:
    return data[resource.offset : resource.offset + resource.size]


def patch_header(
    xbox_blob: bytes, xbox_bao: int, pc_blob: bytes, pc_bao: int
) -> bytes:
    patched = bytearray(xbox_blob)
    for offset in range(0x28, 0xA4, 4):
        struct.pack_into(
            ">I",
            patched,
            xbox_bao + offset,
            u32(pc_blob, pc_bao + offset, "little"),
        )
    return bytes(patched)


def graft_memory_bao(
    xbox_blob: bytes, xbox_bao: int, pc_blob: bytes, pc_bao: int
) -> bytes:
    # AC1 class-3 atomic memory BAOs start their codec payload at +0x2C.
    rebuilt = bytearray(
        xbox_blob[: xbox_bao + 0x2C] + pc_blob[pc_bao + 0x2C :]
    )
    old_declared = struct.unpack_from(">I", xbox_blob, 4)[0]
    delta = len(rebuilt) - len(xbox_blob)
    new_declared = old_declared + delta
    if new_declared < 0 or new_declared > 0xFFFFFFFF:
        raise ValueError("rebuilt resource declared length is invalid")
    struct.pack_into(">I", rebuilt, 4, new_declared)
    return bytes(rebuilt)


def rebuild_directory(
    original: bytes, resources: list[Resource], blobs: list[bytes]
) -> bytes:
    if len(resources) != len(blobs):
        raise ValueError("resource/blob count differs")
    rebuilt = bytearray(original)
    count = struct.unpack_from(">H", original, 0)[0]
    if count != len(resources):
        raise ValueError("resource directory count differs")
    for resource, blob in zip(resources, blobs):
        record = 2 + resource.index * 8
        directory_id = struct.unpack_from(">I", original, record)[0]
        if directory_id != resource.directory_id:
            raise ValueError("resource directory order differs")
        struct.pack_into(">I", rebuilt, record + 4, len(blob))
    return bytes(rebuilt)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pc-directory", required=True, type=Path)
    parser.add_argument("--pc-data", required=True, type=Path)
    parser.add_argument("--pc-language-forge", required=True, type=Path)
    parser.add_argument("--xbox-language-forge", required=True, type=Path)
    parser.add_argument("--xbox-area-forge", required=True, type=Path)
    parser.add_argument("--entry-name", required=True)
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--output-language-forge", required=True, type=Path)
    parser.add_argument("--output-area-forge", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    source_raw = forge_entry_payload(args.xbox_area_forge, args.entry_name)
    with tempfile.TemporaryDirectory(
        prefix="ac1_dub_embedded_"
    ) as temporary_name:
        temporary = Path(temporary_name)
        xbox_decoded, decoder_returncode, decoder_output = decode_xbox_prefix(
            source_raw, args.decoder, temporary, f"xbox_{args.entry_name}"
        )
        xbox_directory = (xbox_decoded / "0.dat").read_bytes()
        xbox_data = (xbox_decoded / "1.dat").read_bytes()
        pc_data = args.pc_data.read_bytes()

        pc_resources_list = parse_bundle(
            args.pc_directory, args.pc_data, "little"
        )
        xbox_resources_list = parse_bundle(
            xbox_decoded / "0.dat", xbox_decoded / "1.dat", "big"
        )
        pc_resources = {item.name: item for item in pc_resources_list}
        xbox_resources = {item.name: item for item in xbox_resources_list}

        pc_language_archive = parse_forge(args.pc_language_forge)
        xbox_language_archive = parse_forge(args.xbox_language_forge)
        pc_streams = entries_by_audio_id(
            pc_language_archive, PC_LANGUAGE_PREFIX
        )
        xbox_streams = entries_by_audio_id(
            xbox_language_archive, XBOX_LANGUAGE_PREFIX
        )

        replacements: dict[str, bytes] = {}
        embedded_ids: set[int] = set()
        external_ids: set[int] = set()
        skipped_codec4 = 0
        header_reports: list[dict[str, object]] = []

        for name, pc_resource in pc_resources.items():
            if not name.startswith("BAO_0x2"):
                continue
            xbox_resource = xbox_resources.get(name)
            if xbox_resource is None:
                continue
            pc_blob = resource_blob(pc_data, pc_resource)
            xbox_blob = resource_blob(xbox_data, xbox_resource)
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
            if u32(xbox_blob, xbox_bao + 0x20, "big") >> 28 != 2:
                continue
            if u32(pc_blob, pc_bao + 0x2C, "little") != 1:
                continue
            codec = u32(pc_blob, pc_bao + 0x8C, "little")
            if codec == 4:
                skipped_codec4 += 1
                continue
            if codec != 3:
                continue

            audio_id = u32(pc_blob, pc_bao + 0x44, "little")
            if u32(xbox_blob, xbox_bao + 0x44, "big") != audio_id:
                raise ValueError(f"{name}: PC/Xbox audio ID differs")
            memory_name = f"BAO_0x{audio_id:08x}"
            mode: str
            if audio_id in pc_streams and audio_id in xbox_streams:
                external_ids.add(audio_id)
                mode = "external"
            elif (
                memory_name in pc_resources
                and memory_name in xbox_resources
            ):
                pc_memory_resource = pc_resources[memory_name]
                xbox_memory_resource = xbox_resources[memory_name]
                pc_memory = resource_blob(pc_data, pc_memory_resource)
                xbox_memory = resource_blob(
                    xbox_data, xbox_memory_resource
                )
                pc_memory_bao = pc_memory.find(BAO_SIGNATURE)
                xbox_memory_bao = xbox_memory.find(BAO_SIGNATURE)
                if pc_memory_bao < 0 or xbox_memory_bao < 0:
                    raise ValueError(f"{memory_name}: BAO signature missing")
                replacements[memory_name] = graft_memory_bao(
                    xbox_memory,
                    xbox_memory_bao,
                    pc_memory,
                    pc_memory_bao,
                )
                embedded_ids.add(audio_id)
                mode = "embedded"
            else:
                continue

            replacements[name] = patch_header(
                xbox_blob, xbox_bao, pc_blob, pc_bao
            )
            header_reports.append(
                {
                    "resource": name,
                    "audio_id": f"{audio_id:08X}",
                    "mode": mode,
                    "stream_size": u32(
                        pc_blob, pc_bao + 0x30, "little"
                    ),
                    "samples": u32(pc_blob, pc_bao + 0x78, "little"),
                }
            )

        if not header_reports:
            raise ValueError("no embedded or external Czech speech selected")

        rebuilt_blobs = [
            replacements.get(
                resource.name, resource_blob(xbox_data, resource)
            )
            for resource in xbox_resources_list
        ]
        rebuilt_directory = rebuild_directory(
            xbox_directory, xbox_resources_list, rebuilt_blobs
        )
        rebuilt_data = b"".join(rebuilt_blobs)
        rebuilt_raw = stored_wrapper(rebuilt_directory) + stored_wrapper(
            rebuilt_data
        )

        verified, verify_returncode, verify_output = decode_xbox_prefix(
            rebuilt_raw, args.decoder, temporary, "verify_embedded_audio"
        )
        if (verified / "0.dat").read_bytes() != rebuilt_directory:
            raise AssertionError("rebuilt directory did not decode exactly")
        verified_data = (verified / "1.dat").read_bytes()
        if verified_data != rebuilt_data:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            (args.report.parent / "debug_rebuilt_raw.bin").write_bytes(
                rebuilt_raw
            )
            mismatch = next(
                (
                    index
                    for index, (left, right) in enumerate(
                        zip(verified_data, rebuilt_data)
                    )
                    if left != right
                ),
                min(len(verified_data), len(rebuilt_data)),
            )
            raise AssertionError(
                "rebuilt data did not decode exactly: "
                f"decoded={len(verified_data)}, expected={len(rebuilt_data)}, "
                f"first_mismatch=0x{mismatch:X}, decoder={verify_output!r}"
            )

        language_replacements: dict[int, bytes] = {}
        for audio_id in sorted(external_ids):
            pc_entry = pc_streams[audio_id]
            xbox_entry = xbox_streams[audio_id]
            pc_payload = entry_payload(args.pc_language_forge, pc_entry)
            xbox_payload = entry_payload(
                args.xbox_language_forge, xbox_entry
            )
            new_payload = (
                xbox_payload[:BAO_STREAM_HEADER_SIZE]
                + pc_payload[BAO_STREAM_HEADER_SIZE:]
            )
            if new_payload != xbox_payload:
                language_replacements[xbox_entry.index] = new_payload

        if language_replacements:
            batch_replace_forge_entries(
                args.xbox_language_forge,
                args.output_language_forge,
                language_replacements,
            )
        else:
            args.output_language_forge.parent.mkdir(
                parents=True, exist_ok=True
            )
            shutil.copyfile(
                args.xbox_language_forge, args.output_language_forge
            )
        area_report = replace_forge_entries(
            args.xbox_area_forge,
            args.output_area_forge,
            {args.entry_name: rebuilt_raw},
        )

    report = {
        "entry": args.entry_name,
        "patched_headers": len(header_reports),
        "embedded_czech_streams": len(embedded_ids),
        "external_czech_streams": len(external_ids),
        "new_external_payloads": len(language_replacements),
        "skipped_codec4": skipped_codec4,
        "resource_replacements": len(replacements),
        "decoded_directory_before": len(xbox_directory),
        "decoded_directory_after": len(rebuilt_directory),
        "decoded_data_before": len(xbox_data),
        "decoded_data_after": len(rebuilt_data),
        "raw_before": len(source_raw),
        "raw_after": len(rebuilt_raw),
        "area_replace": area_report,
        "decoder_returncode": decoder_returncode,
        "decoder_output": decoder_output,
        "verify_decoder_returncode": verify_returncode,
        "verify_decoder_output": verify_output,
        "first_headers": header_reports[:20],
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
