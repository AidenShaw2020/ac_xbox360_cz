#!/usr/bin/env python3
"""Build an Xbox 360 AC1 Data360.forge with the Czech PC GUI locale.

The early Scimitar MGB resources contain one self-contained MAGMA object per
language.  Most PC Czech and Xbox 360 resources have byte-identical locale
object lengths.  For those resources this tool replaces only the first
(English) Xbox locale object with the first (Czech) PC locale object, while
leaving the Xbox resource header and the other four console languages intact.

The rebuilt raw streams use uncompressed 32 KiB chunks.  The game format
explicitly supports stored chunks when compressed_size == uncompressed_size.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ac1_forge import parse_forge
from ac1_resource_bundle import parse_bundle


RAW_SIGNATURE = bytes.fromhex("1004FA9957FBAA33")
MAGMA = b"MAGMA"
MGB_TYPE = 0x1823A912


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def magma_offsets(data: bytes) -> list[int]:
    result: list[int] = []
    cursor = 0
    while True:
        cursor = data.find(MAGMA, cursor)
        if cursor < 0:
            return result
        result.append(cursor)
        cursor += len(MAGMA)


def graft_first_locale(
    pc: bytes, xbox: bytes, name: str, *, allow_resize: bool = False
) -> bytes:
    pc_offsets = magma_offsets(pc)
    xbox_offsets = magma_offsets(xbox)
    if len(pc_offsets) != 2:
        raise ValueError(f"{name}: expected 2 PC MAGMA locales, got {len(pc_offsets)}")
    if len(xbox_offsets) != 5:
        raise ValueError(
            f"{name}: expected 5 Xbox MAGMA locales, got {len(xbox_offsets)}"
        )

    pc_start, pc_end = pc_offsets[:2]
    xbox_start, xbox_end = xbox_offsets[:2]
    pc_length = pc_end - pc_start
    xbox_length = xbox_end - xbox_start
    if pc_length != xbox_length and not allow_resize:
        raise ValueError(
            f"{name}: locale object size differs "
            f"(PC={pc_length}, Xbox={xbox_length})"
        )

    result = xbox[:xbox_start] + pc[pc_start:pc_end] + xbox[xbox_end:]
    if not allow_resize and len(result) != len(xbox):
        raise AssertionError(f"{name}: graft unexpectedly changed resource size")
    expected_offsets = [
        xbox_offsets[0],
        *[
            offset + (pc_length - xbox_length)
            for offset in xbox_offsets[1:]
        ],
    ]
    if magma_offsets(result) != expected_offsets:
        raise AssertionError(f"{name}: MAGMA boundaries changed")
    if len(result) != len(xbox):
        # The resource wrapper is big-endian on Xbox. Its declared payload
        # length is the complete resource size minus the 24-byte fixed header.
        result = bytearray(result)
        struct.pack_into(">I", result, 4, len(result) - 24)
        result = bytes(result)
    return result


def stored_wrapper(data: bytes, chunk_size: int = 0x8000) -> bytes:
    if not data:
        raise ValueError("empty raw stream is unsupported")
    chunks = [data[offset : offset + chunk_size] for offset in range(0, len(data), chunk_size)]
    if len(chunks) > 0xFFFF:
        raise ValueError("raw stream needs too many chunks")

    output = bytearray(RAW_SIGNATURE)
    output += struct.pack(">HBHHH", 1, 3, chunk_size, chunk_size, len(chunks))
    for chunk in chunks:
        output += struct.pack(">HH", len(chunk), len(chunk))
    for chunk in chunks:
        # Scimitar readers skip this field; the original archives call it CRC.
        output += b"\0\0\0\0"
        output += chunk
    return bytes(output)


def wrapper_source_chunks(
    raw_payload: bytes,
) -> list[list[tuple[int, int, bytes, bytes]]]:
    """Parse source chunk sizes, opaque fields, and stored payloads."""
    wrappers: list[list[tuple[int, int, bytes, bytes]]] = []
    cursor = 0
    while cursor < len(raw_payload):
        if raw_payload[cursor : cursor + 8] != RAW_SIGNATURE:
            raise ValueError(f"invalid raw wrapper signature at 0x{cursor:X}")
        if cursor + 17 > len(raw_payload):
            raise ValueError("truncated raw wrapper header")
        chunk_count = struct.unpack_from(">H", raw_payload, cursor + 15)[0]
        table_start = cursor + 17
        data_cursor = table_start + chunk_count * 4
        chunks: list[tuple[int, int, bytes, bytes]] = []
        for index in range(chunk_count):
            if data_cursor + 4 > len(raw_payload):
                raise ValueError("truncated raw chunk field")
            uncompressed_size, compressed_size = struct.unpack_from(
                ">HH", raw_payload, table_start + index * 4
            )
            field = raw_payload[data_cursor : data_cursor + 4]
            stored = raw_payload[
                data_cursor + 4 : data_cursor + 4 + compressed_size
            ]
            if len(stored) != compressed_size:
                raise ValueError("truncated raw chunk payload")
            chunks.append((uncompressed_size, compressed_size, field, stored))
            data_cursor += 4 + compressed_size
        wrappers.append(chunks)
        cursor = data_cursor
    return wrappers


def xmemlzx_wrapper(
    data: bytes,
    quickbms: Path,
    compress_script: Path,
    temporary_root: Path,
    tag: str,
    original_chunks: list[tuple[int, int, bytes, bytes]],
    chunk_size: int = 0x8000,
    force_stored_chunks: set[int] | None = None,
    preserve_source_stored_chunks: bool = True,
) -> bytes:
    """Encode native Xbox XMem/LZX chunks using QuickBMS' compressor."""
    chunks = [data[offset : offset + chunk_size] for offset in range(0, len(data), chunk_size)]
    if not chunks or len(chunks) > 0xFFFF:
        raise ValueError("unsupported XMem/LZX chunk count")

    input_path = temporary_root / f"{tag}.dat"
    output_directory = temporary_root / f"{tag}_chunks"
    input_path.write_bytes(data)
    output_directory.mkdir()
    command = [
        str(quickbms),
        "-Q",
        "-o",
        str(compress_script),
        str(input_path),
        str(output_directory),
    ]
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"QuickBMS XMem/LZX compression failed for {tag}:\n{completed.stdout}"
        )

    encoded_chunks: list[bytes] = []
    for index, plain in enumerate(chunks):
        encoded = (output_directory / f"{index:05d}.bin").read_bytes()
        if len(encoded) < 10 or encoded[-5:] != b"\0" * 5:
            raise ValueError(f"{tag} chunk {index}: unexpected QuickBMS framing")
        if len(plain) == chunk_size:
            if encoded[:3] != b"\xFF\x80\x00":
                raise ValueError(f"{tag} chunk {index}: expected full-block prefix")
            # Ubisoft uses XMem's compact two-byte prefix for full blocks.
            encoded = encoded[3:-5]
        else:
            if encoded[0] != 0xFF or struct.unpack_from(">H", encoded, 1)[0] != len(plain):
                raise ValueError(f"{tag} chunk {index}: invalid partial-block prefix")
            encoded = encoded[:-5]
        if force_stored_chunks and index in force_stored_chunks:
            encoded = plain
        elif (
            preserve_source_stored_chunks
            and
            index < len(original_chunks)
            and original_chunks[index][0] == len(plain)
            and original_chunks[index][1] == len(plain)
        ):
            # Match Ubisoft's choice to keep incompressible source blocks
            # uncompressed. This also makes a no-op rebuild byte-identical.
            encoded = plain
        elif len(encoded) >= len(plain):
            # XMem/LZX output can occasionally be slightly larger than a full
            # 32 KiB input block. Ubisoft's reader expects such incompressible
            # blocks to be stored verbatim; feeding the oversized LZX stream
            # to the console decoder results in an LZX error.
            encoded = plain
        if len(encoded) > 0xFFFF:
            raise ValueError(f"{tag} chunk {index}: compressed block is too large")
        encoded_chunks.append(encoded)

    output = bytearray(RAW_SIGNATURE)
    output += struct.pack(">HBHHH", 1, 3, chunk_size, chunk_size, len(chunks))
    for plain, encoded in zip(chunks, encoded_chunks):
        output += struct.pack(">HH", len(plain), len(encoded))
    for index, encoded in enumerate(encoded_chunks):
        # The original loader-facing format carries four opaque bytes before
        # each compressed chunk. Preserve the source values where possible;
        # community extractors label this field CRC but do not validate it.
        field = (
            original_chunks[index][2]
            if index < len(original_chunks)
            else b"\0" * 4
        )
        output += field
        output += encoded
    return bytes(output)


def replace_forge_entry(
    source: Path, output: Path, entry_name: str, payload: bytes
) -> tuple[int, int]:
    archive = parse_forge(source)
    matches = [entry for entry in archive.entries if entry.name == entry_name]
    if len(matches) != 1:
        raise ValueError(f"expected one FORGE entry named {entry_name!r}")
    entry = matches[0]

    with source.open("rb") as stream:
        stream.seek(entry.offset)
        wrapper = bytearray(stream.read(440))
    if len(wrapper) != 440 or wrapper[:8] != b"FILEDATA":
        raise ValueError(f"{entry_name}: unsupported FILEDATA wrapper")
    struct.pack_into("<I", wrapper, 395, len(payload))

    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    with output.open("r+b") as stream:
        stream.seek(0, 2)
        new_offset = stream.tell()
        alignment = (-new_offset) & 0x7FF
        if alignment:
            stream.write(b"\0" * alignment)
            new_offset += alignment
        stream.write(wrapper)
        stream.write(payload)
        stream.seek(entry.record_offset)
        stream.write(struct.pack("<Q", new_offset))
        stream.seek(entry.record_offset + 12)
        stream.write(struct.pack("<I", len(payload)))

    check = parse_forge(output)
    patched = check.entries[entry.index]
    if patched.offset != new_offset or patched.size != len(payload):
        raise AssertionError("FORGE index round-trip verification failed")
    return entry.size, len(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pc-directory", required=True, type=Path)
    parser.add_argument("--pc-data", required=True, type=Path)
    parser.add_argument("--xbox-directory", required=True, type=Path)
    parser.add_argument("--xbox-data", required=True, type=Path)
    parser.add_argument("--source-forge", required=True, type=Path)
    parser.add_argument("--output-forge", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--quickbms", type=Path)
    parser.add_argument("--compress-script", type=Path)
    parser.add_argument(
        "--exclude-resource",
        action="append",
        default=[],
        help="additional MGB resource name to leave untouched",
    )
    parser.add_argument(
        "--inplace-text-probe",
        action="store_true",
        help="change only one same-length Xbox UTF-16 string; graft no PC objects",
    )
    args = parser.parse_args()

    pc_resources = parse_bundle(args.pc_directory, args.pc_data, "little")
    xbox_resources = parse_bundle(args.xbox_directory, args.xbox_data, "big")
    pc_mgb = {
        resource.name: resource
        for resource in pc_resources
        if resource.type_id == MGB_TYPE
    }

    # Globals_PC_MGB is specific to the PC Director's Cut. Globals_MGB itself
    # is shared with Xbox and contains the immediately visible front-end GUI;
    # its Czech locale is larger, so the bundle directory is rebuilt below.
    excluded = {"Globals_PC_MGB", *args.exclude_resource}
    target_names = (
        []
        if args.inplace_text_probe
        else sorted(
            {
                resource.name
                for resource in xbox_resources
                if resource.type_id == MGB_TYPE
                and resource.name in pc_mgb
                and resource.name not in excluded
            }
        )
    )
    if not target_names and not args.inplace_text_probe:
        raise ValueError("no matching MGB resources found")

    pc_data = args.pc_data.read_bytes()
    xbox_data = args.xbox_data.read_bytes()
    rebuilt = bytearray()
    rebuilt_directory = bytearray(args.xbox_directory.read_bytes())
    patched_names: list[str] = []
    for resource in xbox_resources:
        original = xbox_data[resource.offset : resource.offset + resource.size]
        if resource.name not in target_names:
            rebuilt += original
            continue

        pc_resource = pc_mgb[resource.name]
        pc_blob = pc_data[pc_resource.offset : pc_resource.offset + pc_resource.size]
        patched = graft_first_locale(
            pc_blob,
            original,
            resource.name,
            allow_resize=resource.name == "Globals_MGB",
        )
        struct.pack_into(">I", rebuilt_directory, 2 + resource.index * 8 + 4, len(patched))
        rebuilt += patched
        patched_names.append(resource.name)

    if set(patched_names) != set(target_names) or len(patched_names) != len(target_names):
        raise AssertionError("not all requested resources were patched")
    if args.inplace_text_probe:
        source_text = ">Animus Credits".encode("utf-16le")
        target_text = ">ČEŠTINA TEST!!".encode("utf-16le")
        if len(source_text) != len(target_text):
            raise AssertionError("probe strings must have exactly the same byte length")
        if bytes(rebuilt).count(source_text) != 1:
            raise ValueError("expected one Xbox '>Animus Credits' string")
        rebuilt = bytearray(bytes(rebuilt).replace(source_text, target_text))
        patched_names.append("Globals_MGB::>Animus Credits -> >ČEŠTINA TEST!!")

    archive = parse_forge(args.source_forge)
    bootstrap = next(
        entry for entry in archive.entries if entry.name == "Game Bootstrap Settings"
    )
    with args.source_forge.open("rb") as stream:
        stream.seek(bootstrap.offset + 440)
        source_raw_payload = stream.read(bootstrap.size)
    original_chunks = wrapper_source_chunks(source_raw_payload)
    if len(original_chunks) != 2:
        raise ValueError("expected two source raw wrappers")

    if bool(args.quickbms) != bool(args.compress_script):
        raise ValueError("--quickbms and --compress-script must be used together")
    if args.quickbms:
        args.output_forge.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="ac1_xmem_", dir=args.output_forge.parent
        ) as temporary:
            temporary_root = Path(temporary)
            raw_payload = xmemlzx_wrapper(
                bytes(rebuilt_directory),
                args.quickbms,
                args.compress_script,
                temporary_root,
                "directory",
                original_chunks[0],
            ) + xmemlzx_wrapper(
                bytes(rebuilt),
                args.quickbms,
                args.compress_script,
                temporary_root,
                "data",
                original_chunks[1],
            )
        compression_mode = "native-xmemlzx"
    else:
        raw_payload = stored_wrapper(bytes(rebuilt_directory)) + stored_wrapper(
            bytes(rebuilt)
        )
        compression_mode = "stored-test"
    old_size, new_size = replace_forge_entry(
        args.source_forge,
        args.output_forge,
        "Game Bootstrap Settings",
        raw_payload,
    )

    report = {
        "mode": (
            "inplace-same-length-text-probe"
            if args.inplace_text_probe
            else "first-locale-graft"
        ),
        "compression": compression_mode,
        "patched_resources": patched_names,
        "excluded_resources": sorted(excluded),
        "resource_count_pc": len(pc_resources),
        "resource_count_xbox": len(xbox_resources),
        "decompressed_directory_size": len(rebuilt_directory),
        "decompressed_data_size": len(rebuilt),
        "original_raw_payload_size": old_size,
        "rebuilt_raw_payload_size": new_size,
        "output_forge_size": args.output_forge.stat().st_size,
        "output_forge_sha256": sha256(args.output_forge.read_bytes()),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
