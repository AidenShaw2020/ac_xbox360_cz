#!/usr/bin/env python3
"""Build a standalone AC1 XMA1 BAO pair from an SDK-encoded XMA2 RIFF."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path


BAO_SIGNATURE = b"\x01\x1B\x01\x00"
BAO_HEADER_SIZE = 0x28


def signatures(data: bytes) -> list[int]:
    return [
        offset
        for offset in range(len(data) - 3)
        if data.startswith(BAO_SIGNATURE, offset)
    ]


def riff_chunks(data: bytes) -> dict[bytes, bytes]:
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF WAVE file")
    chunks: dict[bytes, bytes] = {}
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        start = offset + 8
        end = start + size
        if end > len(data):
            raise ValueError(f"truncated RIFF chunk {chunk_id!r}")
        chunks[chunk_id] = data[start:end]
        offset = end + (size & 1)
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--header", required=True, type=Path)
    parser.add_argument("--memory", required=True, type=Path)
    parser.add_argument("--xma2", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    header = args.header.read_bytes()
    memory = args.memory.read_bytes()
    header_offsets = signatures(header)
    memory_offsets = signatures(memory)
    if not header_offsets or not memory_offsets:
        raise ValueError("BAO signature missing")
    header_start = header_offsets[0]
    header_end = (
        header_offsets[1] if len(header_offsets) > 1 else len(header)
    )
    memory_start = memory_offsets[0]
    header_section = bytearray(header[header_start:header_end])
    memory_header = memory[
        memory_start : memory_start + BAO_HEADER_SIZE
    ]

    chunks = riff_chunks(args.xma2.read_bytes())
    fmt = chunks.get(b"fmt ")
    xma_data = chunks.get(b"data")
    if fmt is None or xma_data is None or len(fmt) < 0x34:
        raise ValueError("XMA2 fmt/data chunk missing")
    if struct.unpack_from("<H", fmt, 0)[0] != 0x0166:
        raise ValueError("input is not XMA2")
    channels = struct.unpack_from("<H", fmt, 2)[0]
    sample_rate = struct.unpack_from("<I", fmt, 4)[0]
    avg_bps = struct.unpack_from("<I", fmt, 8)[0]
    samples = struct.unpack_from("<I", fmt, 24)[0]

    struct.pack_into(">I", header_section, 0x30, len(xma_data))
    struct.pack_into(">I", header_section, 0x6C, channels)
    struct.pack_into(">I", header_section, 0x70, sample_rate)
    struct.pack_into(">I", header_section, 0x78, samples)
    struct.pack_into(">I", header_section, 0x80, samples * 2)
    struct.pack_into(">I", header_section, 0x8C, 0)
    # AC1's first 0x20-byte codec extra is an XMA1 WAVEFORMAT structure.
    struct.pack_into(">I", header_section, 0xB0, avg_bps)
    struct.pack_into(">I", header_section, 0xB4, sample_rate)
    struct.pack_into(">I", header_section, 0xB8, 0)
    struct.pack_into(">I", header_section, 0xBC, 0)

    audio_id = struct.unpack_from(">I", header_section, 0x44)[0]
    args.output.mkdir(parents=True, exist_ok=True)
    header_path = args.output / "probe.bao"
    memory_path = args.output / f"BAO_0x{audio_id:08x}"
    header_path.write_bytes(header_section)
    memory_path.write_bytes(memory_header + xma_data)
    print(
        json.dumps(
            {
                "header": str(header_path),
                "memory": str(memory_path),
                "audio_id": f"{audio_id:08X}",
                "channels": channels,
                "sample_rate": sample_rate,
                "samples": samples,
                "avg_bps": avg_bps,
                "xma_bytes": len(xma_data),
                "header_bytes": len(header_section),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
