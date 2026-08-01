#!/usr/bin/env python3
"""Decode AC1 Xbox FORGE entries and inventory their embedded resources."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from ac1_forge import parse_forge
from ac1_resource_bundle import parse_bundle


RAW_SIGNATURE = bytes.fromhex("1004FA9957FBAA33")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("forge", type=Path)
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resource-name")
    parser.add_argument("--type-id", type=lambda value: int(value, 0))
    args = parser.parse_args()

    archive = parse_forge(args.forge)
    inventory: list[dict[str, object]] = []
    temporary_parent = args.output.parent if args.output else args.forge.parent
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="ac1_inventory_", dir=temporary_parent
    ) as temporary:
        root = Path(temporary)
        with args.forge.open("rb") as stream:
            for entry in archive.entries:
                stream.seek(entry.offset + 440)
                signature = stream.read(8)
                if signature != RAW_SIGNATURE:
                    continue
                stream.seek(entry.offset + 440)
                raw = stream.read(entry.size)
                raw_path = root / f"{entry.index:05d}.bin"
                decoded_parent = root / f"d_{entry.index:05d}"
                raw_path.write_bytes(raw)
                completed = subprocess.run(
                    [str(args.decoder), str(raw_path), str(decoded_parent)],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                raw_path.unlink()
                if completed.returncode != 0:
                    inventory.append(
                        {
                            "entry_index": entry.index,
                            "entry_name": entry.name,
                            "decode_error": completed.stdout.strip(),
                        }
                    )
                    shutil.rmtree(decoded_parent, ignore_errors=True)
                    continue
                decoded = decoded_parent / f"{entry.index:05d}"
                directory = decoded / "0.dat"
                data = decoded / "1.dat"
                if not directory.exists() or not data.exists():
                    shutil.rmtree(decoded_parent, ignore_errors=True)
                    continue
                try:
                    resources = parse_bundle(directory, data, "big")
                except ValueError:
                    shutil.rmtree(decoded_parent, ignore_errors=True)
                    continue
                selected = [
                    {
                        "index": resource.index,
                        "name": resource.name,
                        "type_id": f"0x{resource.type_id:08X}",
                        "size": resource.size,
                    }
                    for resource in resources
                    if not args.resource_name
                    or resource.name == args.resource_name
                ]
                if args.type_id is not None:
                    selected = [
                        resource
                        for resource in selected
                        if int(resource["type_id"], 0) == args.type_id
                    ]
                if selected:
                    inventory.append(
                        {
                            "entry_index": entry.index,
                            "entry_name": entry.name,
                            "entry_size": entry.size,
                            "resources": selected,
                        }
                    )
                shutil.rmtree(decoded_parent, ignore_errors=True)

    if args.output:
        args.output.write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(inventory, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
