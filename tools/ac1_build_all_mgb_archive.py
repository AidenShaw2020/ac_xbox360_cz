#!/usr/bin/env python3
"""Safely translate every MGB resource in selected AC1 Xbox FORGE entries."""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from ac1_build_complete_text import (
    forge_entry_payload,
    replace_forge_entries,
    replace_forge_entries_inplace,
    sha256_path,
)
from ac1_build_gui_forge import wrapper_source_chunks, xmemlzx_wrapper
from ac1_build_masyaf_text import (
    CHUNK_SIZE,
    changed_chunks,
    partial_resource_map,
    serialize_wrapper,
    wrapper_ranges,
)
from ac1_complete_text import (
    patch_complete_mgb,
    patch_complete_mgb_slot_stable,
)
from ac1_forge import parse_forge
from ac1_resource_bundle import parse_bundle


MGB_TYPE = 0x1823A912


def replace_forge_entries_at_offsets(
    source: Path,
    output: Path,
    replacements: dict[str, bytes],
    target_offsets: dict[str, int],
) -> dict[str, dict[str, int]]:
    """Place rebuilt entries into explicit existing FILEDATA slots.

    Some AC1 Xbox code paths retain a physical offset outside the normal FORGE
    index.  Retargeting both the payload and the main index keeps those paths
    consistent and avoids leaving the active copy appended at end-of-file.
    """
    archive = parse_forge(source)
    by_name = {entry.name: entry for entry in archive.entries}
    indexed_offsets = sorted({entry.offset for entry in archive.entries})
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    report: dict[str, dict[str, int]] = {}
    with source.open("rb") as source_stream, output.open("r+b") as stream:
        for name, payload in replacements.items():
            entry = by_name.get(name)
            if entry is None:
                raise ValueError(f"FORGE entry not found: {name}")
            target = target_offsets.get(name, entry.offset)
            following = [offset for offset in indexed_offsets if offset > target]
            slot_end = following[0] if following else source.stat().st_size
            allocation = slot_end - target - 440
            if allocation < 0 or len(payload) > allocation:
                raise ValueError(
                    f"{name}: payload needs {len(payload)} bytes but target "
                    f"slot at {target} has {allocation}"
                )

            source_stream.seek(target)
            wrapper = bytearray(source_stream.read(440))
            if len(wrapper) != 440 or wrapper[:8] != b"FILEDATA":
                raise ValueError(
                    f"{name}: target offset {target} has no FILEDATA wrapper"
                )
            struct.pack_into("<I", wrapper, 395, len(payload))

            stream.seek(target)
            stream.write(wrapper)
            stream.write(payload)
            tail = allocation - len(payload)
            if tail:
                stream.write(b"\0" * tail)
            stream.seek(entry.record_offset)
            stream.write(struct.pack("<Q", target))
            stream.seek(entry.record_offset + 12)
            stream.write(struct.pack("<I", len(payload)))
            report[name] = {
                "original_offset": entry.offset,
                "original_payload_size": entry.size,
                "new_offset": target,
                "new_payload_size": len(payload),
                "slot_capacity": allocation,
                "slot_slack": allocation - len(payload),
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


def mirror_indexed_entries_at_offsets(
    source: Path,
    output: Path,
    target_offsets: dict[str, int],
) -> dict[str, dict[str, int]]:
    """Mirror indexed source payloads into legacy physical FILEDATA slots.

    The mirror is intentionally byte-for-byte: no data chunks are recompressed
    and the FORGE index remains pointed at the normal rebuilt entry.  This
    supports AC1 code paths that still read the original physical bootstrap
    while avoiding the Xbox freeze caused by recompressing untouched chunks.
    """
    source_archive = parse_forge(source)
    output_archive = parse_forge(output)
    source_by_name = {entry.name: entry for entry in source_archive.entries}
    output_offsets = sorted({entry.offset for entry in output_archive.entries})
    report: dict[str, dict[str, int]] = {}
    with source.open("rb") as source_stream, output.open("r+b") as stream:
        for name, target in target_offsets.items():
            entry = source_by_name.get(name)
            if entry is None:
                raise ValueError(f"FORGE entry not found: {name}")
            following = [offset for offset in output_offsets if offset > target]
            slot_end = following[0] if following else output.stat().st_size
            allocation = slot_end - target - 440
            if entry.size > allocation:
                raise ValueError(
                    f"{name}: source payload needs {entry.size} bytes but "
                    f"mirror slot at {target} has {allocation}"
                )

            source_stream.seek(entry.offset)
            indexed_wrapper = source_stream.read(440)
            payload = source_stream.read(entry.size)
            if (
                len(indexed_wrapper) != 440
                or indexed_wrapper[:8] != b"FILEDATA"
                or len(payload) != entry.size
            ):
                raise ValueError(f"{name}: indexed source payload is invalid")

            stream.seek(target)
            target_wrapper = bytearray(stream.read(440))
            if len(target_wrapper) != 440 or target_wrapper[:8] != b"FILEDATA":
                raise ValueError(
                    f"{name}: mirror offset {target} has no FILEDATA wrapper"
                )
            struct.pack_into("<I", target_wrapper, 395, entry.size)
            stream.seek(target)
            stream.write(target_wrapper)
            stream.write(payload)
            tail = allocation - entry.size
            if tail:
                stream.write(b"\0" * tail)
            report[name] = {
                "source_indexed_offset": entry.offset,
                "mirror_offset": target,
                "payload_size": entry.size,
                "slot_capacity": allocation,
                "slot_slack": allocation - entry.size,
                "payload_byte_exact": True,
            }
    return report


def decode_xbox_prefix(
    raw: bytes, decoder: Path, root: Path, tag: str
) -> tuple[Path, int, str]:
    raw_path = root / f"{tag}.bin"
    destination = root / f"{tag}_decoded"
    raw_path.write_bytes(raw)
    completed = subprocess.run(
        [str(decoder), str(raw_path), str(destination)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    decoded = destination / raw_path.stem
    if not (decoded / "0.dat").exists() or not (decoded / "1.dat").exists():
        raise RuntimeError(
            f"{tag}: Xbox decoder produced no resource bundle:\n"
            f"{completed.stdout}"
        )
    return decoded, completed.returncode, completed.stdout.strip()


def decode_pc(
    raw: bytes,
    quickbms: Path,
    raw_script: Path,
    root: Path,
    tag: str,
) -> Path:
    raw_path = root / f"{tag}.bin"
    destination = root / f"{tag}_decoded"
    raw_path.write_bytes(raw)
    destination.mkdir()
    completed = subprocess.run(
        [
            str(quickbms),
            "-Q",
            "-o",
            str(raw_script),
            str(raw_path),
            str(destination),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    decoded = destination / raw_path.stem
    if (
        completed.returncode != 0
        or not (decoded / "0.dat").exists()
        or not (decoded / "1.dat").exists()
    ):
        raise RuntimeError(f"{tag}: PC decode failed:\n{completed.stdout}")
    return decoded


def rebuild_entry(
    *,
    name: str,
    xbox_raw: bytes,
    pc_raw: bytes,
    decoder: Path,
    quickbms: Path,
    pc_raw_script: Path,
    compress_script: Path,
    temporary: Path,
    optimize_all_chunks: bool = False,
    optimize_unchanged_limit: int | None = None,
    slot_stable: bool = False,
    template_raw: bytes | None = None,
    selected_resources: set[str] | None = None,
) -> tuple[bytes, dict[str, object], bytes, bytes]:
    xbox_decoded, decoder_returncode, decoder_output = decode_xbox_prefix(
        xbox_raw, decoder, temporary, f"xbox_{name}"
    )
    pc_decoded = decode_pc(
        pc_raw,
        quickbms,
        pc_raw_script,
        temporary,
        f"pc_{name}",
    )
    xbox_directory = (xbox_decoded / "0.dat").read_bytes()
    xbox_data = (xbox_decoded / "1.dat").read_bytes()
    pc_directory_path = pc_decoded / "0.dat"
    pc_data_path = pc_decoded / "1.dat"
    pc_data = pc_data_path.read_bytes()
    template_resources: dict[str, object] = {}
    template_data = b""
    template_decoder_returncode: int | None = None
    template_decoder_output = ""
    if template_raw is not None:
        template_decoded, template_decoder_returncode, template_decoder_output = (
            decode_xbox_prefix(
                template_raw, decoder, temporary, f"template_{name}"
            )
        )
        template_directory = (template_decoded / "0.dat").read_bytes()
        template_data = (template_decoded / "1.dat").read_bytes()
        template_resources = partial_resource_map(
            template_directory, template_data
        )

    xbox_resources = partial_resource_map(xbox_directory, xbox_data)
    pc_resources = {
        resource.directory_id: resource
        for resource in parse_bundle(
            pc_directory_path, pc_data_path, "little"
        )
        if resource.type_id == MGB_TYPE
    }
    rebuilt_data = bytearray(xbox_data)
    resource_reports: dict[str, object] = {}
    changed_resources: list[str] = []
    for resource in xbox_resources.values():
        if resource.type_id != MGB_TYPE:
            continue
        if (
            selected_resources is not None
            and resource.name not in selected_resources
        ):
            continue
        xbox_blob = xbox_data[
            resource.offset : resource.offset + resource.size
        ]
        template_resource = template_resources.get(resource.name)
        template_pair = None
        base_blob = xbox_blob
        if (
            template_resource is not None
            and template_resource.type_id == resource.type_id
            and template_resource.size == resource.size
        ):
            base_blob = template_data[
                template_resource.offset
                : template_resource.offset + template_resource.size
            ]
            template_pair = template_resource.name
        pc_resource = pc_resources.get(resource.directory_id)
        if pc_resource is None:
            # Still apply console-only literal and hash overrides safely.
            pc_blob = base_blob
            paired_name = None
        else:
            pc_blob = pc_data[
                pc_resource.offset : pc_resource.offset + pc_resource.size
            ]
            paired_name = pc_resource.name
        try:
            patcher = (
                patch_complete_mgb_slot_stable
                if slot_stable
                else patch_complete_mgb
            )
            patched, patch_report = patcher(base_blob, pc_blob)
        except ValueError as error:
            resource_reports[resource.name] = {
                "status": "skipped",
                "reason": str(error),
                "resource_id": f"0x{resource.directory_id:08X}",
            }
            continue
        if len(patched) != len(xbox_blob):
            raise AssertionError(f"{resource.name}: resource size changed")
        changed = sum(left != right for left, right in zip(xbox_blob, patched))
        resource_reports[resource.name] = {
            "status": "changed" if changed else "unchanged",
            "resource_id": f"0x{resource.directory_id:08X}",
            "offset": resource.offset,
            "size": resource.size,
            "pc_pair": paired_name,
            "template_pair": template_pair,
            "changed_bytes": changed,
            "text": patch_report,
        }
        if changed:
            rebuilt_data[
                resource.offset : resource.offset + resource.size
            ] = patched
            changed_resources.append(resource.name)

    rebuilt_data_bytes = bytes(rebuilt_data)
    modified_indices = changed_chunks(xbox_data, rebuilt_data_bytes)
    if not modified_indices:
        return xbox_raw, {
            "entry": name,
            "changed_resources": [],
            "resources": resource_reports,
            "decoder_returncode": decoder_returncode,
            "decoder_output": decoder_output,
            "template_decoder_returncode": template_decoder_returncode,
            "template_decoder_output": template_decoder_output,
        }, xbox_directory, rebuilt_data_bytes

    source_wrappers = wrapper_source_chunks(xbox_raw)
    ranges = wrapper_ranges(xbox_raw)
    if len(source_wrappers) != 2 or len(ranges) != 2:
        raise ValueError(f"{name}: expected two compression wrappers")
    data_chunks = list(source_wrappers[1])
    if max(modified_indices) >= len(data_chunks):
        raise ValueError(f"{name}: changed chunk is outside source wrapper")

    compression_indices = (
        list(range(len(data_chunks)))
        if optimize_all_chunks
        and len(xbox_data) >= sum(chunk[0] for chunk in data_chunks)
        else modified_indices
    )
    plain_changed = b"".join(
        rebuilt_data_bytes[
            index * CHUNK_SIZE : (index + 1) * CHUNK_SIZE
        ]
        for index in compression_indices
    )
    source_changed = [data_chunks[index] for index in compression_indices]
    encoded_wrapper = xmemlzx_wrapper(
        plain_changed,
        quickbms,
        compress_script,
        temporary,
        f"encoded_{name}",
        source_changed,
        preserve_source_stored_chunks=not optimize_all_chunks,
    )
    encoded_chunks = wrapper_source_chunks(encoded_wrapper)
    if len(encoded_chunks) != 1:
        raise AssertionError(f"{name}: temporary compression wrapper mismatch")
    optimized_candidates: list[
        tuple[int, int, tuple[int, int, bytes, bytes]]
    ] = []
    for index, replacement in zip(compression_indices, encoded_chunks[0]):
        original = data_chunks[index]
        if index in modified_indices:
            data_chunks[index] = replacement
        elif replacement[1] < original[1]:
            optimized_candidates.append(
                (original[1] - replacement[1], index, replacement)
            )
    optimized_candidates.sort(reverse=True)
    if optimize_unchanged_limit is not None:
        optimized_candidates = optimized_candidates[
            : optimize_unchanged_limit
        ]
    for _saving, index, replacement in optimized_candidates:
        data_chunks[index] = replacement
    optimized_indices = {
        index for _saving, index, _replacement in optimized_candidates
    }
    optimized_unchanged = len(optimized_indices)

    directory_start, directory_end = ranges[0]
    data_start, _data_end = ranges[1]
    rebuilt_raw = (
        xbox_raw[directory_start:directory_end]
        + serialize_wrapper(
            xbox_raw[data_start : data_start + 17], data_chunks
        )
    )
    rebuilt_wrappers = wrapper_source_chunks(rebuilt_raw)
    for wrapper_index, (before, after) in enumerate(
        zip(source_wrappers, rebuilt_wrappers)
    ):
        for chunk_index, (old_chunk, new_chunk) in enumerate(
            zip(before, after)
        ):
            if (
                wrapper_index == 1
                and (
                    chunk_index in modified_indices
                    or (
                        optimize_all_chunks
                        and chunk_index in optimized_indices
                    )
                )
            ):
                continue
            if old_chunk != new_chunk:
                raise AssertionError(
                    f"{name}: untouched wrapper {wrapper_index} "
                    f"chunk {chunk_index} changed"
                )

    report = {
        "entry": name,
        "changed_resources": changed_resources,
        "changed_data_chunks": modified_indices,
        "preserved_data_chunks": len(data_chunks) - len(modified_indices),
        "optimized_unchanged_chunks": optimized_unchanged,
        "raw_before_size": len(xbox_raw),
        "raw_after_size": len(rebuilt_raw),
        "resources": resource_reports,
        "decoder_returncode": decoder_returncode,
        "decoder_output": decoder_output,
        "template_decoder_returncode": template_decoder_returncode,
        "template_decoder_output": template_decoder_output,
    }
    return rebuilt_raw, report, xbox_directory, rebuilt_data_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-forge", required=True, type=Path)
    parser.add_argument("--pc-forge", required=True, type=Path)
    parser.add_argument("--entry-name", action="append", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--pc-raw-script", required=True, type=Path)
    parser.add_argument("--compress-script", required=True, type=Path)
    parser.add_argument(
        "--append",
        action="store_true",
        help="append rebuilt entries instead of retaining physical offsets",
    )
    parser.add_argument(
        "--optimize-all-chunks",
        action="store_true",
        help="recompress unchanged data chunks and retain any smaller result",
    )
    parser.add_argument(
        "--optimize-unchanged-limit",
        type=int,
        help=(
            "when optimizing all chunks, retain only this many of the "
            "largest unchanged-chunk savings"
        ),
    )
    parser.add_argument(
        "--slot-stable",
        action="store_true",
        help="preserve every localization record offset and original length",
    )
    parser.add_argument(
        "--resource-name",
        action="append",
        help=(
            "patch only the named MGB resource; may be repeated. "
            "By default every paired MGB is considered"
        ),
    )
    parser.add_argument("--template-forge", type=Path)
    parser.add_argument("--template-entry-name")
    parser.add_argument(
        "--target-offset",
        action="append",
        default=[],
        metavar="ENTRY=OFFSET",
        help="place an entry in an existing physical FILEDATA slot",
    )
    parser.add_argument(
        "--mirror-offset",
        action="append",
        default=[],
        metavar="ENTRY=OFFSET",
        help=(
            "copy the indexed source payload byte-for-byte into a legacy "
            "physical slot without changing the rebuilt FORGE index"
        ),
    )
    parser.add_argument(
        "--truncate-size",
        type=lambda value: int(value, 0),
        help="truncate obsolete appended data after retargeting entries",
    )
    args = parser.parse_args()
    if bool(args.template_forge) != bool(args.template_entry_name):
        parser.error(
            "--template-forge and --template-entry-name must be used together"
        )
    target_offsets: dict[str, int] = {}
    for specification in args.target_offset:
        name, separator, raw_offset = specification.rpartition("=")
        if not separator or not name:
            parser.error(f"invalid --target-offset value: {specification!r}")
        target_offsets[name] = int(raw_offset, 0)
    mirror_offsets: dict[str, int] = {}
    for specification in args.mirror_offset:
        name, separator, raw_offset = specification.rpartition("=")
        if not separator or not name:
            parser.error(f"invalid --mirror-offset value: {specification!r}")
        mirror_offsets[name] = int(raw_offset, 0)

    args.output.mkdir(parents=True, exist_ok=True)
    replacements: dict[str, bytes] = {}
    entry_reports: dict[str, object] = {}
    expected_prefixes: dict[str, tuple[bytes, bytes]] = {}
    with tempfile.TemporaryDirectory(
        prefix="ac1_all_mgb_", dir=args.output
    ) as temporary_name:
        temporary = Path(temporary_name)
        for sequence, name in enumerate(args.entry_name):
            print(f"PATCH {sequence + 1}/{len(args.entry_name)} {name}", flush=True)
            xbox_raw = forge_entry_payload(args.source_forge, name)
            pc_raw = forge_entry_payload(args.pc_forge, name)
            template_raw = (
                forge_entry_payload(
                    args.template_forge, args.template_entry_name
                )
                if args.template_forge
                else None
            )
            rebuilt, report, directory, data = rebuild_entry(
                name=name,
                xbox_raw=xbox_raw,
                pc_raw=pc_raw,
                decoder=args.decoder,
                quickbms=args.quickbms,
                pc_raw_script=args.pc_raw_script,
                compress_script=args.compress_script,
                temporary=temporary,
                optimize_all_chunks=args.optimize_all_chunks,
                optimize_unchanged_limit=args.optimize_unchanged_limit,
                slot_stable=args.slot_stable,
                template_raw=template_raw,
                selected_resources=(
                    set(args.resource_name) if args.resource_name else None
                ),
            )
            replacements[name] = rebuilt
            entry_reports[name] = report
            expected_prefixes[name] = (directory, data)

        output_forge = args.output / args.source_forge.name
        if target_offsets:
            missing = set(target_offsets) - set(replacements)
            if missing:
                parser.error(
                    "--target-offset names are not selected entries: "
                    + ", ".join(sorted(missing))
                )
            forge_report = replace_forge_entries_at_offsets(
                args.source_forge,
                output_forge,
                replacements,
                target_offsets,
            )
        else:
            replacement_method = (
                replace_forge_entries
                if args.append
                else replace_forge_entries_inplace
            )
            forge_report = replacement_method(
                args.source_forge, output_forge, replacements
            )
        mirror_report = (
            mirror_indexed_entries_at_offsets(
                args.source_forge,
                output_forge,
                mirror_offsets,
            )
            if mirror_offsets
            else {}
        )
        if args.truncate_size is not None:
            highest_end = max(
                entry.offset + 440 + entry.size
                for entry in parse_forge(output_forge).entries
            )
            if args.truncate_size < highest_end:
                raise ValueError(
                    f"truncate size {args.truncate_size} would cut an indexed "
                    f"entry ending at {highest_end}"
                )
            with output_forge.open("r+b") as stream:
                stream.truncate(args.truncate_size)

        verification: dict[str, object] = {}
        for sequence, name in enumerate(args.entry_name):
            print(
                f"VERIFY {sequence + 1}/{len(args.entry_name)} {name}",
                flush=True,
            )
            final_raw = forge_entry_payload(output_forge, name)
            decoded, returncode, output = decode_xbox_prefix(
                final_raw, args.decoder, temporary, f"verify_{sequence}"
            )
            actual_directory = (decoded / "0.dat").read_bytes()
            actual_data = (decoded / "1.dat").read_bytes()
            expected_directory, expected_data = expected_prefixes[name]
            if actual_directory != expected_directory:
                raise AssertionError(f"{name}: final directory differs")
            if actual_data[: len(expected_data)] != expected_data:
                raise AssertionError(f"{name}: final decoded prefix differs")
            verification[name] = {
                "decoder_returncode": returncode,
                "decoded_prefix_size": len(actual_data),
                "patched_prefix_byte_exact": True,
                "decoder_output": output,
            }

    report = {
        "mode": "all-paired-mgb-text-fixed-extent",
        "source": str(args.source_forge),
        "pc_source": str(args.pc_forge),
        "entries": entry_reports,
        "forge_replacement": forge_report,
        "legacy_physical_mirrors": mirror_report,
        "verification": verification,
        "file": {
            "name": output_forge.name,
            "size": output_forge.stat().st_size,
            "sha256": sha256_path(output_forge),
        },
    }
    (args.output / f"{args.source_forge.stem}_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["file"], ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
