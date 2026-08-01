#!/usr/bin/env python3
"""Build the complete safe Czech text patch for Assassin's Creed Xbox 360.

The builder deliberately keeps every Xbox MAGMA object and every decompressed
resource at its original size.  It only rewrites the serialized first-locale
text tables.  PC objects and PC bytecode are never copied into the console
archives.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path

from ac1_build_gui_forge import wrapper_source_chunks, xmemlzx_wrapper
from ac1_complete_text import (
    patch_animus,
    patch_email,
    patch_globals,
    patch_press_start,
)
from ac1_forge import parse_forge
from ac1_resource_bundle import Resource, parse_bundle


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def resource_map(directory: Path, data: Path, endian: str) -> dict[str, Resource]:
    result: dict[str, Resource] = {}
    for resource in parse_bundle(directory, data, endian):
        if not resource.name:
            continue
        # Geometry and material names are commonly reused in one bundle.
        # Localization resources targeted by this builder are unique.
        result.setdefault(resource.name, resource)
    return result


def resource_blob(data: bytes, resource: Resource) -> bytes:
    return data[resource.offset : resource.offset + resource.size]


def patch_bundle(
    xbox_directory: Path,
    xbox_data: Path,
    pc_directory: Path,
    pc_data: Path,
    patches: dict[str, object],
) -> tuple[bytes, dict[str, object]]:
    xbox_resources = resource_map(xbox_directory, xbox_data, "big")
    pc_resources = resource_map(pc_directory, pc_data, "little")
    original_data = xbox_data.read_bytes()
    pc_stream = pc_data.read_bytes()
    rebuilt = bytearray(original_data)
    report: dict[str, object] = {}

    for name, patcher in patches.items():
        if name not in xbox_resources:
            raise ValueError(f"Xbox resource not found: {name}")
        xbox_resource = xbox_resources[name]
        original = resource_blob(original_data, xbox_resource)
        if patcher is patch_press_start:
            patched, details = patcher(original)
        else:
            if name not in pc_resources:
                raise ValueError(f"PC resource not found: {name}")
            pc_resource = pc_resources[name]
            pc_blob = resource_blob(pc_stream, pc_resource)
            patched, details = patcher(original, pc_blob)
        if len(patched) != len(original):
            raise AssertionError(f"{name}: patch changed resource size")
        start = xbox_resource.offset
        rebuilt[start : start + xbox_resource.size] = patched
        report[name] = {
            "offset": start,
            "size": xbox_resource.size,
            "details": (
                [asdict(item) for item in details]
                if isinstance(details, list)
                else details
            ),
        }

    changed = []
    rebuilt_bytes = bytes(rebuilt)
    for name, resource in xbox_resources.items():
        old = resource_blob(original_data, resource)
        new = resource_blob(rebuilt_bytes, resource)
        if old != new:
            changed.append(name)
    if set(changed) != set(patches):
        raise AssertionError(
            f"unexpected changed resource set: {changed}; expected {sorted(patches)}"
        )
    return rebuilt_bytes, report


def forge_entry_payload(path: Path, entry_name: str) -> bytes:
    archive = parse_forge(path)
    matches = [entry for entry in archive.entries if entry.name == entry_name]
    if len(matches) != 1:
        raise ValueError(f"expected one FORGE entry named {entry_name!r}")
    entry = matches[0]
    with path.open("rb") as stream:
        stream.seek(entry.offset + 440)
        payload = stream.read(entry.size)
    if len(payload) != entry.size:
        raise ValueError(f"{entry_name}: truncated raw payload")
    return payload


def compress_bundle(
    directory: bytes,
    data: bytes,
    source_raw: bytes,
    quickbms: Path,
    compress_script: Path,
    temporary_root: Path,
    tag: str,
) -> bytes:
    wrappers = wrapper_source_chunks(source_raw)
    if len(wrappers) != 2:
        raise ValueError(f"{tag}: expected two raw wrappers")
    return xmemlzx_wrapper(
        directory,
        quickbms,
        compress_script,
        temporary_root,
        f"{tag}_directory",
        wrappers[0],
    ) + xmemlzx_wrapper(
        data,
        quickbms,
        compress_script,
        temporary_root,
        f"{tag}_data",
        wrappers[1],
    )


def replace_forge_entries(
    source: Path, output: Path, replacements: dict[str, bytes]
) -> dict[str, dict[str, int]]:
    archive = parse_forge(source)
    by_name = {entry.name: entry for entry in archive.entries}
    for name in replacements:
        if name not in by_name:
            raise ValueError(f"FORGE entry not found: {name}")

    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    report: dict[str, dict[str, int]] = {}
    with source.open("rb") as source_stream, output.open("r+b") as stream:
        for name, payload in replacements.items():
            entry = by_name[name]
            source_stream.seek(entry.offset)
            wrapper = bytearray(source_stream.read(440))
            if len(wrapper) != 440 or wrapper[:8] != b"FILEDATA":
                raise ValueError(f"{name}: unsupported FILEDATA wrapper")
            struct.pack_into("<I", wrapper, 395, len(payload))

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
            report[name] = {
                "original_offset": entry.offset,
                "original_payload_size": entry.size,
                "new_offset": new_offset,
                "new_payload_size": len(payload),
            }

    checked = parse_forge(output)
    for name, expected in report.items():
        entry = next(item for item in checked.entries if item.name == name)
        if (
            entry.offset != expected["new_offset"]
            or entry.size != expected["new_payload_size"]
        ):
            raise AssertionError(f"{name}: FORGE index verification failed")
    return report


def replace_forge_entries_inplace(
    source: Path, output: Path, replacements: dict[str, bytes]
) -> dict[str, dict[str, int]]:
    """Replace payloads inside their original aligned slots.

    Streamed cell metadata can retain direct physical offsets in addition to
    the main FORGE index. Keeping those offsets unchanged is therefore safer
    than appending replacement cells to the end of the archive.
    """
    archive = parse_forge(source)
    by_name = {entry.name: entry for entry in archive.entries}
    ordered = sorted(archive.entries, key=lambda entry: entry.offset)
    next_offset: dict[int, int] = {}
    for index, entry in enumerate(ordered):
        next_offset[entry.index] = (
            ordered[index + 1].offset
            if index + 1 < len(ordered)
            else source.stat().st_size
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    report: dict[str, dict[str, int]] = {}
    with source.open("rb") as source_stream, output.open("r+b") as stream:
        for name, payload in replacements.items():
            if name not in by_name:
                raise ValueError(f"FORGE entry not found: {name}")
            entry = by_name[name]
            allocation = next_offset[entry.index] - entry.offset - 440
            if len(payload) > allocation:
                raise ValueError(
                    f"{name}: payload needs {len(payload)} bytes but the "
                    f"in-place slot has {allocation}"
                )
            source_stream.seek(entry.offset)
            wrapper = bytearray(source_stream.read(440))
            if len(wrapper) != 440 or wrapper[:8] != b"FILEDATA":
                raise ValueError(f"{name}: unsupported FILEDATA wrapper")
            struct.pack_into("<I", wrapper, 395, len(payload))

            stream.seek(entry.offset)
            stream.write(wrapper)
            stream.write(payload)
            tail = allocation - len(payload)
            if tail:
                stream.write(b"\0" * tail)
            stream.seek(entry.record_offset + 12)
            stream.write(struct.pack("<I", len(payload)))
            report[name] = {
                "original_offset": entry.offset,
                "original_payload_size": entry.size,
                "new_offset": entry.offset,
                "new_payload_size": len(payload),
                "slot_capacity": allocation,
            }

    checked = parse_forge(output)
    for name, expected in report.items():
        entry = next(item for item in checked.entries if item.name == name)
        if (
            entry.offset != expected["original_offset"]
            or entry.size != expected["new_payload_size"]
        ):
            raise AssertionError(f"{name}: in-place FORGE verification failed")
    return report


def verify_decoded_bundle(
    decoder: Path,
    raw_payload: bytes,
    destination: Path,
    expected_directory: bytes,
    expected_data: bytes,
) -> None:
    raw_path = destination.parent / f"{destination.name}.bin"
    raw_path.write_bytes(raw_payload)
    if destination.exists():
        shutil.rmtree(destination)
    completed = subprocess.run(
        [str(decoder), str(raw_path), str(destination)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"native decode failed:\n{completed.stdout}")
    # The native helper creates one child directory named after the raw input.
    decoded_root = destination / raw_path.stem
    actual_directory = (decoded_root / "0.dat").read_bytes()
    actual_data = (decoded_root / "1.dat").read_bytes()
    if actual_directory != expected_directory or actual_data != expected_data:
        raise AssertionError("compressed archive did not decode byte-exactly")
    raw_path.unlink()


def build(args: argparse.Namespace) -> dict[str, object]:
    output = args.output
    output.mkdir(parents=True, exist_ok=True)

    bootstrap_directory = args.bootstrap_xbox / "0.dat"
    bootstrap_data = args.bootstrap_xbox / "1.dat"
    bootstrap_rebuilt, bootstrap_text = patch_bundle(
        bootstrap_directory,
        bootstrap_data,
        args.bootstrap_pc / "0.dat",
        args.bootstrap_pc / "1.dat",
        {"Globals_MGB": patch_globals},
    )

    map_directory = args.map_xbox / "0.dat"
    map_data = args.map_xbox / "1.dat"
    map_rebuilt, map_text = patch_bundle(
        map_directory,
        map_data,
        args.map_pc / "0.dat",
        args.map_pc / "1.dat",
        {"PressStart_MGB": patch_press_start},
    )

    cell84_directory = args.cell84_xbox / "0.dat"
    cell84_data = args.cell84_xbox / "1.dat"
    cell84_rebuilt, cell84_text = patch_bundle(
        cell84_directory,
        cell84_data,
        args.cell84_pc / "0.dat",
        args.cell84_pc / "1.dat",
        {"Animus_MGB": patch_animus},
    )

    cell27_directory = args.cell27_xbox / "0.dat"
    cell27_data = args.cell27_xbox / "1.dat"
    cell27_rebuilt, cell27_text = patch_bundle(
        cell27_directory,
        cell27_data,
        args.cell27_pc / "0.dat",
        args.cell27_pc / "1.dat",
        {
            "PressStart_MGB": patch_press_start,
            "Email_UI_MGB": patch_email,
        },
    )

    source_specs = [
        (
            "Data360.forge",
            args.source_data360,
            "Game Bootstrap Settings",
            bootstrap_directory.read_bytes(),
            bootstrap_rebuilt,
        ),
        (
            "Data360_Map_Menu.forge",
            args.source_map,
            "Map_Menu",
            map_directory.read_bytes(),
            map_rebuilt,
        ),
    ]
    compressed: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="ac1_complete_", dir=output) as temporary:
        temporary_root = Path(temporary)
        for filename, source, entry, directory, data in source_specs:
            compressed[filename] = compress_bundle(
                directory,
                data,
                forge_entry_payload(source, entry),
                args.quickbms,
                args.compress_script,
                temporary_root,
                filename.replace(".", "_"),
            )

        cell84_name = "Cell00084_DataBlock"
        cell27_name = "Cell00027_DataBlock"
        compressed["cell84"] = compress_bundle(
            cell84_directory.read_bytes(),
            cell84_rebuilt,
            args.cell84_raw.read_bytes(),
            args.quickbms,
            args.compress_script,
            temporary_root,
            "present_cell84",
        )
        compressed["cell27"] = compress_bundle(
            cell27_directory.read_bytes(),
            cell27_rebuilt,
            args.cell27_raw.read_bytes(),
            args.quickbms,
            args.compress_script,
            temporary_root,
            "present_cell27",
        )

        data360_report = replace_forge_entries(
            args.source_data360,
            output / "Data360.forge",
            {"Game Bootstrap Settings": compressed["Data360.forge"]},
        )
        map_report = replace_forge_entries(
            args.source_map,
            output / "Data360_Map_Menu.forge",
            {"Map_Menu": compressed["Data360_Map_Menu.forge"]},
        )
        present_report = replace_forge_entries_inplace(
            args.source_present,
            output / "Data360_Present_Room.forge",
            {
                cell84_name: compressed["cell84"],
                cell27_name: compressed["cell27"],
            },
        )

        verification_root = output / "verify_decode"
        verification_root.mkdir(exist_ok=True)
        verify_decoded_bundle(
            args.decoder,
            compressed["Data360.forge"],
            verification_root / "bootstrap",
            bootstrap_directory.read_bytes(),
            bootstrap_rebuilt,
        )
        verify_decoded_bundle(
            args.decoder,
            compressed["Data360_Map_Menu.forge"],
            verification_root / "map_menu",
            map_directory.read_bytes(),
            map_rebuilt,
        )
        verify_decoded_bundle(
            args.decoder,
            compressed["cell84"],
            verification_root / "present_cell84",
            cell84_directory.read_bytes(),
            cell84_rebuilt,
        )
        verify_decoded_bundle(
            args.decoder,
            compressed["cell27"],
            verification_root / "present_cell27",
            cell27_directory.read_bytes(),
            cell27_rebuilt,
        )

    files = {}
    for filename in (
        "Data360.forge",
        "Data360_Map_Menu.forge",
        "Data360_Present_Room.forge",
    ):
        path = output / filename
        files[filename] = {
            "size": path.stat().st_size,
            "sha256": sha256_path(path),
        }

    report: dict[str, object] = {
        "mode": "xbox-native-fixed-extent-czech-text",
        "text_patches": {
            "bootstrap": bootstrap_text,
            "map_menu": map_text,
            "present_cell84": cell84_text,
            "present_cell27": cell27_text,
        },
        "forge_replacements": {
            "Data360.forge": data360_report,
            "Data360_Map_Menu.forge": map_report,
            "Data360_Present_Room.forge": present_report,
        },
        "files": files,
        "verification": "all rebuilt raw streams decode byte-exactly",
    }
    (output / "build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--compress-script", required=True, type=Path)
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--source-data360", required=True, type=Path)
    parser.add_argument("--source-map", required=True, type=Path)
    parser.add_argument("--source-present", required=True, type=Path)
    parser.add_argument("--bootstrap-xbox", required=True, type=Path)
    parser.add_argument("--bootstrap-pc", required=True, type=Path)
    parser.add_argument("--map-xbox", required=True, type=Path)
    parser.add_argument("--map-pc", required=True, type=Path)
    parser.add_argument("--cell84-xbox", required=True, type=Path)
    parser.add_argument("--cell84-pc", required=True, type=Path)
    parser.add_argument("--cell84-raw", required=True, type=Path)
    parser.add_argument("--cell27-xbox", required=True, type=Path)
    parser.add_argument("--cell27-pc", required=True, type=Path)
    parser.add_argument("--cell27-raw", required=True, type=Path)
    args = parser.parse_args()
    # Windows terminals commonly use CP1250, which cannot encode the Xbox
    # controller glyph U+00B2 used in one source label.
    print(json.dumps(build(args), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
