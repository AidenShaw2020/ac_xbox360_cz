#!/usr/bin/env python3
"""Find references from one AC1 resource bundle to BAOs in sibling bundles."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

from ac1_resource_bundle import parse_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles-root", required=True, type=Path)
    parser.add_argument("--target-directory", required=True, type=Path)
    parser.add_argument("--target-data", required=True, type=Path)
    parser.add_argument("--endian", choices=("little", "big"), required=True)
    args = parser.parse_args()

    target = (
        args.target_directory.read_bytes()
        + args.target_data.read_bytes()
    )
    byteorder = "<" if args.endian == "little" else ">"
    matches: list[dict[str, object]] = []
    total_baos = 0
    for child in sorted(args.bundles_root.iterdir()):
        if not child.is_dir():
            continue
        directory = child / "0.dat"
        data = child / "1.dat"
        if not directory.is_file() or not data.is_file():
            continue
        try:
            resources = parse_bundle(
                directory, data, args.endian
            )
        except (OSError, ValueError, struct.error):
            continue
        for resource in resources:
            if not resource.name.startswith("BAO_0x"):
                continue
            total_baos += 1
            ids = {
                value
                for value in (
                    resource.entry_id,
                    resource.directory_id,
                    int(resource.name[6:], 16),
                )
                if value is not None
            }
            found: dict[str, int] = {}
            for value in ids:
                needle = struct.pack(f"{byteorder}I", value)
                count = target.count(needle)
                if count:
                    found[f"{value:08X}"] = count
            if found:
                matches.append(
                    {
                        "bundle": child.name,
                        "resource": resource.name,
                        "entry_id": (
                            f"{resource.entry_id:08X}"
                            if resource.entry_id is not None
                            else None
                        ),
                        "directory_id": f"{resource.directory_id:08X}",
                        "references": found,
                    }
                )
    print(
        json.dumps(
            {
                "total_baos": total_baos,
                "matches": matches,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
