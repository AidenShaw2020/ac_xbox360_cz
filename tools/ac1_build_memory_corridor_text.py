#!/usr/bin/env python3
"""Build the Czech AC1 Xbox Memory Corridor interface archive."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from ac1_build_complete_text import (
    compress_bundle,
    forge_entry_payload,
    patch_bundle,
    replace_forge_entries_inplace,
    sha256_path,
    verify_decoded_bundle,
)
from ac1_complete_text import patch_memory_paused


ENTRY_NAME = "Cell00084_DataBlock"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-forge", required=True, type=Path)
    parser.add_argument("--xbox-bundle", required=True, type=Path)
    parser.add_argument("--pc-bundle", required=True, type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--compress-script", required=True, type=Path)
    parser.add_argument("--decoder", required=True, type=Path)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    xbox_directory = args.xbox_bundle / "0.dat"
    xbox_data = args.xbox_bundle / "1.dat"
    rebuilt, text_report = patch_bundle(
        xbox_directory,
        xbox_data,
        args.pc_bundle / "0.dat",
        args.pc_bundle / "1.dat",
        {"MemoryPaused_MGB": patch_memory_paused},
    )
    source_raw = forge_entry_payload(args.source_forge, ENTRY_NAME)

    with tempfile.TemporaryDirectory(
        prefix="ac1_memory_corridor_", dir=args.output
    ) as temporary:
        raw = compress_bundle(
            xbox_directory.read_bytes(),
            rebuilt,
            source_raw,
            args.quickbms,
            args.compress_script,
            Path(temporary),
            "memory_corridor_cell84",
        )

    output_forge = args.output / "Data360_Memory_Corridor.forge"
    forge_report = replace_forge_entries_inplace(
        args.source_forge, output_forge, {ENTRY_NAME: raw}
    )
    verify_root = args.output / "verify_decode"
    verify_root.mkdir(exist_ok=True)
    verify_decoded_bundle(
        args.decoder,
        forge_entry_payload(output_forge, ENTRY_NAME),
        verify_root / "cell84",
        xbox_directory.read_bytes(),
        rebuilt,
    )
    report = {
        "mode": "xbox-native-fixed-extent-memory-corridor-czech",
        "text_patches": text_report,
        "forge_replacement": forge_report,
        "file": {
            "size": output_forge.stat().st_size,
            "sha256": sha256_path(output_forge),
        },
        "verification": "final FORGE entry decodes byte-exactly",
    }
    (args.output / "build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
