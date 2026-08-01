#!/usr/bin/env python3
"""Port every matching PC Czech dialogue in one AC1 Xbox area FORGE.

The Xbox English language slot and every resource size stay unchanged.  PC
audio is decoded to PCM, encoded with the official Xbox XMA encoder and placed
inside the original Xbox XMA1 containers.  Embedded language-chain footers are
preserved byte-for-byte.
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

from ac1_build_all_mgb_archive import decode_xbox_prefix
from ac1_build_complete_text import (
    forge_entry_payload,
    replace_forge_entries,
)
from ac1_build_dub_embedded_ima import signature_offsets, u32
from ac1_build_dub_embedded_safe import resource_blob, sha256_path
from ac1_build_dub_embedded_xma_fixed import encode_xma, set_u32
from ac1_build_dub_masyaf_probe import (
    BAO_RESOURCE_TYPE,
    BAO_SIGNATURE,
    BAO_STREAM_HEADER_SIZE,
    PC_LANGUAGE_PREFIX,
    XBOX_LANGUAGE_PREFIX,
    entries_by_audio_id,
    entry_payload,
)
from ac1_build_gui_forge import wrapper_source_chunks
from ac1_build_masyaf_text import (
    CHUNK_SIZE,
    changed_chunks,
    serialize_wrapper,
    wrapper_ranges,
)
from ac1_forge import ForgeEntry, parse_forge
from ac1_resource_bundle import parse_bundle
from ac1_ubi_ima import decode_ubi_ima_mono, write_pcm16_mono_wav


RAW_SIGNATURE = bytes.fromhex("1004FA9957FBAA33")
PC_RAW_SIGNATURE = RAW_SIGNATURE[::-1]


def pc_decode(
    raw: bytes,
    *,
    quickbms: Path,
    raw_script: Path,
    temporary: Path,
    tag: str,
) -> tuple[Path, Path]:
    raw_path = temporary / f"{tag}.bin"
    output_parent = temporary / f"{tag}_decoded"
    output_parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(raw)
    completed = subprocess.run(
        [
            str(quickbms),
            "-Q",
            "-o",
            str(raw_script),
            str(raw_path),
            str(output_parent),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    decoded = output_parent / raw_path.stem
    directory = decoded / "0.dat"
    data = decoded / "1.dat"
    if (
        completed.returncode != 0
        or not directory.exists()
        or not data.exists()
    ):
        raise ValueError(
            f"{tag}: PC decode failed: "
            f"{completed.stdout.strip()} {completed.stderr.strip()}"
        )
    return directory, data


def forge_payload(path: Path, entry: ForgeEntry) -> bytes:
    with path.open("rb") as stream:
        stream.seek(entry.offset + 440)
        payload = stream.read(entry.size)
    if len(payload) != entry.size:
        raise ValueError(f"{path}: truncated {entry.name}")
    return payload


def pcm_wav(
    *,
    pc_payload: bytes,
    codec: int,
    samples: int,
    sample_rate: int,
    ffmpeg: Path,
    temporary: Path,
    tag: str,
) -> Path:
    wav_path = temporary / f"{tag}.wav"
    audio = pc_payload[BAO_STREAM_HEADER_SIZE:]
    if codec == 3:
        decoded = decode_ubi_ima_mono(
            audio, samples, allow_truncated=True
        )
        write_pcm16_mono_wav(wav_path, decoded, sample_rate)
        return wav_path
    if codec == 4:
        ogg_path = temporary / f"{tag}.ogg"
        ogg_path.write_bytes(audio)
        completed = subprocess.run(
            [
                str(ffmpeg),
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
                str(sample_rate),
                str(wav_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"{tag}: ffmpeg failed: {completed.stderr.strip()}"
            )
        return wav_path
    raise ValueError(f"{tag}: unsupported PC codec {codec}")


def encoded_xma(
    *,
    audio_id: int,
    pc_payload: bytes,
    codec: int,
    samples: int,
    sample_rate: int,
    capacity: int,
    ffmpeg: Path,
    encoder: Path,
    cache: Path,
    temporary: Path,
    quality: int,
    min_quality: int,
    quality_step: int,
    block_size: int,
) -> tuple[bytes, dict[str, int]]:
    source_hash = hashlib.sha256(pc_payload).hexdigest()
    cache_data = cache / f"{audio_id:08X}.xma"
    cache_meta = cache / f"{audio_id:08X}.json"
    if cache_data.exists() and cache_meta.exists():
        meta = json.loads(cache_meta.read_text(encoding="utf-8"))
        data = cache_data.read_bytes()
        if (
            meta.get("source_sha256") == source_hash
            and len(data) <= capacity
        ):
            return data, {
                "channels": int(meta["channels"]),
                "sample_rate": int(meta["sample_rate"]),
                "samples": int(meta["samples"]),
                "quality": int(meta["quality"]),
            }

    tag = f"{audio_id:08X}"
    try:
        wav_path = pcm_wav(
            pc_payload=pc_payload,
            codec=codec,
            samples=samples,
            sample_rate=sample_rate,
            ffmpeg=ffmpeg,
            temporary=temporary,
            tag=tag,
        )
    except ValueError as error:
        raise ValueError(f"BAO 0x{audio_id:08X}: {error}") from error
    xma_path = temporary / f"{tag}.xma"
    selected: tuple[bytes, bytes, int] | None = None
    rates = [sample_rate]
    for fallback_rate in (24000, 22050, 16000, 12000, 11025, 8000):
        if fallback_rate < sample_rate and fallback_rate not in rates:
            rates.append(fallback_rate)
    for target_rate in rates:
        encode_wav = wav_path
        if target_rate != sample_rate:
            encode_wav = temporary / f"{tag}_{target_rate}.wav"
            completed = subprocess.run(
                [
                    str(ffmpeg),
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(wav_path),
                    "-acodec",
                    "pcm_s16le",
                    "-ac",
                    "1",
                    "-ar",
                    str(target_rate),
                    str(encode_wav),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise ValueError(
                    f"{tag}: resampling failed: "
                    f"{completed.stderr.strip()}"
                )
        quality_candidates = sorted(
            {
                quality,
                50,
                40,
                30,
                20,
                15,
                10,
                5,
                min_quality,
            },
            reverse=True,
        )
        quality_candidates = [
            value
            for value in quality_candidates
            if min_quality <= value <= quality
        ]
        block_candidates = []
        for candidate in (block_size, 4, 8, 16, 32, 64):
            if candidate not in block_candidates:
                block_candidates.append(candidate)
        for current_quality in quality_candidates:
            for current_block_size in block_candidates:
                fmt, data = encode_xma(
                    encoder,
                    encode_wav,
                    xma_path,
                    current_quality,
                    current_block_size,
                )
                if len(data) <= capacity:
                    selected = fmt, data, current_quality
                    break
            if selected is not None:
                break
        if selected is not None:
            break
    if selected is None:
        raise ValueError(
            f"BAO 0x{audio_id:08X}: XMA does not fit "
            f"{capacity} bytes (rate={sample_rate}, samples={samples})"
        )
    fmt, data, selected_quality = selected
    channels = struct.unpack_from("<H", fmt, 2)[0]
    encoded_rate = struct.unpack_from("<I", fmt, 4)[0]
    encoded_samples = struct.unpack_from("<I", fmt, 24)[0]
    play_length = struct.unpack_from("<I", fmt, 36)[0]
    result_meta = {
        "channels": channels,
        "sample_rate": encoded_rate,
        "samples": play_length or encoded_samples,
        "quality": selected_quality,
    }
    cache.mkdir(parents=True, exist_ok=True)
    cache_data.write_bytes(data)
    cache_meta.write_text(
        json.dumps(
            {
                **result_meta,
                "source_sha256": source_hash,
                "bytes": len(data),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return data, result_meta


def patch_audio_header(
    blob: bytes,
    bao_offset: int,
    *,
    audio_id: int,
    xma_size: int,
    prefetch_size: int = 0,
    channels: int,
    sample_rate: int,
    samples: int,
) -> bytes:
    patched = bytearray(blob)
    set_u32(patched, bao_offset + 0x30, xma_size)
    set_u32(patched, bao_offset + 0x44, audio_id)
    set_u32(patched, bao_offset + 0x50, 0)
    set_u32(patched, bao_offset + 0x5C, 0)
    set_u32(patched, bao_offset + 0x6C, channels)
    set_u32(patched, bao_offset + 0x70, sample_rate)
    set_u32(patched, bao_offset + 0x78, samples)
    set_u32(patched, bao_offset + 0x80, samples * 2)
    set_u32(patched, bao_offset + 0x8C, 0)
    set_u32(patched, bao_offset + 0x9C, prefetch_size)
    return bytes(patched)


def rebuild_bundle(
    source_raw: bytes,
    original_data: bytes,
    patched_data: bytes,
) -> tuple[bytes, list[int]]:
    indices = changed_chunks(original_data, patched_data)
    if not indices:
        return source_raw, []
    wrappers = wrapper_source_chunks(source_raw)
    ranges = wrapper_ranges(source_raw)
    if len(wrappers) != 2 or len(ranges) != 2:
        raise ValueError("expected two XMem/LZX wrappers")
    chunks = list(wrappers[1])
    for index in indices:
        plain = patched_data[
            index * CHUNK_SIZE : (index + 1) * CHUNK_SIZE
        ]
        original = chunks[index]
        chunks[index] = (len(plain), len(plain), original[2], plain)
    directory_start, directory_end = ranges[0]
    data_start, _data_end = ranges[1]
    rebuilt = (
        source_raw[directory_start:directory_end]
        + serialize_wrapper(
            source_raw[data_start : data_start + 17], chunks
        )
    )
    return rebuilt, indices


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pc-forge", required=True, type=Path)
    parser.add_argument("--xbox-area-forge", required=True, type=Path)
    parser.add_argument("--pc-language-forge", required=True, type=Path)
    parser.add_argument("--xbox-language-forge", required=True, type=Path)
    parser.add_argument(
        "--language-output-forge", required=True, type=Path
    )
    parser.add_argument("--output-area-forge", required=True, type=Path)
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--pc-raw-script", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--xma2encode", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--only-entry", action="append")
    parser.add_argument("--pc-variant", type=int, default=1)
    parser.add_argument("--xbox-variant", type=int, default=1)
    parser.add_argument("--quality", type=int, default=60)
    parser.add_argument("--min-quality", type=int, default=5)
    parser.add_argument("--quality-step", type=int, default=5)
    parser.add_argument("--block-size", type=int, default=2)
    parser.add_argument(
        "--localized-only",
        action="store_true",
        help=(
            "convert only PC BAOs with multiple language variants and "
            "restore single-variant external effects from the clean Xbox "
            "language archive"
        ),
    )
    parser.add_argument(
        "--consume-all-xbox-variants",
        action="store_true",
        help=(
            "if a localized embedded clip does not fit its first Xbox "
            "language slot, reuse all later language slots in that BAO"
        ),
    )
    args = parser.parse_args()

    pc_archive = parse_forge(args.pc_forge)
    xbox_archive = parse_forge(args.xbox_area_forge)
    pc_entries = {entry.name: entry for entry in pc_archive.entries}
    xbox_entries = {entry.name: entry for entry in xbox_archive.entries}
    names = sorted(set(pc_entries) & set(xbox_entries))
    if args.only_entry:
        allowed = set(args.only_entry)
        names = [name for name in names if name in allowed]

    pc_language_archive = parse_forge(args.pc_language_forge)
    xbox_language_archive = parse_forge(args.xbox_language_forge)
    pc_streams = entries_by_audio_id(
        pc_language_archive, PC_LANGUAGE_PREFIX
    )
    xbox_streams = entries_by_audio_id(
        xbox_language_archive, XBOX_LANGUAGE_PREFIX
    )
    if args.language_output_forge.exists():
        language_base = args.language_output_forge
    else:
        args.language_output_forge.parent.mkdir(
            parents=True, exist_ok=True
        )
        shutil.copyfile(
            args.xbox_language_forge, args.language_output_forge
        )
        language_base = args.language_output_forge
    language_temp = args.language_output_forge.with_suffix(
        args.language_output_forge.suffix + ".tmp"
    )
    shutil.copyfile(language_base, language_temp)

    replacements: dict[str, bytes] = {}
    external_payloads: dict[int, bytes] = {}
    localized_external_ids: set[int] = set()
    restored_external_ids: set[int] = set()
    rows: list[dict[str, object]] = []
    skipped_entries: list[str] = []
    skipped_audio: list[dict[str, str]] = []
    pc_variant = args.pc_variant - 1
    xbox_variant = args.xbox_variant - 1

    with tempfile.TemporaryDirectory(
        prefix="ac1_complete_dub_"
    ) as temporary_name:
        temporary_root = Path(temporary_name)
        for number, name in enumerate(names, 1):
            pc_entry = pc_entries[name]
            xbox_entry = xbox_entries[name]
            pc_raw = forge_payload(args.pc_forge, pc_entry)
            xbox_raw = forge_payload(args.xbox_area_forge, xbox_entry)
            if (
                not pc_raw.startswith(PC_RAW_SIGNATURE)
                or not xbox_raw.startswith(RAW_SIGNATURE)
            ):
                continue
            print(f"[{number}/{len(names)}] {name}", flush=True)
            with tempfile.TemporaryDirectory(
                prefix="entry_", dir=temporary_root
            ) as entry_temp_name:
                entry_temp = Path(entry_temp_name)
                try:
                    pc_directory, pc_data_path = pc_decode(
                        pc_raw,
                        quickbms=args.quickbms,
                        raw_script=args.pc_raw_script,
                        temporary=entry_temp,
                        tag=f"pc_{pc_entry.index:05d}",
                    )
                    xbox_decoded, _rc, _text = decode_xbox_prefix(
                        xbox_raw,
                        args.decoder,
                        entry_temp,
                        f"xbox_{xbox_entry.index:05d}",
                    )
                    xbox_directory = xbox_decoded / "0.dat"
                    xbox_data_path = xbox_decoded / "1.dat"
                    pc_data = pc_data_path.read_bytes()
                    xbox_data_original = xbox_data_path.read_bytes()
                    xbox_data = bytearray(xbox_data_original)
                    try:
                        pc_parsed_resources = parse_bundle(
                            pc_directory, pc_data_path, "little"
                        )
                    except ValueError as error:
                        raise ValueError(f"PC bundle: {error}") from error
                    try:
                        xbox_parsed_resources = parse_bundle(
                            xbox_directory,
                            xbox_data_path,
                            "big",
                            allow_truncated=True,
                        )
                    except ValueError as error:
                        raise ValueError(f"Xbox bundle: {error}") from error
                    pc_resources = {
                        resource.name: resource
                        for resource in pc_parsed_resources
                        if resource.type_id == BAO_RESOURCE_TYPE
                    }
                    xbox_resources = {
                        resource.name: resource
                        for resource in xbox_parsed_resources
                        if resource.type_id == BAO_RESOURCE_TYPE
                    }
                except ValueError as error:
                    skipped_entries.append(name)
                    print(f"  skipped {name}: {error}", flush=True)
                    continue

                for header_name in sorted(
                    set(pc_resources) & set(xbox_resources)
                ):
                    if not header_name.startswith("BAO_0x2"):
                        continue
                    pc_header_resource = pc_resources[header_name]
                    xbox_header_resource = xbox_resources[header_name]
                    pc_header = resource_blob(
                        pc_data, pc_header_resource
                    )
                    xbox_header = resource_blob(
                        xbox_data_original, xbox_header_resource
                    )
                    pc_offsets = signature_offsets(pc_header)
                    xbox_offsets = signature_offsets(xbox_header)
                    if (
                        pc_variant >= len(pc_offsets)
                        or xbox_variant >= len(xbox_offsets)
                    ):
                        continue
                    pc_bao = pc_offsets[pc_variant]
                    xbox_bao = xbox_offsets[xbox_variant]
                    if (
                        pc_bao + 0xA4 > len(pc_header)
                        or xbox_bao + 0xA4 > len(xbox_header)
                    ):
                        continue
                    if (
                        u32(pc_header, pc_bao + 0x20, "little")
                        >> 28
                        != 2
                        or u32(xbox_header, xbox_bao + 0x20, "big")
                        >> 28
                        != 2
                    ):
                        continue
                    audio_id = u32(
                        pc_header, pc_bao + 0x44, "little"
                    )
                    if (
                        u32(xbox_header, xbox_bao + 0x44, "big")
                        != audio_id
                    ):
                        continue
                    codec = u32(
                        pc_header, pc_bao + 0x8C, "little"
                    )
                    if codec not in (3, 4):
                        continue
                    if args.localized_only and len(pc_offsets) < 2:
                        if (
                            audio_id in xbox_streams
                            and audio_id not in localized_external_ids
                        ):
                            external_payloads[audio_id] = entry_payload(
                                args.xbox_language_forge,
                                xbox_streams[audio_id],
                            )
                            restored_external_ids.add(audio_id)
                        continue
                    source_rate = u32(
                        pc_header, pc_bao + 0x70, "little"
                    )
                    source_samples = u32(
                        pc_header, pc_bao + 0x78, "little"
                    )

                    mode: str
                    consumed_all_variants = False
                    can_consume_all_variants = False
                    footer = b""
                    section_size = 0
                    xbox_prefetch_memory: tuple[
                        object, bytes, int, int, int, bytes
                    ] | None = None
                    if audio_id in pc_streams and audio_id in xbox_streams:
                        mode = "external"
                        pc_payload = entry_payload(
                            args.pc_language_forge,
                            pc_streams[audio_id],
                        )
                        xbox_payload = entry_payload(
                            args.xbox_language_forge,
                            xbox_streams[audio_id],
                        )
                        external_capacity = (
                            len(xbox_payload) - BAO_STREAM_HEADER_SIZE
                        )
                        pc_prefetch_size = u32(
                            pc_header, pc_bao + 0x9C, "little"
                        )
                        xbox_prefetch_size = u32(
                            xbox_header, xbox_bao + 0x9C, "big"
                        )
                        memory_capacity = 0
                        if pc_prefetch_size or xbox_prefetch_size:
                            memory_id = (
                                (audio_id & 0x0FFFFFFF) | 0x30000000
                            )
                            memory_name = f"BAO_0x{memory_id:08x}"
                            if (
                                memory_name not in pc_resources
                                or memory_name not in xbox_resources
                            ):
                                skipped_audio.append(
                                    {
                                        "entry": name,
                                        "header": header_name,
                                        "audio_id": f"{audio_id:08X}",
                                        "reason": (
                                            "prefetch memory BAO "
                                            f"{memory_name} not found"
                                        ),
                                    }
                                )
                                continue
                            pc_memory_resource = pc_resources[memory_name]
                            xbox_memory_resource = xbox_resources[
                                memory_name
                            ]
                            pc_memory = resource_blob(
                                pc_data, pc_memory_resource
                            )
                            xbox_memory = resource_blob(
                                xbox_data_original, xbox_memory_resource
                            )
                            pc_memory_offsets = signature_offsets(
                                pc_memory
                            )
                            xbox_memory_offsets = signature_offsets(
                                xbox_memory
                            )
                            if (
                                pc_variant >= len(pc_memory_offsets)
                                or xbox_variant
                                >= len(xbox_memory_offsets)
                            ):
                                skipped_audio.append(
                                    {
                                        "entry": name,
                                        "header": header_name,
                                        "audio_id": f"{audio_id:08X}",
                                        "reason": (
                                            "prefetch language variant "
                                            "not found"
                                        ),
                                    }
                                )
                                continue
                            pc_start = pc_memory_offsets[pc_variant]
                            pc_end = (
                                pc_memory_offsets[pc_variant + 1]
                                if pc_variant + 1
                                < len(pc_memory_offsets)
                                else len(pc_memory)
                            )
                            pc_memory_payload = pc_memory[pc_start:pc_end]
                            if (
                                len(pc_memory_payload)
                                < BAO_STREAM_HEADER_SIZE
                            ):
                                continue
                            pc_payload = (
                                pc_memory_payload[
                                    :BAO_STREAM_HEADER_SIZE
                                ]
                                + pc_memory_payload[
                                    BAO_STREAM_HEADER_SIZE:
                                ]
                                + pc_payload[BAO_STREAM_HEADER_SIZE:]
                            )

                            memory_start = xbox_memory_offsets[
                                xbox_variant
                            ]
                            memory_end = (
                                xbox_memory_offsets[xbox_variant + 1]
                                if xbox_variant + 1
                                < len(xbox_memory_offsets)
                                else len(xbox_memory)
                            )
                            section_size = memory_end - memory_start
                            footer_size = (
                                8
                                if xbox_variant + 1
                                < len(xbox_memory_offsets)
                                else 0
                            )
                            footer = (
                                xbox_memory[
                                    memory_end - footer_size : memory_end
                                ]
                                if footer_size
                                else b""
                            )
                            memory_capacity = (
                                section_size
                                - BAO_STREAM_HEADER_SIZE
                                - footer_size
                            )
                            if memory_capacity < 0:
                                continue
                            xbox_prefetch_memory = (
                                xbox_memory_resource,
                                xbox_memory,
                                memory_start,
                                memory_end,
                                memory_capacity,
                                footer,
                            )
                            mode = "external_prefetch"
                        capacity = memory_capacity + external_capacity
                        if capacity <= 0:
                            skipped_audio.append(
                                {
                                    "entry": name,
                                    "header": header_name,
                                    "audio_id": f"{audio_id:08X}",
                                    "reason": "empty Xbox stream slot",
                                }
                            )
                            continue
                    else:
                        mode = "embedded"
                        memory_name = f"BAO_0x{audio_id:08x}"
                        if (
                            memory_name not in pc_resources
                            or memory_name not in xbox_resources
                        ):
                            continue
                        pc_memory_resource = pc_resources[memory_name]
                        xbox_memory_resource = xbox_resources[memory_name]
                        pc_memory = resource_blob(
                            pc_data, pc_memory_resource
                        )
                        xbox_memory = resource_blob(
                            xbox_data_original, xbox_memory_resource
                        )
                        pc_memory_offsets = signature_offsets(pc_memory)
                        xbox_memory_offsets = signature_offsets(
                            xbox_memory
                        )
                        if (
                            pc_variant >= len(pc_memory_offsets)
                            or xbox_variant >= len(xbox_memory_offsets)
                        ):
                            continue
                        pc_start = pc_memory_offsets[pc_variant]
                        pc_end = (
                            pc_memory_offsets[pc_variant + 1]
                            if pc_variant + 1
                            < len(pc_memory_offsets)
                            else len(pc_memory)
                        )
                        pc_payload = pc_memory[pc_start:pc_end]
                        memory_start = xbox_memory_offsets[xbox_variant]
                        memory_end = (
                            xbox_memory_offsets[xbox_variant + 1]
                            if xbox_variant + 1
                            < len(xbox_memory_offsets)
                            else len(xbox_memory)
                        )
                        section_size = memory_end - memory_start
                        footer_size = (
                            8
                            if xbox_variant + 1
                            < len(xbox_memory_offsets)
                            else 0
                        )
                        footer = (
                            xbox_memory[
                                memory_end - footer_size : memory_end
                            ]
                            if footer_size
                            else b""
                        )
                        capacity = (
                            section_size
                            - BAO_STREAM_HEADER_SIZE
                            - footer_size
                        )
                        can_consume_all_variants = (
                            args.consume_all_xbox_variants
                            and xbox_variant == 0
                            and len(xbox_memory_offsets) > 1
                        )

                    try:
                        xma_data, meta = encoded_xma(
                            audio_id=audio_id,
                            pc_payload=pc_payload,
                            codec=codec,
                            samples=source_samples,
                            sample_rate=source_rate,
                            capacity=capacity,
                            ffmpeg=args.ffmpeg,
                            encoder=args.xma2encode,
                            cache=args.cache_dir,
                            temporary=entry_temp,
                            quality=args.quality,
                            min_quality=args.min_quality,
                            quality_step=args.quality_step,
                            block_size=args.block_size,
                        )
                    except ValueError as error:
                        if can_consume_all_variants:
                            memory_end = len(xbox_memory)
                            section_size = memory_end - memory_start
                            footer = b""
                            capacity = (
                                section_size - BAO_STREAM_HEADER_SIZE
                            )
                            try:
                                xma_data, meta = encoded_xma(
                                    audio_id=audio_id,
                                    pc_payload=pc_payload,
                                    codec=codec,
                                    samples=source_samples,
                                    sample_rate=source_rate,
                                    capacity=capacity,
                                    ffmpeg=args.ffmpeg,
                                    encoder=args.xma2encode,
                                    cache=args.cache_dir,
                                    temporary=entry_temp,
                                    quality=args.quality,
                                    min_quality=args.min_quality,
                                    quality_step=args.quality_step,
                                    block_size=args.block_size,
                                )
                                consumed_all_variants = True
                            except ValueError as expanded_error:
                                error = expanded_error
                        if consumed_all_variants:
                            pass
                        else:
                            skipped_audio.append(
                                {
                                    "entry": name,
                                    "header": header_name,
                                    "audio_id": f"{audio_id:08X}",
                                    "reason": str(error),
                                }
                            )
                            continue
                    patched_header = patch_audio_header(
                        xbox_header,
                        xbox_bao,
                        audio_id=audio_id,
                        xma_size=len(xma_data),
                        prefetch_size=(
                            min(
                                len(xma_data),
                                xbox_prefetch_memory[4],
                            )
                            if xbox_prefetch_memory is not None
                            else 0
                        ),
                        channels=meta["channels"],
                        sample_rate=meta["sample_rate"],
                        samples=meta["samples"],
                    )
                    xbox_data[
                        xbox_header_resource.offset :
                        xbox_header_resource.offset
                        + xbox_header_resource.size
                    ] = patched_header

                    if mode in ("external", "external_prefetch"):
                        if xbox_prefetch_memory is None:
                            stream_xma = xma_data
                            stream_capacity = capacity
                        else:
                            (
                                xbox_memory_resource,
                                xbox_memory,
                                memory_start,
                                memory_end,
                                memory_capacity,
                                footer,
                            ) = xbox_prefetch_memory
                            prefetch_xma = xma_data[:memory_capacity]
                            stream_xma = xma_data[memory_capacity:]
                            stream_capacity = (
                                len(xbox_payload)
                                - BAO_STREAM_HEADER_SIZE
                            )
                            memory_header = xbox_memory[
                                memory_start :
                                memory_start + BAO_STREAM_HEADER_SIZE
                            ]
                            new_section = (
                                memory_header
                                + prefetch_xma
                                + b"\0"
                                * (memory_capacity - len(prefetch_xma))
                                + footer
                            )
                            if len(new_section) != (
                                memory_end - memory_start
                            ):
                                raise AssertionError(
                                    f"{header_name}: prefetch memory "
                                    "size changed"
                                )
                            patched_memory = bytearray(xbox_memory)
                            patched_memory[
                                memory_start:memory_end
                            ] = new_section
                            xbox_data[
                                xbox_memory_resource.offset :
                                xbox_memory_resource.offset
                                + xbox_memory_resource.size
                            ] = patched_memory
                        external_payloads[audio_id] = (
                            xbox_payload[:BAO_STREAM_HEADER_SIZE]
                            + stream_xma
                            + b"\0"
                            * (stream_capacity - len(stream_xma))
                        )
                        localized_external_ids.add(audio_id)
                        restored_external_ids.discard(audio_id)
                    else:
                        memory_header = xbox_memory[
                            memory_start :
                            memory_start + BAO_STREAM_HEADER_SIZE
                        ]
                        new_section = (
                            memory_header
                            + xma_data
                            + b"\0" * (capacity - len(xma_data))
                            + footer
                        )
                        if len(new_section) != section_size:
                            raise AssertionError(
                                f"{header_name}: memory size changed"
                            )
                        patched_memory = bytearray(xbox_memory)
                        patched_memory[
                            memory_start:memory_end
                        ] = new_section
                        xbox_data[
                            xbox_memory_resource.offset :
                            xbox_memory_resource.offset
                            + xbox_memory_resource.size
                        ] = patched_memory

                    rows.append(
                        {
                            "entry": name,
                            "header": header_name,
                            "audio_id": f"{audio_id:08X}",
                            "mode": mode,
                            "pc_codec": codec,
                            "source_rate": source_rate,
                            "source_samples": source_samples,
                            "xma_rate": meta["sample_rate"],
                            "xma_samples": meta["samples"],
                            "quality": meta["quality"],
                            "xma_bytes": len(xma_data),
                            "capacity": capacity,
                            "padding": capacity - len(xma_data),
                            "footer_hex": footer.hex().upper(),
                            "consumed_all_xbox_variants": (
                                consumed_all_variants
                            ),
                        }
                    )

                patched_data = bytes(xbox_data)
                rebuilt, indices = rebuild_bundle(
                    xbox_raw, xbox_data_original, patched_data
                )
                if indices:
                    verified, _verify_rc, _verify_text = (
                        decode_xbox_prefix(
                            rebuilt,
                            args.decoder,
                            entry_temp,
                            f"verify_{xbox_entry.index:05d}",
                        )
                    )
                    if (
                        (verified / "0.dat").read_bytes()
                        != xbox_directory.read_bytes()
                        or (verified / "1.dat").read_bytes()
                        != patched_data
                    ):
                        raise AssertionError(
                            f"{name}: rebuilt bundle verification failed"
                        )
                    replacements[name] = rebuilt

        with language_temp.open("r+b") as language_stream:
            for audio_id, payload in external_payloads.items():
                entry = xbox_streams[audio_id]
                if len(payload) != entry.size:
                    raise AssertionError(
                        f"BAO 0x{audio_id:08X}: stream size changed"
                    )
                language_stream.seek(entry.offset + 440)
                language_stream.write(payload)
        language_temp.replace(args.language_output_forge)

        if replacements:
            replace_report = replace_forge_entries(
                args.xbox_area_forge,
                args.output_area_forge,
                replacements,
            )
        else:
            args.output_area_forge.parent.mkdir(
                parents=True, exist_ok=True
            )
            shutil.copyfile(
                args.xbox_area_forge, args.output_area_forge
            )
            replace_report = {}

    mode_counts: dict[str, int] = {}
    for row in rows:
        mode = str(row["mode"])
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
    report = {
        "pc_forge": str(args.pc_forge),
        "xbox_source": str(args.xbox_area_forge),
        "matching_entries": len(names),
        "modified_entries": len(replacements),
        "dialogues": len(rows),
        "mode_counts": mode_counts,
        "external_streams": len(external_payloads),
        "localized_external_streams": len(localized_external_ids),
        "restored_nonlocalized_external_streams": len(
            restored_external_ids
        ),
        "skipped_entries": skipped_entries,
        "skipped_audio": skipped_audio,
        "replace_report": replace_report,
        "first_dialogues": rows[:50],
        "area_size": args.output_area_forge.stat().st_size,
        "area_sha256": sha256_path(args.output_area_forge),
        "language_size": args.language_output_forge.stat().st_size,
        "language_sha256": sha256_path(
            args.language_output_forge
        ),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
