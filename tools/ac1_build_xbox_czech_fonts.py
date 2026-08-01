#!/usr/bin/env python3
"""Build complete native Czech GUI glyphs for Assassin's Creed Xbox 360.

The retail fonts contain the Western Czech glyphs (acute accents, S/Z with a
caron), but not C/D/E/N/R/T with a caron nor U with a ring. This builder keeps
all resource sizes unchanged. It repurposes otherwise unused C1-control glyph
slots (and rare non-Czech Latin-1 slots only when necessary), composes complete
glyph bitmaps from the font's own base letters and accents, patches the sparse
Unicode lookup pages, and rewrites the tiled Xenon DXT3 atlases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

from ac1_build_gui_forge import replace_forge_entry, wrapper_source_chunks, xmemlzx_wrapper
from ac1_forge import parse_forge
from ac1_resource_bundle import parse_bundle
from gow_x360_font_tool import decode_dxt3, endian_transform, tile_blocks, untile_blocks


FONT_TYPE = 0x3CDF1895
TEXTURE_TYPE = 0xA2B7E917
PIXMAP_MARKER = b"PixmapFont"
COMPILED_TEXTURE_MAP = bytes.fromhex("13 23 7F E9")
GLYPH_RECORD_SIZE = 30
XENON_TEXTURE_HEADER_SIZE = 0x58

FONT_TEXTURES = {
    "AnimusText Bold_360": "AnimusText Bold_360 tga32_Map",
    "Animus Title": "AnimusTitle Thin 74 tga32_Map",
    "AnimusSmallText 360": "AnimusSmallText 360 tga32_Map",
}

CARON_TARGETS = {
    0x010C: ord("C"), 0x010D: ord("c"),
    0x010E: ord("D"),
    0x011A: ord("E"), 0x011B: ord("e"),
    0x0147: ord("N"), 0x0148: ord("n"),
    0x0158: ord("R"), 0x0159: ord("r"),
    0x0164: ord("T"),
}
SPECIAL_TARGETS = {0x010F: ord("d"), 0x0165: ord("t")}
RING_TARGETS = {0x016E: ord("U"), 0x016F: ord("u")}
TARGET_CODEPOINTS = tuple(CARON_TARGETS | SPECIAL_TARGETS | RING_TARGETS)

# C1 records are duplicate Windows-1252 compatibility glyphs with independent
# atlas rectangles. They are never emitted by valid Czech Unicode. Rare
# non-Czech Latin-1 glyphs provide a few additional tall rectangles needed by
# the large title font.
DONOR_POOL = tuple(range(0x80, 0xA0)) + (
    0x00C0, 0x00C2, 0x00C3, 0x00C4, 0x00C5, 0x00C6, 0x00C7,
    0x00C8, 0x00CA, 0x00CB, 0x00CC, 0x00CE, 0x00CF, 0x00D0,
    0x00D1, 0x00D2, 0x00D4, 0x00D5, 0x00D6, 0x00D8, 0x00D9,
    0x00DB, 0x00DC, 0x00DE, 0x00DF,
    0x00E0, 0x00E2, 0x00E3, 0x00E4, 0x00E5, 0x00E6, 0x00E7,
    0x00E8, 0x00EA, 0x00EB, 0x00EC, 0x00EE, 0x00F0, 0x00F1,
    0x00F2, 0x00F4, 0x00F5, 0x00F6, 0x00F8, 0x00F9, 0x00FB,
    0x00FC, 0x00FE,
)


@dataclass(frozen=True)
class Glyph:
    codepoint: int
    index: int
    width: int
    height: int
    x_offset: int
    y_offset: int
    advance: int
    u0: float
    v0: float
    u1: float
    v1: float
    flags: int


@dataclass
class ComposedGlyph:
    codepoint: int
    image: Image.Image
    width: int
    height: int
    x_offset: int
    y_offset: int
    advance: int
    donor_codepoint: int | None = None


def find_pixmap_tables(blob: bytes, name: str) -> list[tuple[int, int, int, dict[int, Glyph]]]:
    markers: list[int] = []
    cursor = 0
    while True:
        marker = blob.find(PIXMAP_MARKER, cursor)
        if marker < 0:
            break
        markers.append(marker)
        cursor = marker + len(PIXMAP_MARKER)
    if len(markers) != 5:
        raise ValueError(f"{name}: expected five PixmapFont objects, got {len(markers)}")

    tables = []
    for marker in markers:
        header = marker + len(PIXMAP_MARKER)
        count = int.from_bytes(blob[header : header + 2], "little")
        records = header + 2
        glyphs: dict[int, Glyph] = {}
        for index in range(count):
            offset = records + index * GLYPH_RECORD_SIZE
            values = struct.unpack_from("<hhhhhhffffH", blob, offset)
            glyph = Glyph(*values[:1], index, *values[1:])
            glyphs[glyph.codepoint] = glyph
        tables.append((records, count, records + count * GLYPH_RECORD_SIZE, glyphs))
    return tables


def texture_layout(blob: bytes, expected_name: str) -> tuple[int, int, int, int]:
    marker = blob.find(COMPILED_TEXTURE_MAP)
    if marker < 0:
        raise ValueError(f"{expected_name}: CompiledTextureMap marker missing")
    compiled_length = int.from_bytes(blob[marker + 4 : marker + 8], "big")
    surface = marker + 8
    fetch = surface + 0x20
    dword0, dword1, dword2 = struct.unpack_from(">III", blob, fetch)
    width = (dword2 & 0x1FFF) + 1
    height = ((dword2 >> 13) & 0x1FFF) + 1
    pitch = ((dword0 >> 22) & 0x1FF) << 5
    tiled = (dword0 >> 31) & 1
    texture_format = dword1 & 0x3F
    texture_endian = (dword1 >> 6) & 3
    if pitch != width or not tiled or texture_format != 19 or texture_endian != 1:
        raise ValueError(
            f"{expected_name}: unexpected Xenon fetch "
            f"{width}x{height}, pitch={pitch}, tiled={tiled}, "
            f"format={texture_format}, endian={texture_endian}"
        )
    texture_size = width * height  # 8 bpp DXT3
    if compiled_length != XENON_TEXTURE_HEADER_SIZE + texture_size:
        raise ValueError(f"{expected_name}: unexpected compiled texture length")
    texture_offset = surface + XENON_TEXTURE_HEADER_SIZE
    if texture_offset + texture_size > len(blob):
        raise ValueError(f"{expected_name}: texture payload is truncated")
    return texture_offset, texture_size, width, height


def decode_uv_alpha(blob: bytes, name: str) -> tuple[Image.Image, bytes, tuple[int, int, int, int]]:
    layout = texture_layout(blob, name)
    offset, size, width, height = layout
    tiled = blob[offset : offset + size]
    linear_guest = untile_blocks(tiled, width, height)
    linear_host = endian_transform(linear_guest, "swap16")
    alpha_gpu = decode_dxt3(linear_host, width, height).getchannel("A")
    # PixmapFont UV coordinates use a bottom-left origin.
    alpha_uv = alpha_gpu.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return alpha_uv, linear_host, layout


def glyph_origin(glyph: Glyph, width: int, height: int) -> tuple[int, int]:
    return round(glyph.u0 * width - 0.5), round(glyph.v0 * height - 0.5)


def glyph_image(alpha_uv: Image.Image, glyph: Glyph) -> Image.Image:
    x, y = glyph_origin(glyph, *alpha_uv.size)
    return alpha_uv.crop((x, y, x + glyph.width, y + glyph.height))


def accent_band(alpha_uv: Image.Image, accented: Glyph, plain: Glyph) -> Image.Image:
    extension = accented.y_offset - plain.y_offset
    if extension <= 0:
        raise ValueError("accented source has no top extension")
    band = glyph_image(alpha_uv, accented).crop((0, 0, accented.width, extension))
    bbox = band.getbbox()
    if bbox is None:
        raise ValueError("accent band is empty")
    # Remove unused top/side padding, but keep the original bottom gap between
    # the accent and the base glyph.
    return band.crop((bbox[0], bbox[1], bbox[2], extension))


def standalone_accent(alpha_uv: Image.Image, glyph: Glyph) -> Image.Image:
    """Return the complete bitmap of a spacing accent glyph.

    The caron in Š/š overlaps the top of the letter.  Consequently, taking
    only the vertical extension above S/s cuts off the lower half of the
    caron.  U+02C7 is the same native caron as a complete standalone glyph.
    """
    image = glyph_image(alpha_uv, glyph)
    bbox = image.getbbox()
    if bbox is None:
        raise ValueError("standalone accent is empty")
    return image.crop(bbox)


def paste_max(canvas: Image.Image, sprite: Image.Image, xy: tuple[int, int]) -> None:
    layer = Image.new("L", canvas.size, 0)
    layer.paste(sprite, xy)
    canvas.paste(ImageChops.lighter(canvas, layer))


def compose_standard(
    alpha_uv: Image.Image, glyphs: dict[int, Glyph], target: int, base_cp: int,
    accent_cp: int, accent_base_cp: int, extra_gap: bool = True,
) -> ComposedGlyph:
    base = glyphs[base_cp]
    accent = accent_band(alpha_uv, glyphs[accent_cp], glyphs[accent_base_cp])
    # Keep the accent optically clear of the letter body. The original source
    # band is tightly cropped, so without this font-size-aware gap the caron
    # can touch or disappear behind the upper stroke on the Xbox renderer.
    gap = max(2, round(base.height / 18)) if extra_gap else 0
    extension = accent.height + gap
    image = Image.new("L", (base.width, base.height + extension), 0)
    paste_max(image, glyph_image(alpha_uv, base), (0, extension))
    paste_max(image, accent, ((base.width - accent.width) // 2, 0))
    return ComposedGlyph(
        target, image, image.width, image.height, base.x_offset,
        base.y_offset + extension, base.advance,
    )


def compose_caron(
    alpha_uv: Image.Image, glyphs: dict[int, Glyph], target: int, base_cp: int,
) -> ComposedGlyph:
    base = glyphs[base_cp]
    upper = chr(base_cp).isupper()
    source_accented = glyphs[0x0160 if upper else 0x0161]
    source_plain = glyphs[ord("S") if upper else ord("s")]
    extension = source_accented.y_offset - source_plain.y_offset
    if extension <= 0:
        raise ValueError("native caron source has no top extension")

    # Use the whole native caron.  It intentionally overlaps the top few
    # rows of the base letter, exactly as it does in the retail Š/š glyph.
    accent = standalone_accent(alpha_uv, glyphs[0x02C7])
    image = Image.new("L", (base.width, base.height + extension), 0)
    paste_max(image, glyph_image(alpha_uv, base), (0, extension))
    # The standalone spacing caron is optically centred a little to the left
    # and sits higher than the same shape embedded in Czech letters.  Nudge it
    # by one atlas pixel in the regular fonts (two in the large title font).
    horizontal_nudge = max(1, round(base.height / 28))
    vertical_nudge = max(2, round(base.height / 14))
    paste_max(
        image,
        accent,
        (
            (base.width - accent.width) // 2 + horizontal_nudge,
            vertical_nudge,
        ),
    )
    return ComposedGlyph(
        target, image, image.width, image.height, base.x_offset,
        base.y_offset + extension, base.advance,
    )


def compose_special(
    alpha_uv: Image.Image, glyphs: dict[int, Glyph], target: int, base_cp: int,
) -> ComposedGlyph:
    base = glyphs[base_cp]
    quote = glyphs[0x2019]
    quote_image = glyph_image(alpha_uv, quote)
    bbox = quote_image.getbbox()
    if bbox is None:
        raise ValueError("right quote glyph is empty")
    quote_image = quote_image.crop(bbox)
    scale = base.height / 23.0
    if base_cp == ord("d"):
        width_extra = max(2, round(4 * scale))
        advance_extra = max(2, round(4 * scale))
    else:
        width_extra = max(1, round(3 * scale))
        advance_extra = max(1, round(1 * scale))
    y_extra = max(1, round(scale))
    width = base.width + width_extra
    height = base.height + y_extra
    y_offset = base.y_offset + y_extra
    image = Image.new("L", (width, height), 0)
    paste_max(image, glyph_image(alpha_uv, base), (0, y_extra))
    quote_y = max(0, y_offset - quote.y_offset + bbox[1])
    paste_max(image, quote_image, (width - quote_image.width, quote_y))
    return ComposedGlyph(
        target, image, width, height, base.x_offset, y_offset,
        base.advance + advance_extra,
    )


def compose_targets(alpha_uv: Image.Image, glyphs: dict[int, Glyph]) -> dict[int, ComposedGlyph]:
    result: dict[int, ComposedGlyph] = {}
    for target, base in CARON_TARGETS.items():
        result[target] = compose_caron(alpha_uv, glyphs, target, base)
    for target, base in RING_TARGETS.items():
        upper = chr(base).isupper()
        result[target] = compose_standard(
            alpha_uv, glyphs, target, base,
            0x00C5 if upper else 0x00E5,
            ord("A") if upper else ord("a"),
            extra_gap=False,
        )
    for target, base in SPECIAL_TARGETS.items():
        result[target] = compose_special(alpha_uv, glyphs, target, base)
    return result


def assign_donors(composed: dict[int, ComposedGlyph], glyphs: dict[int, Glyph]) -> None:
    available = [glyphs[cp] for cp in DONOR_POOL if cp in glyphs and glyphs[cp].index != 0]
    used: set[int] = set()
    # Allocate the hardest rectangles first.
    targets = sorted(composed.values(), key=lambda item: (item.height, item.width, item.width * item.height), reverse=True)
    for item in targets:
        fitting = [
            donor for donor in available
            if donor.codepoint not in used and donor.width >= item.width and donor.height >= item.height
        ]
        if not fitting:
            raise ValueError(
                f"no donor slot fits U+{item.codepoint:04X} "
                f"({item.width}x{item.height})"
            )
        # Prefer C1 slots, then minimize wasted area and dimensions.
        donor = min(
            fitting,
            key=lambda value: (
                0 if 0x80 <= value.codepoint <= 0x9F else 1,
                value.width * value.height - item.width * item.height,
                value.height - item.height,
                value.width - item.width,
            ),
        )
        item.donor_codepoint = donor.codepoint
        used.add(donor.codepoint)


def patch_font_and_alpha(
    font_blob: bytes, font_name: str, alpha_uv: Image.Image,
) -> tuple[bytes, Image.Image, list[dict[str, object]]]:
    tables = find_pixmap_tables(font_blob, font_name)
    locale_zero = tables[0][3]
    required = set(CARON_TARGETS.values()) | set(RING_TARGETS.values()) | set(SPECIAL_TARGETS.values()) | {
        0x0160, 0x0161, ord("S"), ord("s"), 0x00C5, 0x00E5,
        ord("A"), ord("a"), 0x2019,
    }
    missing = sorted(required - set(locale_zero))
    if missing:
        raise ValueError(f"{font_name}: source glyphs missing: {[hex(cp) for cp in missing]}")

    composed = compose_targets(alpha_uv, locale_zero)
    assign_donors(composed, locale_zero)
    output_font = bytearray(font_blob)
    output_alpha = alpha_uv.copy()
    report: list[dict[str, object]] = []

    # Patch pixels once, using the locale-zero atlas rectangles.
    for target in sorted(composed):
        item = composed[target]
        assert item.donor_codepoint is not None
        donor = locale_zero[item.donor_codepoint]
        x, y = glyph_origin(donor, *output_alpha.size)
        # Clear the full donor glyph and its one-pixel UV pad.
        output_alpha.paste(0, (x, y, x + donor.width + 1, y + donor.height + 1))
        output_alpha.paste(item.image, (x, y))

    # Patch all five locale objects identically while retaining each object's
    # own donor UV position.
    for locale, (records, count, page_zero, glyphs) in enumerate(tables):
        for target in sorted(composed):
            item = composed[target]
            assert item.donor_codepoint is not None
            donor = glyphs[item.donor_codepoint]
            if donor.index >= count:
                raise AssertionError("donor index outside glyph table")
            x, y = glyph_origin(donor, *output_alpha.size)
            u0, v0 = donor.u0, donor.v0
            u1 = u0 + (item.width + 1) / output_alpha.width
            v1 = v0 + (item.height + 1) / output_alpha.height
            if u1 > donor.u1 + 1e-6 or v1 > donor.v1 + 1e-6:
                raise ValueError(f"{font_name}: U+{target:04X} exceeds donor UV rectangle")
            replacement = struct.pack(
                "<hhhhhhffffH", target, item.width, item.height,
                item.x_offset, item.y_offset, item.advance,
                u0, v0, u1, v1, donor.flags,
            )
            record = records + donor.index * GLYPH_RECORD_SIZE
            output_font[record : record + GLYPH_RECORD_SIZE] = replacement

            page_one_marker = page_zero + 512
            if font_blob[page_one_marker : page_one_marker + 2] != b"\x00\x01":
                raise ValueError(f"{font_name} locale {locale}: page one missing")
            page_one = page_one_marker + 1
            cell = page_one + (target & 0xFF) * 2
            old_index = int.from_bytes(font_blob[cell : cell + 2], "big")
            output_font[cell : cell + 2] = donor.index.to_bytes(2, "big")
            if locale == 0:
                report.append({
                    "codepoint": f"U+{target:04X}",
                    "character": chr(target),
                    "donor_codepoint": f"U+{item.donor_codepoint:04X}",
                    "donor_index": donor.index,
                    "old_lookup_index": old_index,
                    "size": [item.width, item.height],
                    "offset": [item.x_offset, item.y_offset],
                    "advance": item.advance,
                    "atlas_xy": [x, y],
                })

    if len(output_font) != len(font_blob):
        raise AssertionError(f"{font_name}: font resource size changed")
    return bytes(output_font), output_alpha, report


def encode_uv_alpha(
    texture_blob: bytes, name: str, alpha_uv: Image.Image, linear_host: bytes,
    layout: tuple[int, int, int, int],
) -> bytes:
    offset, size, width, height = layout
    if alpha_uv.size != (width, height):
        raise ValueError("alpha atlas size changed")
    alpha_gpu = alpha_uv.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    pixels = alpha_gpu.load()
    patched_linear = bytearray(linear_host)
    width_blocks = width // 4
    height_blocks = height // 4
    for by in range(height_blocks):
        for bx in range(width_blocks):
            bits = 0
            for py in range(4):
                for px in range(4):
                    value = pixels[bx * 4 + px, by * 4 + py]
                    nibble = min(15, (value + 8) // 17)
                    bits |= nibble << ((py * 4 + px) * 4)
            block = (by * width_blocks + bx) * 16
            patched_linear[block : block + 8] = bits.to_bytes(8, "little")
    original_tiled = texture_blob[offset : offset + size]
    patched_guest = endian_transform(bytes(patched_linear), "swap16")
    patched_tiled = tile_blocks(patched_guest, original_tiled, width, height)
    output = bytearray(texture_blob)
    output[offset : offset + size] = patched_tiled
    if len(output) != len(texture_blob):
        raise AssertionError(f"{name}: texture resource size changed")
    return bytes(output)


def save_preview(path: Path, fonts: dict[str, tuple[Image.Image, list[dict[str, object]]]]) -> None:
    rows = []
    for name, (alpha, report) in fonts.items():
        row = Image.new("L", (1120, 130), 40)
        draw = ImageDraw.Draw(row)
        draw.text((8, 5), name, fill=255)
        x = 8
        for item in report:
            ax, ay = item["atlas_xy"]
            width, height = item["size"]
            glyph = alpha.crop((ax, ay, ax + width, ay + height))
            glyph.thumbnail((55, 82), Image.Resampling.NEAREST)
            row.paste(glyph, (x, 30))
            draw.text((x, 113), item["character"], fill=255)
            x += 76
        rows.append(row)
    preview = Image.new("L", (1120, 130 * len(rows)), 20)
    for index, row in enumerate(rows):
        preview.paste(row, (0, index * 130))
    path.parent.mkdir(parents=True, exist_ok=True)
    preview.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xbox-directory", required=True, type=Path)
    parser.add_argument("--xbox-data", required=True, type=Path)
    parser.add_argument(
        "--clean-font-directory",
        type=Path,
        help="optional clean bundle directory supplying only the font resources",
    )
    parser.add_argument(
        "--clean-font-data",
        type=Path,
        help="optional clean bundle data supplying only the font resources",
    )
    parser.add_argument("--source-forge", required=True, type=Path)
    parser.add_argument("--output-forge", required=True, type=Path)
    parser.add_argument("--quickbms", required=True, type=Path)
    parser.add_argument("--compress-script", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()

    resources = parse_bundle(args.xbox_directory, args.xbox_data, "big")
    source_data = args.xbox_data.read_bytes()
    by_name = {resource.name: resource for resource in resources}
    if bool(args.clean_font_directory) != bool(args.clean_font_data):
        raise ValueError(
            "--clean-font-directory and --clean-font-data must be supplied together"
        )
    if args.clean_font_directory:
        font_resources = parse_bundle(
            args.clean_font_directory, args.clean_font_data, "big"
        )
        font_source_data = args.clean_font_data.read_bytes()
        font_by_name = {resource.name: resource for resource in font_resources}
    else:
        font_source_data = source_data
        font_by_name = by_name
    patched: dict[str, bytes] = {}
    report_fonts: dict[str, list[dict[str, object]]] = {}
    preview_fonts: dict[str, tuple[Image.Image, list[dict[str, object]]]] = {}

    for font_name, texture_name in FONT_TEXTURES.items():
        font_resource = font_by_name[font_name]
        texture_resource = font_by_name[texture_name]
        if font_resource.type_id != FONT_TYPE or texture_resource.type_id != TEXTURE_TYPE:
            raise ValueError(f"unexpected resource type for {font_name}")
        font_blob = font_source_data[
            font_resource.offset : font_resource.offset + font_resource.size
        ]
        texture_blob = font_source_data[
            texture_resource.offset : texture_resource.offset + texture_resource.size
        ]
        alpha_uv, linear_host, layout = decode_uv_alpha(texture_blob, texture_name)
        patched_font, patched_alpha, changes = patch_font_and_alpha(font_blob, font_name, alpha_uv)
        patched_texture = encode_uv_alpha(texture_blob, texture_name, patched_alpha, linear_host, layout)
        patched[font_name] = patched_font
        patched[texture_name] = patched_texture
        report_fonts[font_name] = changes
        preview_fonts[font_name] = (patched_alpha, changes)

    rebuilt_data = bytearray()
    for resource in resources:
        blob = source_data[resource.offset : resource.offset + resource.size]
        rebuilt_data += patched.get(resource.name, blob)
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
    with tempfile.TemporaryDirectory(prefix="ac1_czech_fonts_", dir=args.output_forge.parent) as temporary:
        root = Path(temporary)
        raw_payload = xmemlzx_wrapper(
            args.xbox_directory.read_bytes(), args.quickbms, args.compress_script,
            root, "directory", source_chunks[0],
        ) + xmemlzx_wrapper(
            bytes(rebuilt_data), args.quickbms, args.compress_script,
            root, "data", source_chunks[1],
        )
    old_size, new_size = replace_forge_entry(
        args.source_forge, args.output_forge, "Game Bootstrap Settings", raw_payload
    )

    if args.preview:
        save_preview(args.preview, preview_fonts)
    report = {
        "mode": "complete-native-czech-glyphs",
        "codepoints": [f"U+{cp:04X}" for cp in TARGET_CODEPOINTS],
        "fonts": report_fonts,
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
