#!/usr/bin/env python3
"""Replace embedded AC1 Xbox XMA dialogue without changing bundle sizes."""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import tempfile
from pathlib import Path

from ac1_build_all_mgb_archive import decode_xbox_prefix
from ac1_build_complete_text import forge_entry_payload, replace_forge_entries
from ac1_build_dub_embedded_safe import resource_blob, sha256_path
from ac1_build_dub_embedded_ima import signature_offsets, u32
from ac1_build_dub_masyaf_probe import BAO_STREAM_HEADER_SIZE
from ac1_build_gui_forge import wrapper_source_chunks
from ac1_build_masyaf_text import (
    CHUNK_SIZE,
    changed_chunks,
    serialize_wrapper,
    wrapper_ranges,
)
from ac1_probe_xma2_as_xma1 import riff_chunks
from ac1_resource_bundle import parse_bundle


def set_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into(">I", data, offset, value)


def encode_xma(
    encoder: Path,
    wav_path: Path,
    output_path: Path,
    quality: int,
    block_size: int,
) -> tuple[bytes, bytes]:
    if output_path.exists():
        output_path.unlink()
    completed = subprocess.run(
        [
            str(encoder),
            str(wav_path),
            "/TargetFile",
            str(output_path),
            "/UseLoopPoints",
            "/BlockSize",
            str(block_size),
            "/Quality",
            str(quality),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            f"xma2encode failed: "
            f"{completed.stdout.strip()} {completed.stderr.strip()}"
        )
    chunks = riff_chunks(output_path.read_bytes())
    fmt = chunks.get(b"fmt ")
    xma_data = chunks.get(b"data")
    if fmt is None or xma_data is None or len(fmt) < 0x34:
        raise ValueError("encoded XMA2 fmt/data chunk missing")
    if struct.unpack_from("<H", fmt, 0)[0] != 0x0166:
        raise ValueError("encoder did not produce XMA2")
    return fmt, xma_data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pc-directory", required=True, type=Path)
    parser.add_argument("--pc-data", required=True, type=Path)
    parser.add_argument("--xbox-area-forge", required=True, type=Path)
    parser.add_argument("--entry-name", required=True)
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--xma2encode", required=True, type=Path)
    parser.add_argument("--output-area-forge", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--include-resource", action="append", required=True
    )
    parser.add_argument("--pc-variant", type=int, default=2)
    parser.add_argument("--xbox-variant", type=int, default=1)
    parser.add_argument("--quality", type=int, default=60)
    parser.add_argument("--min-quality", type=int, default=20)
    parser.add_argument("--quality-step", type=int, default=5)
    parser.add_argument("--block-size", type=int, default=64)
    parser.add_argument("--verify-directory", type=Path)
    args = parser.parse_args()

    source_raw = forge_entry_payload(args.xbox_area_forge, args.entry_name)
    with tempfile.TemporaryDirectory(
        prefix="ac1_dub_embedded_xma_"
    ) as temporary_name:
        temporary = Path(temporary_name)
        decoded, decoder_returncode, decoder_output = decode_xbox_prefix(
            source_raw, args.decoder, temporary, "xbox_embedded_xma"
        )
        xbox_directory_path = decoded / "0.dat"
        xbox_data_path = decoded / "1.dat"
        xbox_directory = xbox_directory_path.read_bytes()
        xbox_data_original = xbox_data_path.read_bytes()
        xbox_data = bytearray(xbox_data_original)
        pc_data = args.pc_data.read_bytes()

        pc_resources = {
            resource.name: resource
            for resource in parse_bundle(
                args.pc_directory, args.pc_data, "little"
            )
        }
        xbox_resources = {
            resource.name: resource
            for resource in parse_bundle(
                xbox_directory_path, xbox_data_path, "big"
            )
        }
        if args.verify_directory is not None:
            args.verify_directory.mkdir(parents=True, exist_ok=True)

        pc_index = args.pc_variant - 1
        xbox_index = args.xbox_variant - 1
        rows: list[dict[str, object]] = []
        for header_name in args.include_resource:
            pc_header_resource = pc_resources.get(header_name)
            xbox_header_resource = xbox_resources.get(header_name)
            if pc_header_resource is None or xbox_header_resource is None:
                raise ValueError(f"{header_name}: resource missing")
            pc_header = resource_blob(pc_data, pc_header_resource)
            xbox_header = resource_blob(
                xbox_data_original, xbox_header_resource
            )
            pc_header_offsets = signature_offsets(pc_header)
            xbox_header_offsets = signature_offsets(xbox_header)
            if pc_index not in range(len(pc_header_offsets)):
                raise ValueError(f"{header_name}: PC variant missing")
            if xbox_index not in range(len(xbox_header_offsets)):
                raise ValueError(f"{header_name}: Xbox variant missing")
            pc_header_bao = pc_header_offsets[pc_index]
            xbox_header_bao = xbox_header_offsets[xbox_index]
            audio_id = u32(pc_header, pc_header_bao + 0x44, "little")
            if u32(xbox_header, xbox_header_bao + 0x44, "big") != audio_id:
                raise ValueError(f"{header_name}: audio ID differs")

            memory_name = f"BAO_0x{audio_id:08x}"
            pc_memory_resource = pc_resources.get(memory_name)
            xbox_memory_resource = xbox_resources.get(memory_name)
            if pc_memory_resource is None or xbox_memory_resource is None:
                raise ValueError(f"{header_name}: {memory_name} missing")
            pc_memory = resource_blob(pc_data, pc_memory_resource)
            xbox_memory = resource_blob(
                xbox_data_original, xbox_memory_resource
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
            source_rate = u32(
                pc_header, pc_header_bao + 0x70, "little"
            )
            ogg_path = temporary / f"{header_name}.ogg"
            wav_path = temporary / f"{header_name}.wav"
            xma_path = temporary / f"{header_name}.xma"
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
                    "-acodec",
                    "pcm_s16le",
                    "-ac",
                    "1",
                    "-ar",
                    str(source_rate),
                    str(wav_path),
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

            memory_start = xbox_memory_offsets[xbox_index]
            memory_end = (
                xbox_memory_offsets[xbox_index + 1]
                if xbox_index + 1 < len(xbox_memory_offsets)
                else len(xbox_memory)
            )
            section_size = memory_end - memory_start
            footer_size = (
                8 if xbox_index + 1 < len(xbox_memory_offsets) else 0
            )
            capacity = (
                section_size - BAO_STREAM_HEADER_SIZE - footer_size
            )
            selected: tuple[bytes, bytes, int] | None = None
            for quality in range(
                args.quality,
                args.min_quality - 1,
                -args.quality_step,
            ):
                fmt, xma_data = encode_xma(
                    args.xma2encode,
                    wav_path,
                    xma_path,
                    quality,
                    args.block_size,
                )
                if len(xma_data) <= capacity:
                    selected = fmt, xma_data, quality
                    break
            if selected is None:
                raise ValueError(
                    f"{header_name}: XMA cannot fit {capacity} bytes"
                )
            fmt, xma_data, quality = selected
            channels = struct.unpack_from("<H", fmt, 2)[0]
            sample_rate = struct.unpack_from("<I", fmt, 4)[0]
            encoded_samples = struct.unpack_from("<I", fmt, 24)[0]
            play_length = struct.unpack_from("<I", fmt, 36)[0]
            samples = play_length or encoded_samples

            patched_header = bytearray(xbox_header)
            base = xbox_header_bao
            set_u32(patched_header, base + 0x30, len(xma_data))
            set_u32(patched_header, base + 0x44, audio_id)
            set_u32(patched_header, base + 0x50, 0)
            set_u32(patched_header, base + 0x5C, 0)
            set_u32(patched_header, base + 0x6C, channels)
            set_u32(patched_header, base + 0x70, sample_rate)
            set_u32(patched_header, base + 0x78, samples)
            set_u32(patched_header, base + 0x80, samples * 2)
            set_u32(patched_header, base + 0x8C, 0)
            set_u32(patched_header, base + 0x9C, 0)

            memory_header = xbox_memory[
                memory_start :
                memory_start + BAO_STREAM_HEADER_SIZE
            ]
            footer = (
                xbox_memory[memory_end - footer_size : memory_end]
                if footer_size
                else b""
            )
            padding = capacity - len(xma_data)
            new_memory_section = (
                memory_header + xma_data + b"\0" * padding + footer
            )
            if len(new_memory_section) != section_size:
                raise AssertionError("memory section size changed")
            patched_memory = bytearray(xbox_memory)
            patched_memory[memory_start:memory_end] = new_memory_section
            if len(patched_header) != len(xbox_header):
                raise AssertionError("header resource size changed")
            if len(patched_memory) != len(xbox_memory):
                raise AssertionError("memory resource size changed")

            xbox_data[
                xbox_header_resource.offset :
                xbox_header_resource.offset + xbox_header_resource.size
            ] = patched_header
            xbox_data[
                xbox_memory_resource.offset :
                xbox_memory_resource.offset + xbox_memory_resource.size
            ] = patched_memory

            if args.verify_directory is not None:
                header_end = (
                    xbox_header_offsets[xbox_index + 1]
                    if xbox_index + 1 < len(xbox_header_offsets)
                    else len(xbox_header)
                )
                (
                    args.verify_directory / f"{header_name}.bao"
                ).write_bytes(
                    patched_header[xbox_header_bao:header_end]
                )
                (
                    args.verify_directory / memory_name
                ).write_bytes(new_memory_section)

            rows.append(
                {
                    "header": header_name,
                    "memory": memory_name,
                    "quality": quality,
                    "channels": channels,
                    "sample_rate": sample_rate,
                    "samples": samples,
                    "duration": samples / sample_rate,
                    "xma_bytes": len(xma_data),
                    "capacity": capacity,
                    "padding": padding,
                    "footer_size": footer_size,
                    "footer_hex": footer.hex().upper(),
                    "header_size_unchanged": len(patched_header),
                    "memory_size_unchanged": len(patched_memory),
                }
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
            rebuilt_raw, args.decoder, temporary, "verify_embedded_xma"
        )
        if (verified / "0.dat").read_bytes() != xbox_directory:
            raise AssertionError("directory decode differs")
        if (verified / "1.dat").read_bytes() != patched_data:
            raise AssertionError("data decode differs")
        area_report = replace_forge_entries(
            args.xbox_area_forge,
            args.output_area_forge,
            {args.entry_name: rebuilt_raw},
        )

    report = {
        "mode": "embedded-xma-fixed-size",
        "entry": args.entry_name,
        "dialogues": rows,
        "changed_data_chunks": modified_indices,
        "decoded_data_before": len(xbox_data_original),
        "decoded_data_after": len(patched_data),
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
