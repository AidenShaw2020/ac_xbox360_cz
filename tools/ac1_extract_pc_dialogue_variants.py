#!/usr/bin/env python3
"""Extract individual Ogg/WAV variants from embedded AC1 PC dialogue BAOs."""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
from pathlib import Path

from ac1_resource_bundle import parse_bundle, safe_name


BAO_SIGNATURE = b"\x01\x1B\x01\x00"
BAO_HEADER_SIZE = 0x28


def signature_offsets(blob: bytes) -> list[int]:
    return [
        offset
        for offset in range(len(blob) - len(BAO_SIGNATURE) + 1)
        if blob.startswith(BAO_SIGNATURE, offset)
    ]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--header", action="append", required=True)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    resources = parse_bundle(args.directory, args.data, "little")
    by_name = {resource.name: resource for resource in resources}
    data = args.data.read_bytes()
    args.output.mkdir(parents=True, exist_ok=True)
    report: list[dict[str, object]] = []

    for header_name in args.header:
        header_resource = by_name.get(header_name)
        if header_resource is None:
            raise ValueError(f"header resource not found: {header_name}")
        header_blob = data[
            header_resource.offset :
            header_resource.offset + header_resource.size
        ]
        header_offsets = signature_offsets(header_blob)
        if not header_offsets:
            raise ValueError(f"{header_name}: no BAO headers")
        audio_ids = [
            u32(header_blob, offset + 0x44) for offset in header_offsets
        ]
        if len(set(audio_ids)) != 1:
            raise ValueError(f"{header_name}: variants use different audio IDs")
        audio_id = audio_ids[0]
        memory_name = f"BAO_0x{audio_id:08x}"
        memory_resource = by_name.get(memory_name)
        if memory_resource is None:
            raise ValueError(f"{header_name}: missing {memory_name}")
        memory_blob = data[
            memory_resource.offset :
            memory_resource.offset + memory_resource.size
        ]
        memory_offsets = signature_offsets(memory_blob)
        if len(memory_offsets) != len(header_offsets):
            raise ValueError(
                f"{header_name}: {len(header_offsets)} headers but "
                f"{len(memory_offsets)} memory BAOs"
            )

        for variant, start in enumerate(memory_offsets, 1):
            end = (
                memory_offsets[variant]
                if variant < len(memory_offsets)
                else len(memory_blob)
            )
            payload_start = start + BAO_HEADER_SIZE
            payload = memory_blob[payload_start:end]
            stem = f"{safe_name(header_name)}_variant_{variant}"
            ogg_path = args.output / f"{stem}.ogg"
            wav_path = args.output / f"{stem}.wav"
            ogg_path.write_bytes(payload)
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
                    str(wav_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise ValueError(
                    f"{header_name} variant {variant}: "
                    f"ffmpeg failed: {completed.stderr.strip()}"
                )
            sample_rate = u32(
                header_blob, header_offsets[variant - 1] + 0x70
            )
            samples = u32(
                header_blob, header_offsets[variant - 1] + 0x78
            )
            report.append(
                {
                    "header": header_name,
                    "variant": variant,
                    "audio_id": f"{audio_id:08X}",
                    "sample_rate": sample_rate,
                    "samples": samples,
                    "duration": samples / sample_rate,
                    "ogg": str(ogg_path),
                    "wav": str(wav_path),
                }
            )

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
