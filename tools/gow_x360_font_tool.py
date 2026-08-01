#!/usr/bin/env python3
"""Small helpers for Gears of War 1 Xbox 360 UE3 font textures.

This intentionally targets the verified Xenon packages used by the project:
big-endian UE3 exports with one LZO-compressed DXT3 mip per texture.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import struct
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps


MAGIC = 0x9E2A83C1


def load_lzo(path: Path) -> ctypes.CDLL:
    lib = ctypes.CDLL(str(path))
    dec = lib.lzo1x_decompress_safe
    dec.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
    ]
    dec.restype = ctypes.c_int
    comp = lib.lzo1x_1_compress
    comp.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
    ]
    comp.restype = ctypes.c_int
    return lib


def lzo_decompress(lib: ctypes.CDLL, data: bytes, expected: int) -> bytes:
    src = ctypes.create_string_buffer(data)
    dst = ctypes.create_string_buffer(expected)
    size = ctypes.c_size_t(expected)
    rc = lib.lzo1x_decompress_safe(src, len(data), dst, ctypes.byref(size), None)
    if rc != 0 or size.value != expected:
        raise RuntimeError(f"LZO decompression failed: rc={rc}, size={size.value}")
    return dst.raw[: size.value]


def lzo_compress(lib: ctypes.CDLL, data: bytes) -> bytes:
    capacity = len(data) + len(data) // 16 + 64 + 3
    src = ctypes.create_string_buffer(data)
    dst = ctypes.create_string_buffer(capacity)
    size = ctypes.c_size_t(capacity)
    work = ctypes.create_string_buffer(1024 * 1024)
    rc = lib.lzo1x_1_compress(src, len(data), dst, ctypes.byref(size), work)
    if rc != 0:
        raise RuntimeError(f"LZO compression failed: rc={rc}")
    result = dst.raw[: size.value]
    if lzo_decompress(lib, result, len(data)) != data:
        raise RuntimeError("LZO round-trip check failed")
    return result


def build_bulk_chunk(data: bytes, lib: ctypes.CDLL) -> bytes:
    blocks: list[tuple[bytes, int]] = []
    for offset in range(0, len(data), 0x20000):
        plain = data[offset : offset + 0x20000]
        blocks.append((lzo_compress(lib, plain), len(plain)))
    payload_size = sum(len(compressed) for compressed, _ in blocks)
    output = bytearray(
        struct.pack(">IIII", MAGIC, MAGIC, payload_size, len(data))
    )
    for compressed, plain_size in blocks:
        output += struct.pack(">II", len(compressed), plain_size)
    for compressed, _ in blocks:
        output += compressed
    return bytes(output)


def read_bulk_chunk(package: bytes, offset: int, lib: ctypes.CDLL) -> tuple[bytes, int]:
    m1, m2, compressed_size, uncompressed_size = struct.unpack_from(
        ">IIII", package, offset
    )
    if (m1, m2) != (MAGIC, MAGIC):
        raise ValueError(f"Invalid UE3 bulk magic at 0x{offset:X}")
    block_count = (uncompressed_size + 0x1FFFF) // 0x20000
    table_offset = offset + 16
    payload_offset = table_offset + block_count * 8
    output = bytearray()
    cursor = payload_offset
    payload_total = 0
    for index in range(block_count):
        block_compressed, block_uncompressed = struct.unpack_from(
            ">II", package, table_offset + index * 8
        )
        output += lzo_decompress(
            lib, package[cursor : cursor + block_compressed], block_uncompressed
        )
        cursor += block_compressed
        payload_total += block_compressed
    if payload_total != compressed_size or len(output) != uncompressed_size:
        raise ValueError("UE3 bulk chunk sizes do not match")
    return bytes(output), cursor - offset


def tiled_xy_2d(
    block_offset: int, width_blocks: int, block_bytes: int
) -> tuple[int, int]:
    """Return the linear block coordinates for an Xbox 360 tiled block.

    This follows the XGAddress2DTiledX/Y layout used by Xenon textures.  The
    previous forward-address approximation mixed byte and block coordinates,
    which kept the data round-trippable but scattered the 4x4 DXT blocks.
    """
    aligned_width = (width_blocks + 31) & ~31
    log2_bpp = (block_bytes >> 2) + (
        (block_bytes >> 1) >> (block_bytes >> 2)
    )
    offset_byte = block_offset << log2_bpp
    offset_tile = (
        ((offset_byte & ~0xFFF) >> 3)
        + ((offset_byte & 0x700) >> 2)
        + (offset_byte & 0x3F)
    )
    offset_macro = offset_tile >> (7 + log2_bpp)

    macro_x = (offset_macro % (aligned_width >> 5)) << 2
    tile_x = (((offset_tile >> (5 + log2_bpp)) & 2) + (offset_byte >> 6)) & 3
    macro_x = (macro_x + tile_x) << 3
    micro_x = (
        (
            ((offset_tile >> 1) & ~0xF)
            + (offset_tile & 0xF)
        )
        & ((block_bytes << 3) - 1)
    ) >> log2_bpp

    macro_y = (offset_macro // (aligned_width >> 5)) << 2
    tile_y = ((offset_tile >> (6 + log2_bpp)) & 1) + (
        (offset_byte & 0x800) >> 10
    )
    macro_y = (macro_y + tile_y) << 3
    micro_y = (
        (
            (offset_tile & (((block_bytes << 6) - 1) & ~0x1F))
            + ((offset_tile & 0xF) << 1)
        )
        >> (3 + log2_bpp)
    ) & ~1

    return (
        macro_x + micro_x,
        macro_y + micro_y + ((offset_tile & 0x10) >> 4),
    )


def untile_blocks(data: bytes, width: int, height: int, block_bytes: int = 16) -> bytes:
    width_blocks = (width + 3) // 4
    height_blocks = (height + 3) // 4
    output = bytearray(width_blocks * height_blocks * block_bytes)
    if len(data) % block_bytes:
        raise ValueError("Tiled texture data is not block-aligned")
    for block_offset in range(len(data) // block_bytes):
        x, y = tiled_xy_2d(block_offset, width_blocks, block_bytes)
        if x >= width_blocks or y >= height_blocks:
            continue
        source = block_offset * block_bytes
        target = (y * width_blocks + x) * block_bytes
        output[target : target + block_bytes] = data[
            source : source + block_bytes
        ]
    return bytes(output)


def tile_blocks(
    linear: bytes,
    original_tiled: bytes,
    width: int,
    height: int,
    block_bytes: int = 16,
) -> bytes:
    width_blocks = (width + 3) // 4
    height_blocks = (height + 3) // 4
    expected = width_blocks * height_blocks * block_bytes
    if len(linear) != expected:
        raise ValueError(f"Linear texture size mismatch: {len(linear)} != {expected}")
    output = bytearray(original_tiled)
    if len(output) % block_bytes:
        raise ValueError("Tiled texture data is not block-aligned")
    for block_offset in range(len(output) // block_bytes):
        x, y = tiled_xy_2d(block_offset, width_blocks, block_bytes)
        if x >= width_blocks or y >= height_blocks:
            continue
        target = block_offset * block_bytes
        source = (y * width_blocks + x) * block_bytes
        output[target : target + block_bytes] = linear[
            source : source + block_bytes
        ]
    return bytes(output)


def endian_transform(data: bytes, mode: str) -> bytes:
    if mode == "none":
        return data
    output = bytearray(len(data))
    if mode == "swap16":
        for i in range(0, len(data), 2):
            output[i : i + 2] = data[i : i + 2][::-1]
    elif mode == "swap32":
        for i in range(0, len(data), 4):
            output[i : i + 4] = data[i : i + 4][::-1]
    elif mode == "swap16in32":
        for i in range(0, len(data), 4):
            chunk = data[i : i + 4]
            output[i : i + 4] = chunk[2:4] + chunk[0:2]
    else:
        raise ValueError(f"Unknown endian mode: {mode}")
    return bytes(output)


def rgb565(value: int) -> tuple[int, int, int]:
    return (
        ((value >> 11) & 31) * 255 // 31,
        ((value >> 5) & 63) * 255 // 63,
        (value & 31) * 255 // 31,
    )


def decode_dxt3(data: bytes, width: int, height: int) -> Image.Image:
    image = Image.new("RGBA", (width, height))
    pixels = image.load()
    width_blocks = (width + 3) // 4
    height_blocks = (height + 3) // 4
    for by in range(height_blocks):
        for bx in range(width_blocks):
            block = data[(by * width_blocks + bx) * 16 :][:16]
            alpha_bits = int.from_bytes(block[:8], "little")
            c0, c1, selectors = struct.unpack_from("<HHI", block, 8)
            p0, p1 = rgb565(c0), rgb565(c1)
            colors = (
                p0,
                p1,
                tuple((2 * p0[i] + p1[i]) // 3 for i in range(3)),
                tuple((p0[i] + 2 * p1[i]) // 3 for i in range(3)),
            )
            for py in range(4):
                for px in range(4):
                    index = py * 4 + px
                    x, y = bx * 4 + px, by * 4 + py
                    if x >= width or y >= height:
                        continue
                    alpha = ((alpha_bits >> (index * 4)) & 15) * 17
                    color = colors[(selectors >> (index * 2)) & 3]
                    pixels[x, y] = (*color, alpha)
    return image


def dxt5_alpha_values(a0: int, a1: int) -> tuple[int, ...]:
    if a0 > a1:
        return (
            a0,
            a1,
            *(
                ((7 - index) * a0 + index * a1) // 7
                for index in range(1, 7)
            ),
        )
    return (
        a0,
        a1,
        *(
            ((5 - index) * a0 + index * a1) // 5
            for index in range(1, 5)
        ),
        0,
        255,
    )


def decode_dxt5(data: bytes, width: int, height: int) -> Image.Image:
    image = Image.new("RGBA", (width, height))
    pixels = image.load()
    width_blocks = (width + 3) // 4
    height_blocks = (height + 3) // 4
    for by in range(height_blocks):
        for bx in range(width_blocks):
            block = data[(by * width_blocks + bx) * 16 :][:16]
            alpha_values = dxt5_alpha_values(block[0], block[1])
            alpha_selectors = int.from_bytes(block[2:8], "little")
            c0, c1, selectors = struct.unpack_from("<HHI", block, 8)
            p0, p1 = rgb565(c0), rgb565(c1)
            colors = (
                p0,
                p1,
                tuple((2 * p0[i] + p1[i]) // 3 for i in range(3)),
                tuple((p0[i] + 2 * p1[i]) // 3 for i in range(3)),
            )
            for py in range(4):
                for px in range(4):
                    index = py * 4 + px
                    x, y = bx * 4 + px, by * 4 + py
                    if x >= width or y >= height:
                        continue
                    alpha = alpha_values[
                        (alpha_selectors >> (index * 3)) & 7
                    ]
                    color = colors[(selectors >> (index * 2)) & 3]
                    pixels[x, y] = (*color, alpha)
    return image


def replace_dxt3_alpha(
    data: bytes, alpha: Image.Image, width: int, height: int
) -> bytes:
    if alpha.mode != "L" or alpha.size != (width, height):
        raise ValueError("Alpha image must be an L image with matching dimensions")
    output = bytearray(data)
    pixels = alpha.load()
    width_blocks = (width + 3) // 4
    height_blocks = (height + 3) // 4
    # Constant light gray matching the decoded original Chrom20 atlas.
    color_565 = ((202 * 31 // 255) << 11) | ((203 * 63 // 255) << 5) | (
        202 * 31 // 255
    )
    for by in range(height_blocks):
        for bx in range(width_blocks):
            alpha_bits = 0
            has_visible = False
            for py in range(4):
                for px in range(4):
                    x, y = bx * 4 + px, by * 4 + py
                    value = 0 if x >= width or y >= height else pixels[x, y]
                    nibble = min(15, (value + 8) // 17)
                    has_visible |= nibble != 0
                    alpha_bits |= nibble << ((py * 4 + px) * 4)
            offset = (by * width_blocks + bx) * 16
            output[offset : offset + 8] = alpha_bits.to_bytes(8, "little")
            if has_visible:
                output[offset + 8 : offset + 16] = struct.pack(
                    "<HHI", color_565, color_565, 0
                )
    return bytes(output)


def replace_dxt5_alpha(
    data: bytes, alpha: Image.Image, width: int, height: int
) -> bytes:
    if alpha.mode != "L" or alpha.size != (width, height):
        raise ValueError("Alpha image must be an L image with matching dimensions")
    output = bytearray(data)
    pixels = alpha.load()
    width_blocks = (width + 3) // 4
    height_blocks = (height + 3) // 4
    color_565 = ((202 * 31 // 255) << 11) | ((203 * 63 // 255) << 5) | (
        202 * 31 // 255
    )
    alpha_values = dxt5_alpha_values(255, 0)
    for by in range(height_blocks):
        for bx in range(width_blocks):
            alpha_selectors = 0
            has_visible = False
            for py in range(4):
                for px in range(4):
                    x, y = bx * 4 + px, by * 4 + py
                    value = 0 if x >= width or y >= height else pixels[x, y]
                    selector = min(
                        range(8),
                        key=lambda index: abs(alpha_values[index] - value),
                    )
                    has_visible |= value != 0
                    alpha_selectors |= selector << ((py * 4 + px) * 3)
            offset = (by * width_blocks + bx) * 16
            output[offset] = 255
            output[offset + 1] = 0
            output[offset + 2 : offset + 8] = alpha_selectors.to_bytes(
                6, "little"
            )
            if has_visible:
                output[offset + 8 : offset + 16] = struct.pack(
                    "<HHI", color_565, color_565, 0
                )
    return bytes(output)


def make_e_caron(source_alpha: Image.Image) -> Image.Image:
    glyph = Image.new("L", (14, 23), 0)
    source = source_alpha.crop((45, 25, 59, 48))
    # Keep the original E on exactly the same baseline as every other capital.
    # Its existing top padding already has enough room for the caron.
    glyph.paste(source, (0, 0))
    pixels = glyph.load()
    caron = {
        (4, 1): 255,
        (5, 1): 255,
        (5, 2): 255,
        (6, 2): 255,
        (6, 3): 255,
        (7, 2): 255,
        (8, 2): 255,
        (8, 1): 255,
        (9, 1): 255,
    }
    for (x, y), value in caron.items():
        pixels[x, y] = value
    return glyph


CZ_GLYPHS: tuple[tuple[int, str, str], ...] = (
    (0x010C, "C", "caron"),
    (0x010D, "c", "caron"),
    (0x010F, "d", "apostrophe"),
    (0x011A, "E", "caron"),
    (0x011B, "e", "caron"),
    (0x0148, "n", "caron"),
    (0x0158, "R", "caron"),
    (0x0159, "r", "caron"),
    (0x0160, "S", "caron"),
    (0x0161, "s", "caron"),
    (0x0164, "T", "caron"),
    (0x0165, "t", "apostrophe"),
    (0x016E, "U", "ring"),
    (0x016F, "u", "ring"),
    (0x017D, "Z", "caron"),
    (0x017E, "z", "caron"),
    (0x2013, "-", "en_dash"),
    (0x2014, "-", "em_dash"),
    (0x201C, '"', "left_quote"),
    (0x201D, '"', "right_quote"),
)


FONT_SPECS = (
    {
        "name": "Chrom20",
        "export_index": 0,
        "font_offset": 4982,
        "font_size": 41568,
        "texture_bulk": 285357,
        "texture_width": 512,
        "texture_height": 128,
    },
    {
        "name": "Chrom24",
        "export_index": 1,
        "font_offset": 46550,
        "font_size": 41568,
        "texture_bulk": 301555,
        "texture_width": 512,
        "texture_height": 256,
    },
    {
        "name": "Euro20",
        "export_index": 2,
        "font_offset": 88118,
        "font_size": 43450,
        "texture_bulk": 332711,
        "texture_width": 512,
        "texture_height": 256,
    },
    {
        "name": "Euro24",
        "export_index": 3,
        "font_offset": 131568,
        "font_size": 42620,
        "texture_bulk": 351225,
        "texture_width": 512,
        "texture_height": 256,
    },
)


def find_font_layout(font: bytes) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    for characters_offset in range(len(font) - 20):
        count = struct.unpack_from(">I", font, characters_offset)[0]
        if not 1 <= count <= 1000:
            continue
        textures_offset = characters_offset + 4 + count * 17
        if textures_offset + 16 > len(font):
            continue
        texture_count = struct.unpack_from(">I", font, textures_offset)[0]
        if not 1 <= texture_count <= 8:
            continue
        texture_end = textures_offset + 4 + texture_count * 4
        if texture_end + 12 > len(font):
            continue
        texture_objects = list(
            struct.unpack_from(
                ">" + "I" * texture_count, font, textures_offset + 4
            )
        )
        if not all(1 <= value <= 30 for value in texture_objects):
            continue
        remap_offset = texture_end + 4
        remap_count = struct.unpack_from(">I", font, remap_offset)[0]
        end = remap_offset + 4 + remap_count * 4 + 4
        if end != len(font):
            continue
        remap_pairs = [
            struct.unpack_from(">HH", font, remap_offset + 4 + index * 4)
            for index in range(remap_count)
        ]
        candidates.append(
            {
                "characters_offset": characters_offset,
                "character_count": count,
                "textures_offset": textures_offset,
                "texture_objects": texture_objects,
                "kerning": font[texture_end : texture_end + 4],
                "remap_offset": remap_offset,
                "remap_pairs": remap_pairs,
            }
        )
    if len(candidates) != 1:
        raise ValueError(
            f"Expected one native font layout, found {len(candidates)}"
        )
    return candidates[0]


def font_character(font: bytes, layout: dict[str, object], index: int) -> tuple[int, int, int, int, int]:
    count = int(layout["character_count"])
    if not 0 <= index < count:
        raise IndexError(f"Font character index {index} outside 0..{count - 1}")
    offset = int(layout["characters_offset"]) + 4 + index * 17
    return struct.unpack_from(">iiiiB", font, offset)


def visible_bbox(image: Image.Image, threshold: int = 80) -> tuple[int, int, int, int]:
    bbox = image.point(lambda value: 255 if value >= threshold else 0).getbbox()
    if bbox is None:
        raise ValueError("Glyph has no visible pixels")
    return bbox


def native_accent_template(
    reference: Image.Image, reference_base: Image.Image
) -> Image.Image:
    _, body_top, _, _ = visible_bbox(reference_base)
    accent_zone = reference.crop((0, 0, reference.width, body_top))
    accent_zone = accent_zone.point(lambda value: value if value >= 32 else 0)
    bbox = visible_bbox(accent_zone, threshold=48)
    return accent_zone.crop(bbox)


def paste_lighter(
    destination: Image.Image, source: Image.Image, position: tuple[int, int]
) -> None:
    layer = Image.new("L", destination.size, 0)
    layer.paste(source, position)
    destination.paste(ImageChops.lighter(destination, layer))


def accent_glyph(
    source: Image.Image,
    kind: str,
    acute_template: Image.Image | None = None,
    ring_template: Image.Image | None = None,
) -> Image.Image:
    glyph = source.copy()
    width, height = glyph.size
    draw = ImageDraw.Draw(glyph)
    scale = max(1, round(height / 23))
    body_left, body_top, body_right, _ = visible_bbox(source)
    center = round((body_left + body_right - 1) / 2)

    if kind == "caron":
        if acute_template is None:
            raise ValueError("Native acute template is required for a caron")
        arm_width = max(2, round(acute_template.width * 0.65))
        arm = acute_template.resize(
            (arm_width, acute_template.height), Image.Resampling.LANCZOS
        )
        mirrored_arm = ImageOps.mirror(arm)
        accent_height = arm.height
        y = max(0, body_top - accent_height - scale)
        paste_lighter(glyph, mirrored_arm, (center - arm.width + 1, y))
        paste_lighter(glyph, arm, (center, y))
    elif kind == "apostrophe":
        if acute_template is None:
            raise ValueError("Native acute template is required for an apostrophe")
        accent = acute_template
        x = min(width - accent.width, body_right - max(1, accent.width // 3))
        y = max(0, body_top - accent.height - scale)
        paste_lighter(glyph, accent, (x, y))
    elif kind == "ring":
        if ring_template is None:
            raise ValueError("Native ring template is required for a ring")
        x = center - ring_template.width // 2
        y = max(0, body_top - ring_template.height - scale)
        paste_lighter(glyph, ring_template, (x, y))
    elif kind in ("en_dash", "em_dash"):
        glyph = Image.new("L", glyph.size, 0)
        draw = ImageDraw.Draw(glyph)
        margin = max(1, width // (4 if kind == "en_dash" else 8))
        y = height // 2
        draw.line(
            ((margin, y), (width - margin - 1, y)),
            fill=255,
            width=scale,
        )
    elif kind in ("left_quote", "right_quote"):
        glyph = Image.new("L", glyph.size, 0)
        draw = ImageDraw.Draw(glyph)
        x = center - scale
        top = max(1, scale)
        if kind == "left_quote":
            points = ((x + scale, top), (x, top + scale), (x, top + 3 * scale))
        else:
            points = ((x, top), (x + scale, top + scale), (x + scale, top + 3 * scale))
        draw.line(points, fill=255, width=scale)
    else:
        raise ValueError(f"Unknown glyph accent kind: {kind}")
    return glyph


def patch_czech_fonts_command(args: argparse.Namespace) -> None:
    package = args.package.read_bytes()
    source_hash = hashlib.sha256(package).hexdigest().upper()
    expected_hash = "7B41BC02DAB75F107E03A03D0330A1D6F54415F3AEA09FA36D62280230384715"
    if source_hash != expected_hash:
        raise ValueError(
            f"Unexpected WarfareFonts.xxx SHA-256: {source_hash}; expected {expected_hash}"
        )
    lib = load_lzo(args.lzo)

    prepared_fonts: list[dict[str, object]] = []
    glyph_items: list[dict[str, object]] = []
    for spec in FONT_SPECS:
        font_offset = int(spec["font_offset"])
        font_size = int(spec["font_size"])
        font = package[font_offset : font_offset + font_size]
        layout = find_font_layout(font)
        remap_pairs = list(layout["remap_pairs"])
        remap = (
            {codepoint: index for codepoint, index in remap_pairs}
            if remap_pairs
            else {
                codepoint: codepoint
                for codepoint in range(int(layout["character_count"]))
            }
        )
        tiled, _ = read_bulk_chunk(package, int(spec["texture_bulk"]), lib)
        linear = endian_transform(
            untile_blocks(
                tiled,
                int(spec["texture_width"]),
                int(spec["texture_height"]),
            ),
            "swap16",
        )
        alpha = decode_dxt5(
            linear,
            int(spec["texture_width"]),
            int(spec["texture_height"]),
        ).getchannel("A")

        native_accents: dict[tuple[str, str], Image.Image] = {}
        for case, base_codepoint, acute_codepoint, ring_codepoint in (
            ("upper", ord("A"), 0x00C1, 0x00C5),
            ("lower", ord("a"), 0x00E1, 0x00E5),
        ):
            base_index = remap.get(base_codepoint, base_codepoint)
            base_u, base_v, base_width, base_height, base_texture = font_character(
                font, layout, base_index
            )
            if base_texture != 0:
                raise ValueError(
                    f"{spec['name']} native accent base uses unexpected texture page "
                    f"{base_texture}"
                )
            base_image = alpha.crop(
                (
                    base_u,
                    base_v,
                    base_u + base_width,
                    base_v + base_height,
                )
            )
            for accent_name, accent_codepoint in (
                ("acute", acute_codepoint),
                ("ring", ring_codepoint),
            ):
                accent_index = remap.get(accent_codepoint, accent_codepoint)
                accent_u, accent_v, accent_width, accent_height, accent_texture = (
                    font_character(font, layout, accent_index)
                )
                if accent_texture != 0:
                    raise ValueError(
                        f"{spec['name']} native {accent_name} uses unexpected "
                        f"texture page {accent_texture}"
                    )
                accent_image = alpha.crop(
                    (
                        accent_u,
                        accent_v,
                        accent_u + accent_width,
                        accent_v + accent_height,
                    )
                )
                native_accents[(case, accent_name)] = native_accent_template(
                    accent_image, base_image
                )

        additions: list[dict[str, object]] = []
        for codepoint, base, kind in CZ_GLYPHS:
            if codepoint in remap:
                continue
            base_index = remap.get(ord(base), ord(base))
            u, v, width, height, texture_index = font_character(
                font, layout, base_index
            )
            if texture_index != 0:
                raise ValueError(
                    f"{spec['name']} base glyph {base!r} uses unexpected texture page {texture_index}"
            )
            source = alpha.crop((u, v, u + width, v + height))
            case = "upper" if base.isupper() else "lower"
            item = {
                "font": spec["name"],
                "codepoint": codepoint,
                "width": width,
                "height": height,
                "image": accent_glyph(
                    source,
                    kind,
                    native_accents.get((case, "acute")),
                    native_accents.get((case, "ring")),
                ),
            }
            additions.append(item)
            glyph_items.append(item)
        prepared_fonts.append(
            {
                "spec": spec,
                "font": font,
                "layout": layout,
                "remap_pairs": remap_pairs,
                "additions": additions,
            }
        )

    atlas_width, atlas_height = 512, 128
    x = y = row_height = 0
    for item in glyph_items:
        width, height = int(item["width"]), int(item["height"])
        if x + width > atlas_width:
            x = 0
            y += row_height + 1
            row_height = 0
        if y + height > atlas_height:
            raise ValueError(
                f"Czech glyph atlas overflow at {item['font']} U+{int(item['codepoint']):04X}"
            )
        item["x"], item["y"] = x, y
        x += width + 1
        row_height = max(row_height, height)

    atlas_alpha = Image.new("L", (atlas_width, atlas_height), 0)
    for item in glyph_items:
        atlas_alpha.paste(
            item["image"], (int(item["x"]), int(item["y"]))
        )

    spare_tiled, spare_stored = read_bulk_chunk(package, 443474, lib)
    if spare_stored != 367 or len(spare_tiled) != 65536:
        raise ValueError("Unexpected spare texture bulk layout")
    blank_linear = bytes(atlas_width * atlas_height)
    # DXT5 uses 16 bytes per 4x4 block, i.e. one byte per pixel.
    blank_dxt5 = bytes(atlas_width * atlas_height)
    patched_linear = replace_dxt5_alpha(
        blank_dxt5, atlas_alpha, atlas_width, atlas_height
    )
    patched_tiled = tile_blocks(
        endian_transform(patched_linear, "swap16"),
        bytes(len(patched_linear)),
        atlas_width,
        atlas_height,
    )
    new_bulk = build_bulk_chunk(patched_tiled, lib)

    new_fonts: list[tuple[int, bytes]] = []
    for prepared in prepared_fonts:
        spec = prepared["spec"]
        font = prepared["font"]
        layout = prepared["layout"]
        additions = prepared["additions"]
        character_count = int(layout["character_count"])
        characters_offset = int(layout["characters_offset"])
        textures_offset = int(layout["textures_offset"])
        character_data = font[
            characters_offset + 4 : textures_offset
        ]
        texture_objects = list(layout["texture_objects"])
        if 13 in texture_objects:
            new_texture_index = texture_objects.index(13)
        else:
            new_texture_index = len(texture_objects)
            texture_objects.append(13)

        appended = bytearray()
        new_pairs = list(prepared["remap_pairs"])
        if not new_pairs:
            new_pairs = [
                (index, index) for index in range(character_count)
            ]
        for offset, item in enumerate(additions):
            new_index = character_count + offset
            appended += struct.pack(
                ">iiiiB",
                int(item["x"]),
                int(item["y"]),
                int(item["width"]),
                int(item["height"]),
                new_texture_index,
            )
            new_pairs.append((int(item["codepoint"]), new_index))

        new_characters = (
            struct.pack(">I", character_count + len(additions))
            + character_data
            + bytes(appended)
        )
        new_textures = (
            struct.pack(">I", len(texture_objects))
            + b"".join(struct.pack(">I", value) for value in texture_objects)
        )
        new_remap = (
            struct.pack(">I", len(new_pairs))
            + b"".join(
                struct.pack(">HH", codepoint, index)
                for codepoint, index in new_pairs
            )
        )
        new_font = (
            font[:characters_offset]
            + new_characters
            + new_textures
            + bytes(layout["kerning"])
            + new_remap
            + struct.pack(">I", 1)
        )
        new_fonts.append((int(spec["export_index"]), new_font))

    texture_offset, texture_size = 443289, 560
    texture = package[texture_offset : texture_offset + texture_size]
    bulk_relative = 185

    output = bytearray(package)
    export_updates: list[tuple[int, int, int]] = []
    for export_index, new_font in new_fonts:
        offset = len(output)
        output += new_font
        export_updates.append((export_index, len(new_font), offset))

    new_texture_offset = len(output)
    new_bulk_offset = new_texture_offset + bulk_relative
    texture_prefix = bytearray(texture[:169])
    struct.pack_into(">I", texture_prefix, 56, atlas_height)
    struct.pack_into(">I", texture_prefix, 161, new_texture_offset + 165)
    new_texture = (
        bytes(texture_prefix)
        + struct.pack(
            ">IIII", 0x10, len(patched_tiled), len(new_bulk), new_bulk_offset
        )
        + new_bulk
        + struct.pack(">II", atlas_width, atlas_height)
    )
    output += new_texture

    while len(output) % 0x8000:
        output.append(0)

    export_table_offset = 4098
    export_entry_size = 68

    def patch_export(index: int, serial_size: int, serial_offset: int) -> None:
        entry = export_table_offset + index * export_entry_size
        struct.pack_into(">II", output, entry + 32, serial_size, serial_offset)

    for export_index, serial_size, serial_offset in export_updates:
        patch_export(export_index, serial_size, serial_offset)
    patch_export(12, len(new_texture), new_texture_offset)

    args.output.write_bytes(output)
    if args.preview:
        atlas_alpha.save(args.preview)
    print(
        f"wrote {args.output} size={len(output)} "
        f"fonts={len(new_fonts)} glyphs={len(glyph_items)} "
        f"atlas={atlas_width}x{atlas_height} used_height={y + row_height} "
        f"texture={len(new_texture)}@{new_texture_offset} "
        f"sha256={hashlib.sha256(output).hexdigest().upper()}"
    )


def patch_warfare_fonts_command(args: argparse.Namespace) -> None:
    package = args.package.read_bytes()
    source_hash = hashlib.sha256(package).hexdigest().upper()
    expected_hash = "7B41BC02DAB75F107E03A03D0330A1D6F54415F3AEA09FA36D62280230384715"
    if source_hash != expected_hash:
        raise ValueError(
            f"Unexpected WarfareFonts.xxx SHA-256: {source_hash}; expected {expected_hash}"
        )
    lib = load_lzo(args.lzo)

    export_table_offset = 4098
    export_entry_size = 68
    font_export_index = 0
    spare_texture_export_index = 12

    font_offset, font_size = 4982, 41568
    font = package[font_offset : font_offset + font_size]
    characters_offset = 37192
    textures_offset = 41548
    remap_offset = 41560
    if struct.unpack_from(">I", font, characters_offset)[0] != 256:
        raise ValueError("Unexpected Chrom20 native character count")
    if struct.unpack_from(">II", font, textures_offset) != (1, 8):
        raise ValueError("Unexpected Chrom20 native texture array")
    if struct.unpack_from(">I", font, remap_offset)[0] != 0:
        raise ValueError("Unexpected Chrom20 native remap count")

    character_data = font[characters_offset + 4 : textures_offset]
    new_character = struct.pack(">iiiiB", 0, 0, 14, 23, 1)
    new_characters = struct.pack(">I", 257) + character_data + new_character
    new_textures = struct.pack(">III", 2, 8, 13)
    kerning = font[41556:41560]
    remap = bytearray(struct.pack(">I", 257))
    for codepoint in range(256):
        remap += struct.pack(">HH", codepoint, codepoint)
    remap += struct.pack(">HH", 0x011A, 256)
    new_font = (
        font[:characters_offset]
        + new_characters
        + new_textures
        + kerning
        + bytes(remap)
        + struct.pack(">I", 1)
    )

    # Read the source E from the Chrom20 atlas.
    chrom_tiled, chrom_stored = read_bulk_chunk(package, 285357, lib)
    if chrom_stored != 16005:
        raise ValueError("Unexpected Chrom20 bulk size")
    chrom_linear = endian_transform(
        untile_blocks(chrom_tiled, 512, 128), "swap16"
    )
    chrom_alpha = decode_dxt5(chrom_linear, 512, 128).getchannel("A")
    glyph = make_e_caron(chrom_alpha)

    # Put Äš into the otherwise unused second Gameover texture page. The
    # Chrom20 font references this existing export as texture page 1.
    texture_offset, texture_size = 443289, 560
    texture = package[texture_offset : texture_offset + texture_size]
    bulk_relative = 185
    spare_tiled, spare_stored = read_bulk_chunk(package, 443474, lib)
    if spare_stored != 367 or len(spare_tiled) != 65536:
        raise ValueError("Unexpected spare texture bulk layout")
    spare_linear = endian_transform(
        untile_blocks(spare_tiled, 512, 64), "swap16"
    )
    spare_image = decode_dxt5(spare_linear, 512, 64)
    spare_alpha = spare_image.getchannel("A")
    spare_alpha.paste(glyph, (0, 0))
    patched_linear = replace_dxt5_alpha(spare_linear, spare_alpha, 512, 64)
    patched_tiled = tile_blocks(
        endian_transform(patched_linear, "swap16"),
        spare_tiled,
        512,
        64,
    )
    new_bulk = build_bulk_chunk(patched_tiled, lib)

    new_font_offset = len(package)
    new_texture_offset = new_font_offset + len(new_font)
    new_bulk_offset = new_texture_offset + bulk_relative
    texture_prefix = bytearray(texture[:169])
    # Empty SourceArt bulk data still carries an absolute skip offset.
    struct.pack_into(">I", texture_prefix, 161, new_texture_offset + 165)
    new_texture = (
        bytes(texture_prefix)
        + struct.pack(">IIII", 0x10, 65536, len(new_bulk), new_bulk_offset)
        + new_bulk
        + texture[bulk_relative + spare_stored :]
    )

    output = bytearray(package)
    output += new_font
    output += new_texture
    while len(output) % 0x8000:
        output.append(0)

    def patch_export(index: int, serial_size: int, serial_offset: int) -> None:
        entry = export_table_offset + index * export_entry_size
        struct.pack_into(">II", output, entry + 32, serial_size, serial_offset)

    patch_export(font_export_index, len(new_font), new_font_offset)
    patch_export(spare_texture_export_index, len(new_texture), new_texture_offset)
    args.output.write_bytes(output)
    print(
        f"wrote {args.output} size={len(output)} "
        f"font={len(new_font)}@{new_font_offset} "
        f"texture={len(new_texture)}@{new_texture_offset} "
        f"sha256={hashlib.sha256(output).hexdigest().upper()}"
    )


def extract_command(args: argparse.Namespace) -> None:
    package = args.package.read_bytes()
    lib = load_lzo(args.lzo)
    tiled, consumed = read_bulk_chunk(package, args.offset, lib)
    linear = untile_blocks(tiled, args.width, args.height)
    linear = endian_transform(linear, args.endian)
    image = (
        decode_dxt5(linear, args.width, args.height)
        if args.format == "dxt5"
        else decode_dxt3(linear, args.width, args.height)
    )
    if args.alpha_only:
        image = image.getchannel("A").convert("RGB")
    image.save(args.output)
    print(
        f"wrote {args.output} ({args.width}x{args.height}, "
        f"bulk={len(tiled)}, stored={consumed}, endian={args.endian})"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    extract = sub.add_parser("extract")
    extract.add_argument("package", type=Path)
    extract.add_argument("output", type=Path)
    extract.add_argument("--offset", type=lambda value: int(value, 0), required=True)
    extract.add_argument("--width", type=int, required=True)
    extract.add_argument("--height", type=int, required=True)
    extract.add_argument("--lzo", type=Path, required=True)
    extract.add_argument(
        "--format", choices=("dxt3", "dxt5"), default="dxt3"
    )
    extract.add_argument(
        "--endian",
        choices=("none", "swap16", "swap32", "swap16in32"),
        default="swap16",
    )
    extract.add_argument("--alpha-only", action="store_true")
    extract.set_defaults(func=extract_command)
    patch = sub.add_parser("patch-warfare-fonts")
    patch.add_argument("package", type=Path)
    patch.add_argument("output", type=Path)
    patch.add_argument("--lzo", type=Path, required=True)
    patch.set_defaults(func=patch_warfare_fonts_command)
    patch_czech = sub.add_parser("patch-czech-fonts")
    patch_czech.add_argument("package", type=Path)
    patch_czech.add_argument("output", type=Path)
    patch_czech.add_argument("--lzo", type=Path, required=True)
    patch_czech.add_argument("--preview", type=Path)
    patch_czech.set_defaults(func=patch_czech_fonts_command)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
