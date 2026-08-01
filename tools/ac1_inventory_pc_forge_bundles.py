#!/usr/bin/env python3
"""Inventory embedded resources in PC Assassin's Creed FORGE entries."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ac1_forge import parse_forge
from ac1_resource_bundle import parse_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("forge", type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--raw-script", required=True, type=Path)
    parser.add_argument("--resource-name")
    parser.add_argument("--type-id", type=lambda value: int(value, 0))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stop-after-first", action="store_true")
    args = parser.parse_args()

    archive = parse_forge(args.forge)
    found: list[dict[str, object]] = []
    temporary_parent = args.output.parent if args.output else args.forge.parent
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="ac1_pc_inventory_", dir=temporary_parent
    ) as temporary:
        root = Path(temporary)
        with args.forge.open("rb") as stream:
            for number, entry in enumerate(archive.entries):
                if entry.size < 1000:
                    continue
                if number % 50 == 0:
                    print(
                        f"scanning {number}/{len(archive.entries)}: {entry.name}",
                        file=sys.stderr,
                    )
                stream.seek(entry.offset + 440)
                raw = stream.read(entry.size)
                if len(raw) != entry.size:
                    continue
                raw_path = root / f"{entry.index:05d}.bin"
                output_parent = root / f"d_{entry.index:05d}"
                output_parent.mkdir()
                raw_path.write_bytes(raw)
                completed = subprocess.run(
                    [
                        str(args.quickbms),
                        "-Q",
                        "-o",
                        str(args.raw_script),
                        str(raw_path),
                        str(output_parent),
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                raw_path.unlink()
                decoded = output_parent / f"{entry.index:05d}"
                directory = decoded / "0.dat"
                data = decoded / "1.dat"
                if completed.returncode == 0 and directory.exists() and data.exists():
                    try:
                        resources = parse_bundle(directory, data, "little")
                    except ValueError:
                        resources = []
                    matches = [
                        resource
                        for resource in resources
                        if (
                            args.resource_name is None
                            or resource.name == args.resource_name
                        )
                        and (
                            args.type_id is None
                            or resource.type_id == args.type_id
                        )
                    ]
                    if matches:
                        found.append(
                            {
                                "entry_index": entry.index,
                                "entry_name": entry.name,
                                "entry_size": entry.size,
                                "resources": [
                                    {
                                        "index": resource.index,
                                        "name": resource.name,
                                        "type_id": f"0x{resource.type_id:08X}",
                                        "size": resource.size,
                                    }
                                    for resource in matches
                                ],
                            }
                        )
                        if args.stop_after_first:
                            shutil.rmtree(output_parent, ignore_errors=True)
                            break
                shutil.rmtree(output_parent, ignore_errors=True)

    if args.output:
        args.output.write_text(
            json.dumps(found, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(found, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
