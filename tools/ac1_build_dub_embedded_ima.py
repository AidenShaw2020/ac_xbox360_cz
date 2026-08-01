#!/usr/bin/env python3
"""Replace Xbox embedded XMA dialogue variants with converted PC Czech audio."""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import tempfile
from pathlib import Path

from ac1_build_all_mgb_archive import decode_xbox_prefix
from ac1_build_complete_text import forge_entry_payload, replace_forge_entries
from ac1_build_dub_embedded_safe import (
    rebuild_directory,
    resource_blob,
    sha256_path,
)
from ac1_build_dub_masyaf_probe import (
    BAO_SIGNATURE,
    BAO_STREAM_HEADER_SIZE,
    XBOX_LANGUAGE_PREFIX,
    entries_by_audio_id,
    entry_payload,
)
from ac1_build_gui_forge import stored_wrapper
from ac1_forge import parse_forge
from ac1_resource_bundle import parse_bundle
from ac1_ubi_ima import encode_ubi_ima_mono


BASE_HEADER_SIZE = 0xA4


def signature_offsets(blob: bytes) -> list[int]:
    return [
        offset
        for offset in range(len(blob) - len(BAO_SIGNATURE) + 1)
        if blob.startswith(BAO_SIGNATURE, offset)
    ]


def u32(data: bytes, offset: int, endian: str) -> int:
    prefix = "<" if endian == "little" else ">"
    return struct.unpack_from(f"{prefix}I", data, offset)[0]


def set_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into(">I", data, offset, value)


def replace_variant(
    blob: bytes,
    offsets: list[int],
    variant: int,
    replacement: bytes,
) -> bytes:
    index = variant - 1
    if index < 0 or index >= len(offsets):
        raise ValueError(
            f"variant {variant} outside 1..{len(offsets)}"
        )
    start = offsets[index]
    end = offsets[index + 1] if index + 1 < len(offsets) else len(blob)
    rebuilt = bytearray(blob[:start] + replacement + blob[end:])
    declared = struct.unpack_from(">I", blob, 4)[0]
    set_u32(rebuilt, 4, declared + len(replacement) - (end - start))
    return bytes(rebuilt)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pc-directory", required=True, type=Path)
    parser.add_argument("--pc-data", required=True, type=Path)
    parser.add_argument("--xbox-area-forge", required=True, type=Path)
    parser.add_argument("--ima-template-forge", required=True, type=Path)
    parser.add_argument("--entry-name", required=True)
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--output-area-forge", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--include-resource", action="append", required=True
    )
    parser.add_argument("--pc-variant", type=int, default=2)
    parser.add_argument("--xbox-variant", type=int, default=1)
    parser.add_argument("--verify-directory", type=Path)
    args = parser.parse_args()

    source_raw = forge_entry_payload(args.xbox_area_forge, args.entry_name)
    with tempfile.TemporaryDirectory(
        prefix="ac1_dub_embedded_ima_"
    ) as temporary_name:
        temporary = Path(temporary_name)
        decoded, decoder_returncode, decoder_output = decode_xbox_prefix(
            source_raw, args.decoder, temporary, "xbox_embedded_ima"
        )
        xbox_directory_path = decoded / "0.dat"
        xbox_data_path = decoded / "1.dat"
        xbox_directory = xbox_directory_path.read_bytes()
        xbox_data = xbox_data_path.read_bytes()
        pc_data = args.pc_data.read_bytes()

        pc_resources_list = parse_bundle(
            args.pc_directory, args.pc_data, "little"
        )
        xbox_resources_list = parse_bundle(
            xbox_directory_path, xbox_data_path, "big"
        )
        pc_resources = {
            resource.name: resource for resource in pc_resources_list
        }
        xbox_resources = {
            resource.name: resource for resource in xbox_resources_list
        }

        template_archive = parse_forge(args.ima_template_forge)
        template_streams = entries_by_audio_id(
            template_archive, XBOX_LANGUAGE_PREFIX
        )
        template_entry = template_streams.get(0x50EA91C8)
        if template_entry is None:
            raise ValueError("Ubisoft IMA template stream 50EA91C8 missing")
        template_payload = entry_payload(
            args.ima_template_forge, template_entry
        )
        ima_frame_header = template_payload[
            BAO_STREAM_HEADER_SIZE :
            BAO_STREAM_HEADER_SIZE + 0x1C
        ]

        replacements: dict[str, bytes] = {}
        rows: list[dict[str, object]] = []
        if args.verify_directory is not None:
            args.verify_directory.mkdir(parents=True, exist_ok=True)

        for header_name in args.include_resource:
            pc_header_resource = pc_resources.get(header_name)
            xbox_header_resource = xbox_resources.get(header_name)
            if pc_header_resource is None or xbox_header_resource is None:
                raise ValueError(f"{header_name}: header resource missing")

            pc_header = resource_blob(pc_data, pc_header_resource)
            xbox_header = resource_blob(xbox_data, xbox_header_resource)
            pc_header_offsets = signature_offsets(pc_header)
            xbox_header_offsets = signature_offsets(xbox_header)
            pc_index = args.pc_variant - 1
            xbox_index = args.xbox_variant - 1
            if pc_index not in range(len(pc_header_offsets)):
                raise ValueError(f"{header_name}: PC variant missing")
            if xbox_index not in range(len(xbox_header_offsets)):
                raise ValueError(f"{header_name}: Xbox variant missing")

            pc_header_bao = pc_header_offsets[pc_index]
            xbox_header_bao = xbox_header_offsets[xbox_index]
            pc_audio_id = u32(
                pc_header, pc_header_bao + 0x44, "little"
            )
            xbox_audio_id = u32(
                xbox_header, xbox_header_bao + 0x44, "big"
            )
            if pc_audio_id != xbox_audio_id:
                raise ValueError(f"{header_name}: audio ID differs")
            memory_name = f"BAO_0x{xbox_audio_id:08x}"
            pc_memory_resource = pc_resources.get(memory_name)
            xbox_memory_resource = xbox_resources.get(memory_name)
            if pc_memory_resource is None or xbox_memory_resource is None:
                raise ValueError(f"{header_name}: {memory_name} missing")

            pc_memory = resource_blob(pc_data, pc_memory_resource)
            xbox_memory = replacements.get(
                memory_name,
                resource_blob(xbox_data, xbox_memory_resource),
            )
            pc_memory_offsets = signature_offsets(pc_memory)
            xbox_memory_offsets = signature_offsets(xbox_memory)
            if pc_index not in range(len(pc_memory_offsets)):
                raise ValueError(f"{memory_name}: PC variant missing")
            if xbox_index not in range(len(xbox_memory_offsets)):
                raise ValueError(f"{memory_name}: Xbox variant missing")
            pc_start = pc_memory_offsets[pc_index]
            pc_end = (
                pc_memory_offsets[pc_index + 1]
                if pc_index + 1 < len(pc_memory_offsets)
                else len(pc_memory)
            )
            ogg_payload = pc_memory[
                pc_start + BAO_STREAM_HEADER_SIZE : pc_end
            ]
            sample_rate = u32(
                pc_header, pc_header_bao + 0x70, "little"
            )
            channels = u32(
                pc_header, pc_header_bao + 0x6C, "little"
            )
            if channels != 1:
                raise ValueError(f"{header_name}: expected mono dialogue")

            ogg_path = temporary / f"{header_name}.ogg"
            pcm_path = temporary / f"{header_name}.pcm"
            ogg_path.write_bytes(ogg_payload)
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
                    f"{header_name}: ffmpeg failed: "
                    f"{completed.stderr.strip()}"
                )
            pcm = pcm_path.read_bytes()
            samples = list(struct.unpack(f"<{len(pcm) // 2}h", pcm))
            codec_payload = encode_ubi_ima_mono(
                samples, ima_frame_header
            )

            memory_section_start = xbox_memory_offsets[xbox_index]
            memory_bao_header = xbox_memory[
                memory_section_start :
                memory_section_start + BAO_STREAM_HEADER_SIZE
            ]
            new_memory_section = memory_bao_header + codec_payload
            patched_memory = replace_variant(
                xbox_memory,
                xbox_memory_offsets,
                args.xbox_variant,
                new_memory_section,
            )
            replacements[memory_name] = patched_memory

            xbox_header = replacements.get(header_name, xbox_header)
            xbox_header_offsets = signature_offsets(xbox_header)
            xbox_header_bao = xbox_header_offsets[xbox_index]
            new_header_section = bytearray(
                xbox_header[
                    xbox_header_bao :
                    xbox_header_bao + BASE_HEADER_SIZE
                ]
            )
            set_u32(new_header_section, 0x30, len(codec_payload))
            set_u32(new_header_section, 0x44, xbox_audio_id)
            set_u32(new_header_section, 0x50, 0)
            set_u32(new_header_section, 0x5C, 0)
            set_u32(new_header_section, 0x6C, 1)
            set_u32(new_header_section, 0x70, sample_rate)
            set_u32(new_header_section, 0x78, len(samples))
            set_u32(new_header_section, 0x80, 0)
            set_u32(new_header_section, 0x8C, 3)
            set_u32(new_header_section, 0x9C, 0)
            patched_header = replace_variant(
                xbox_header,
                xbox_header_offsets,
                args.xbox_variant,
                bytes(new_header_section),
            )
            replacements[header_name] = patched_header

            if args.verify_directory is not None:
                verify_header = (
                    args.verify_directory / f"{header_name}.bao"
                )
                verify_memory = args.verify_directory / memory_name
                verify_header.write_bytes(bytes(new_header_section))
                verify_memory.write_bytes(new_memory_section)

            rows.append(
                {
                    "header": header_name,
                    "memory": memory_name,
                    "pc_variant": args.pc_variant,
                    "xbox_variant": args.xbox_variant,
                    "sample_rate": sample_rate,
                    "samples": len(samples),
                    "duration": len(samples) / sample_rate,
                    "ima_bytes": len(codec_payload),
                    "header_variants_before": len(
                        signature_offsets(
                            resource_blob(xbox_data, xbox_header_resource)
                        )
                    ),
                    "header_variants_after": len(
                        signature_offsets(patched_header)
                    ),
                    "memory_variants_after": len(
                        signature_offsets(patched_memory)
                    ),
                }
            )

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
            rebuilt_raw, args.decoder, temporary, "verify_embedded_ima"
        )
        if (verified / "0.dat").read_bytes() != rebuilt_directory:
            raise AssertionError("rebuilt directory differs")
        if (verified / "1.dat").read_bytes() != rebuilt_data:
            raise AssertionError("rebuilt data differs")
        area_report = replace_forge_entries(
            args.xbox_area_forge,
            args.output_area_forge,
            {args.entry_name: rebuilt_raw},
        )

    report = {
        "mode": "embedded-xma-to-ima",
        "entry": args.entry_name,
        "dialogues": rows,
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
        "area_forge_size": args.output_area_forge.stat().st_size,
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
