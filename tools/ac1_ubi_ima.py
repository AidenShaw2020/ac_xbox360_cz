#!/usr/bin/env python3
"""Encode mono PCM16 samples into the Ubisoft IMA form used by AC1 BAOs."""

from __future__ import annotations

import argparse
import struct
import wave
from pathlib import Path


STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130,
    143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449,
    494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411,
    1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660,
    4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493,
    10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385,
    24623, 27086, 29794, 32767,
)
INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8) * 2


def expand(code: int, predictor: int, index: int) -> tuple[int, int]:
    step = STEP_TABLE[index]
    delta = (((code & 7) * 2 + 1) * step) >> 3
    if code & 8:
        delta = -delta
    predictor = max(-32768, min(32767, predictor + delta))
    index = max(0, min(88, index + INDEX_TABLE[code]))
    return predictor, index


def choose_code(
    target: int, predictor: int, index: int
) -> tuple[int, int, int]:
    best = min(
        range(16),
        key=lambda code: abs(expand(code, predictor, index)[0] - target),
    )
    new_predictor, new_index = expand(best, predictor, index)
    return best, new_predictor, new_index


def choose_initial_index(samples: list[int], predictor: int) -> int:
    probe = samples[: min(len(samples), 1000)]
    best_index = 0
    best_error: int | None = None
    for initial in range(89):
        current = predictor
        index = initial
        error = 0
        for target in probe:
            _code, current, index = choose_code(target, current, index)
            difference = current - target
            error += difference * difference
        if best_error is None or error < best_error:
            best_error = error
            best_index = initial
    return best_index


def encode_ubi_ima_mono(
    samples: list[int], template_header: bytes
) -> bytes:
    if not samples:
        samples = [0]
    while len(samples) < 10:
        samples.append(samples[-1])
    header = bytearray(template_header[:0x1C])
    if len(header) < 0x1C:
        header.extend(b"\0" * (0x1C - len(header)))
    header[0] = 5
    struct.pack_into("<H", header, 0x0E, 10)
    predictor = samples[9]
    initial_index = choose_initial_index(samples[10:], predictor)
    struct.pack_into("<h", header, 0x10, predictor)
    header[0x12] = initial_index
    header[0x13:0x1C] = b"\0" * 9

    output = bytearray(header)
    output += struct.pack("<10h", *samples[:10])
    index = initial_index
    pending: int | None = None
    for target in samples[10:]:
        code, predictor, index = choose_code(target, predictor, index)
        if pending is None:
            pending = code << 4
        else:
            output.append(pending | code)
            pending = None
    if pending is not None:
        output.append(pending)
    return bytes(output)


def decode_ubi_ima_mono(
    payload: bytes,
    sample_count: int,
    *,
    allow_truncated: bool = False,
) -> list[int]:
    """Decode the mono Ubisoft IMA stream used by the PC AC1 BAOs."""

    if sample_count <= 0:
        return []
    if len(payload) < 0x30:
        raise ValueError("truncated Ubisoft IMA payload")
    version = payload[0]
    if version == 0:
        raise ValueError("unsupported headerless Ubisoft IMA payload")
    header = 2
    header_samples = struct.unpack_from("<h", payload, header + 0x0C)[0]
    if header_samples < 0 or header_samples > 0x100:
        raise ValueError(
            f"invalid Ubisoft IMA header sample count {header_samples}"
        )
    predictor = struct.unpack_from("<h", payload, header + 0x0E)[0]
    index = payload[header + 0x10]
    if index > 88:
        raise ValueError(f"invalid Ubisoft IMA step index {index}")

    pcm_offset = header + 0x16
    if version >= 3:
        pcm_offset += 4
    pcm_bytes = header_samples * 2
    if pcm_offset + pcm_bytes > len(payload):
        raise ValueError("truncated Ubisoft IMA initial PCM samples")
    initial = list(
        struct.unpack_from(
            f"<{header_samples}h", payload, pcm_offset
        )
    )
    samples = initial[:sample_count]
    data = payload[pcm_offset + pcm_bytes :]
    needed = sample_count - len(samples)
    for byte in data:
        if needed <= 0:
            break
        for shift in (4, 0):
            predictor, index = expand(
                (byte >> shift) & 0x0F, predictor, index
            )
            samples.append(predictor)
            needed -= 1
            if needed <= 0:
                break
    if len(samples) != sample_count and not allow_truncated:
        raise ValueError(
            "truncated Ubisoft IMA samples: "
            f"decoded {len(samples)}, expected {sample_count}"
        )
    return samples


def write_pcm16_mono_wav(
    path: Path, samples: list[int], sample_rate: int
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--template-offset", type=lambda x: int(x, 0), default=40)
    args = parser.parse_args()
    with wave.open(str(args.wav), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("input WAV must be mono PCM16")
        samples = list(
            struct.unpack(
                f"<{source.getnframes()}h",
                source.readframes(source.getnframes()),
            )
        )
    template = args.template.read_bytes()
    encoded = encode_ubi_ima_mono(
        samples,
        template[
            args.template_offset : args.template_offset + 0x1C
        ],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoded)


if __name__ == "__main__":
    main()
