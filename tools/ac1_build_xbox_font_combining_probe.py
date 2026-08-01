#!/usr/bin/env python3
"""Build an exact-size AC1 Xbox combining-caron font probe.

The retail Xbox GUI fonts lack Czech caron glyphs.  Rather than changing a
texture resource, this probe crops and vertically flips only the circumflex
strip from the existing precomposed e-circumflex glyph
into an unused currency-sign record, turns it into a zero-advance overlay, and
maps U+010D/U+011B to that record. Czech text places this zero-advance marker
immediately before its base letter. This avoids the large negative x offset
needed by a trailing combining mark and leaves the base letter's normal
advance and spacing intact. All serialized objects and decompressed streams
retain their size.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import tempfile
from pathlib import Path

from ac1_build_gui_forge import replace_forge_entry, wrapper_source_chunks, xmemlzx_wrapper
from ac1_forge import parse_forge
from ac1_resource_bundle import parse_bundle


FONT_TYPE = 0x3CDF1895
TARGET_FONTS = ("AnimusText Bold_360", "Animus Title", "AnimusSmallText 360")
PIXMAP_MARKER = b"PixmapFont"
GLYPH_RECORD_SIZE = 30
CARON_SOURCE = 0x00EA  # ê
SPARE = 0x00A4  # currency sign, unused by the Czech localization
MARKERS = (0x010D, 0x011B)


def patch_font(blob: bytes, name: str) -> tuple[bytes, list[dict[str, int]]]:
    output = bytearray(blob)
    markers: list[int] = []
    cursor = 0
    while True:
        marker = blob.find(PIXMAP_MARKER, cursor)
        if marker < 0:
            break
        markers.append(marker)
        cursor = marker + len(PIXMAP_MARKER)
    if len(markers) != 5:
        raise ValueError(f"{name}: expected five PixmapFont objects")

    changes: list[dict[str, int]] = []
    for locale, marker in enumerate(markers):
        header = marker + len(PIXMAP_MARKER)
        glyph_count = int.from_bytes(blob[header : header + 2], "little")
        records = header + 2
        indices: dict[int, int] = {}
        for index in range(glyph_count):
            record = records + index * GLYPH_RECORD_SIZE
            indices[int.from_bytes(blob[record : record + 2], "little")] = index
        if CARON_SOURCE not in indices or ord("e") not in indices or SPARE not in indices:
            raise ValueError(f"{name} locale {locale}: ê, base or spare glyph missing")

        caron_record = records + indices[CARON_SOURCE] * GLYPH_RECORD_SIZE
        base_record = records + indices[ord("e")] * GLYPH_RECORD_SIZE
        spare_record = records + indices[SPARE] * GLYPH_RECORD_SIZE
        replacement = bytearray(blob[caron_record : caron_record + GLYPH_RECORD_SIZE])
        replacement[0:2] = SPARE.to_bytes(2, "little")
        # Tuple: codepoint, width, height, x offset, y offset, advance,
        # four UV floats, flags. Crop only the accent extension above the base
        # e from ê, flip that UV strip vertically, and overlay it over the
        # preceding base letter.
        source_width = struct.unpack_from("<h", replacement, 2)[0]
        source_height = struct.unpack_from("<h", replacement, 4)[0]
        source_x_offset = struct.unpack_from("<h", replacement, 6)[0]
        source_y_offset = struct.unpack_from("<h", replacement, 8)[0]
        base_y_offset = struct.unpack_from("<h", blob, base_record + 8)[0]
        width = source_width
        height = max(2, source_y_offset - base_y_offset)
        y_offset = source_y_offset
        # The marker precedes the base character, so both glyphs start at the
        # same pen position. Its native x offset centers the accent exactly as
        # in e-circumflex, and zero advance preserves all normal base spacing.
        x_offset = source_x_offset
        overlay_advance = 0
        struct.pack_into("<h", replacement, 2, width)
        struct.pack_into("<h", replacement, 4, height)
        struct.pack_into("<h", replacement, 6, x_offset)
        struct.pack_into("<h", replacement, 8, y_offset)
        struct.pack_into("<h", replacement, 10, overlay_advance)
        u0, v0, u1, v1 = struct.unpack_from("<ffff", replacement, 12)
        accent_v_span = (v1 - v0) * height / source_height
        struct.pack_into("<ffff", replacement, 12, u0, v0 + accent_v_span, u1, v0)
        output[spare_record : spare_record + GLYPH_RECORD_SIZE] = replacement

        page_zero = records + glyph_count * GLYPH_RECORD_SIZE
        page_one_marker = page_zero + 512
        if blob[page_one_marker : page_one_marker + 2] != b"\x00\x01":
            raise ValueError(f"{name} locale {locale}: page-one marker missing")
        page_one = page_one_marker + 1
        for codepoint in MARKERS:
            cell = page_one + (codepoint & 0xFF) * 2
            old_index = int.from_bytes(blob[cell : cell + 2], "big")
            output[cell : cell + 2] = indices[SPARE].to_bytes(2, "big")
            changes.append({
                "locale": locale,
                "codepoint": codepoint,
                "old_glyph_index": old_index,
                "new_glyph_index": indices[SPARE],
                "overlay_width": width,
                "overlay_height": height,
                "overlay_x_offset": x_offset,
                "overlay_y_offset": y_offset,
                "overlay_advance": overlay_advance,
                "source_glyph": "U+00EA",
            })

    if len(output) != len(blob):
        raise AssertionError(f"{name}: font size changed")
    return bytes(output), changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xbox-directory", required=True, type=Path)
    parser.add_argument("--xbox-data", required=True, type=Path)
    parser.add_argument("--source-forge", required=True, type=Path)
    parser.add_argument("--output-forge", required=True, type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--compress-script", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    resources = parse_bundle(args.xbox_directory, args.xbox_data, "big")
    source_data = args.xbox_data.read_bytes()
    rebuilt_data = bytearray()
    report_changes: dict[str, list[dict[str, int]]] = {}
    found: set[str] = set()
    for resource in resources:
        blob = source_data[resource.offset : resource.offset + resource.size]
        if resource.type_id == FONT_TYPE and resource.name in TARGET_FONTS:
            blob, changes = patch_font(blob, resource.name)
            report_changes[resource.name] = changes
            found.add(resource.name)
        rebuilt_data += blob
    if found != set(TARGET_FONTS):
        raise ValueError(f"font resources missing: {sorted(set(TARGET_FONTS) - found)}")
    if len(rebuilt_data) != len(source_data):
        raise AssertionError("decompressed resource stream size changed")

    archive = parse_forge(args.source_forge)
    entry = next(item for item in archive.entries if item.name == "Game Bootstrap Settings")
    with args.source_forge.open("rb") as stream:
        stream.seek(entry.offset + 440)
        source_raw = stream.read(entry.size)
    source_chunks = wrapper_source_chunks(source_raw)
    if len(source_chunks) != 2:
        raise ValueError("expected two Game Bootstrap Settings wrappers")

    args.output_forge.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ac1_font_combining_", dir=args.output_forge.parent) as temporary:
        root = Path(temporary)
        raw_payload = xmemlzx_wrapper(
            args.xbox_directory.read_bytes(), args.quickbms, args.compress_script,
            root, "directory", source_chunks[0]
        ) + xmemlzx_wrapper(
            bytes(rebuilt_data), args.quickbms, args.compress_script,
            root, "data", source_chunks[1]
        )
    old_size, new_size = replace_forge_entry(
        args.source_forge, args.output_forge, "Game Bootstrap Settings", raw_payload
    )
    report = {
        "mode": "same-size-combining-caron-probe",
        "spare_codepoint": f"U+{SPARE:04X}",
        "marker_codepoints": [f"U+{cp:04X}" for cp in MARKERS],
        "changes": report_changes,
        "decompressed_data_size": len(rebuilt_data),
        "original_raw_payload_size": old_size,
        "rebuilt_raw_payload_size": new_size,
        "output_forge_size": args.output_forge.stat().st_size,
        "output_forge_sha256": hashlib.sha256(args.output_forge.read_bytes()).hexdigest().upper(),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
