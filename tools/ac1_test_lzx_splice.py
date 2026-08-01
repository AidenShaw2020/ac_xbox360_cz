#!/usr/bin/env python3
"""Build a diagnostic AC1 raw wrapper by splicing contextual LZX ranges."""

from __future__ import annotations

import argparse
from pathlib import Path

from ac1_build_complete_text import forge_entry_payload
from ac1_build_gui_forge import wrapper_source_chunks
from ac1_build_masyaf_text import serialize_wrapper, wrapper_ranges


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-forge", required=True, type=Path)
    parser.add_argument("--entry-name", required=True)
    parser.add_argument("--contextual-wrapper", required=True, type=Path)
    parser.add_argument("--range", action="append", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    raw = forge_entry_payload(args.source_forge, args.entry_name)
    ranges = wrapper_ranges(raw)
    source = wrapper_source_chunks(raw)
    contextual = wrapper_source_chunks(
        args.contextual_wrapper.read_bytes()
    )
    if len(source) != 2 or len(contextual) != 1:
        raise ValueError("unexpected wrapper count")
    data_chunks = list(source[1])
    if len(contextual[0]) != len(data_chunks):
        raise ValueError("contextual chunk count differs")
    selected: list[int] = []
    for specification in args.range:
        first_text, end_text = specification.split(":", 1)
        first, end = int(first_text), int(end_text)
        for index in range(first, end):
            data_chunks[index] = contextual[0][index]
            selected.append(index)
    directory_start, directory_end = ranges[0]
    data_start, _data_end = ranges[1]
    rebuilt = (
        raw[directory_start:directory_end]
        + serialize_wrapper(raw[data_start : data_start + 17], data_chunks)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(rebuilt)
    print(
        f"wrote {len(rebuilt)} bytes with {len(set(selected))} "
        f"contextual chunks"
    )


if __name__ == "__main__":
    main()
