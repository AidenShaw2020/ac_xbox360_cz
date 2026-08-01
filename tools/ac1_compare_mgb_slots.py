#!/usr/bin/env python3
"""Compare Xbox and Czech PC MGB strings without moving Xbox records."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from ac1_complete_text import (
    ALL_KEY_OVERRIDES,
    SLOT_STABLE_KEY_OVERRIDES,
    SLOT_STABLE_SOURCE_OVERRIDES,
    SOURCE_TEXT_OVERRIDES,
    compact_slot_text,
    first_locale,
)
from ac1_build_masyaf_text import partial_resource_map
from ac1_mgb_strings import find_chains
from ac1_resource_bundle import parse_bundle


PLACEHOLDER = re.compile(r"%\d+%|[~¤²³´µ¶·¸¹º»¼½¾¿]")


def resource_blob(
    directory: Path, data: Path, endian: str, name: str
) -> bytes:
    raw = data.read_bytes()
    try:
        resources = parse_bundle(directory, data, endian)
    except ValueError:
        if endian != "big":
            raise
        resources = list(
            partial_resource_map(directory.read_bytes(), raw).values()
        )
    matches = [resource for resource in resources if resource.name == name]
    if len(matches) != 1:
        raise ValueError(f"expected one resource named {name!r}")
    resource = matches[0]
    return raw[resource.offset : resource.offset + resource.size]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xbox-directory", required=True, type=Path)
    parser.add_argument("--xbox-data", required=True, type=Path)
    parser.add_argument("--pc-directory", required=True, type=Path)
    parser.add_argument("--pc-data", required=True, type=Path)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    xbox_blob = resource_blob(
        args.xbox_directory, args.xbox_data, "big", args.resource
    )
    pc_blob = resource_blob(
        args.pc_directory, args.pc_data, "little", args.resource
    )
    xs, xe = first_locale(xbox_blob)
    ps, pe = first_locale(pc_blob)
    xbox_chains = find_chains(xbox_blob[xs:xe], 3)
    pc_chains = find_chains(pc_blob[ps:pe], 3)
    pc_values: dict[int, str] = {}
    for chain in pc_chains:
        for entry in chain.entries:
            pc_values.setdefault(entry.key, entry.value)

    rows: list[dict[str, object]] = []
    for chain_index, xbox_chain in enumerate(xbox_chains):
        keys = [entry.key for entry in xbox_chain.entries]
        candidates = [
            chain
            for chain in pc_chains
            if [entry.key for entry in chain.entries] == keys
        ]
        # The installed PC localization puts Czech in the candidate with the
        # greatest number of non-ASCII characters. If the PC and Xbox tables
        # differ in membership, use the same stable-hash fallback as the
        # production patcher.
        pc_chain = (
            max(
                candidates,
                key=lambda chain: sum(
                    ord(character) > 127
                    for entry in chain.entries
                    for character in entry.value
                ),
            )
            if candidates
            else None
        )
        for entry_index, xbox_entry in enumerate(xbox_chain.entries):
            exact_value = (
                pc_chain.entries[entry_index].value
                if pc_chain is not None
                else pc_values.get(xbox_entry.key, xbox_entry.value)
            )
            hash_value = pc_values.get(xbox_entry.key, xbox_entry.value)
            czech_letters = set(
                "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ"
            )
            pc_value = max(
                (exact_value, hash_value),
                key=lambda value: (
                    sum(character in czech_letters for character in value),
                    value != xbox_entry.value,
                ),
            )
            target = SLOT_STABLE_SOURCE_OVERRIDES.get(
                xbox_entry.value,
                SLOT_STABLE_KEY_OVERRIDES.get(
                    xbox_entry.key,
                    SOURCE_TEXT_OVERRIDES.get(
                        xbox_entry.value,
                        ALL_KEY_OVERRIDES.get(xbox_entry.key, pc_value),
                    ),
                ),
            )
            target = compact_slot_text(target, len(xbox_entry.value))
            source_tokens = PLACEHOLDER.findall(xbox_entry.value)
            target_tokens = PLACEHOLDER.findall(target)
            rows.append(
                {
                    "chain": chain_index,
                    "index": entry_index,
                    "key": f"0x{xbox_entry.key:08X}",
                    "offset": xbox_entry.offset,
                    "source_length": len(xbox_entry.value),
                    "target_length": len(target),
                    "growth": len(target) - len(xbox_entry.value),
                    "source_tokens": source_tokens,
                    "target_tokens": target_tokens,
                    "tokens_match": source_tokens == target_tokens,
                    "source": xbox_entry.value,
                    "target": target,
                }
            )

    summary = {
        "resource": args.resource,
        "entries": len(rows),
        "overlong": sum(row["growth"] > 0 for row in rows),
        "placeholder_mismatches": sum(
            not row["tokens_match"] for row in rows
        ),
        "rows": rows,
    }
    rendered = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
