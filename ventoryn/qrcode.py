"""QR Code encoder — standard library only.

Byte mode, error-correction level L, versions 1-10 (enough for a
TOTP provisioning URI, ~140 characters). No ``qrcode`` / ``segno``
library exists in this environment, so the encoder is written here
from the ISO/IEC 18004 rules:

* data codewords: mode 0100, 8-bit count (versions 1-9) / 16-bit
  count (version 10), terminator, byte padding, 0xEC/0x11 pad bytes
* Reed-Solomon error correction over GF(256), generator 0x11D
* interleaved blocks, zig-zag data placement, 8 mask patterns with
  the four penalty rules picking the best mask
* format information (BCH 15,5) and version information (BCH 18,6)
  computed at runtime

Output is a matrix (list of rows of 0/1) plus an SVG renderer —
Ventoryn is vector-only, so the QR code is drawn as SVG rects,
never as a bitmap.
"""

from __future__ import annotations

# ---------------------------------------------------------------- GF(256)
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    """Generator polynomial coefficients (highest degree first)."""
    poly = [1]
    for i in range(degree):
        poly = _poly_mul(poly, [1, _EXP[i]])
    return poly


def _poly_mul(a: list[int], b: list[int]) -> list[int]:
    out = [0] * (len(a) + len(b) - 1)
    for i, ca in enumerate(a):
        for j, cb in enumerate(b):
            out[i + j] ^= _gf_mul(ca, cb)
    return out


def _rs_remainder(data: list[int], generator: list[int]) -> list[int]:
    """Reed-Solomon check bytes: data(x) * x^degree mod generator."""
    degree = len(generator) - 1
    res = list(data) + [0] * degree
    for i in range(len(data)):
        coef = res[i]
        if coef:
            for j in range(len(generator)):
                res[i + j] ^= _gf_mul(generator[j], coef)
    return res[-degree:]


# ------------------------------------------------------- version tables (L)
# version: (total codewords, ec codewords per block,
#           [(blocks in group, data codewords per block), ...],
#           alignment pattern centres)
_VERSIONS = {
    1: (26, 7, [(1, 19)], []),
    2: (44, 10, [(1, 32)], [6, 18]),
    3: (70, 15, [(1, 53)], [6, 22]),
    4: (100, 20, [(1, 80)], [6, 26]),
    5: (134, 26, [(1, 108)], [6, 30]),
    6: (172, 18, [(2, 68)], [6, 34]),
    7: (196, 20, [(2, 78)], [6, 22, 38]),
    8: (242, 24, [(2, 97)], [6, 24, 42]),
    9: (292, 30, [(2, 116)], [6, 26, 46]),
    10: (346, 18, [(2, 68), (2, 69)], [6, 28, 50]),
}
MAX_VERSION = 10
MAX_BYTES = 271  # byte-mode capacity of version 10-L


def _data_capacity(version: int) -> int:
    """Usable data codewords for the version (level L)."""
    total, ec, groups, _ = _VERSIONS[version]
    blocks = sum(g[0] for g in groups)
    return total - blocks * ec


def _encode_data(text: str, version: int) -> list[int]:
    """Data codewords (with terminator + padding) for byte mode."""
    raw = text.encode("utf-8")
    capacity = _data_capacity(version)
    count_bits = 16 if version >= 10 else 8
    bits: list[int] = []

    def put(value: int, nbits: int) -> None:
        for i in range(nbits - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)  # byte mode
    put(len(raw), count_bits)
    for byte in raw:
        put(byte, 8)
    # Terminator: up to 4 zero bits, as capacity allows.
    room = capacity * 8 - len(bits)
    put(0, min(4, room))
    while len(bits) % 8:
        bits.append(0)
    out = [int("".join(str(b) for b in bits[i:i + 8]), 2)
           for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    pad_idx = 0
    while len(out) < capacity:
        out.append(pad[pad_idx % 2])  # first pad byte is always 0xEC
        pad_idx += 1
    return out


def _build_codewords(text: str, version: int) -> list[int]:
    """Full interleaved codeword stream (data + error correction)."""
    _, ec, groups, _ = _VERSIONS[version]
    data = _encode_data(text, version)
    blocks: list[list[int]] = []
    pos = 0
    for count, dcw in groups:
        for _ in range(count):
            blocks.append(data[pos:pos + dcw])
            pos += dcw
    gen = _rs_generator(ec)
    ec_blocks = [_rs_remainder(b, gen) for b in blocks]
    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec):
        for b in ec_blocks:
            out.append(b[i])
    return out


# ------------------------------------------------------------- matrix build
def _empty_matrix(version: int) -> tuple[list[list[int | None]], int]:
    size = 17 + 4 * version
    return [[None] * size for _ in range(size)], size


def _set(matrix: list[list[int | None]], r: int, c: int, v: int) -> None:
    matrix[r][c] = v


def _finder(matrix, r0: int, c0: int) -> None:
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            r, c = r0 + dr, c0 + dc
            if not (0 <= r < len(matrix) and 0 <= c < len(matrix)):
                continue
            edge = dr in (-1, 7) or dc in (-1, 7)
            ring = dr in (0, 6) or dc in (0, 6)
            _set(matrix, r, c, 0 if edge else (1 if ring else 0))
    for dr in range(3):
        for dc in range(3):
            _set(matrix, r0 + 2 + dr, c0 + 2 + dc, 1)


def _alignment(matrix, centres: list[int]) -> None:
    n = len(matrix)
    for r in centres:
        for c in centres:
            if matrix[r][c] is not None:
                continue  # overlaps a finder pattern
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    _set(matrix, r + dr, c + dc,
                         1 if max(abs(dr), abs(dc)) != 1 else 0)


def _function_patterns(matrix, version: int) -> None:
    size = len(matrix)
    _finder(matrix, 0, 0)
    _finder(matrix, 0, size - 7)
    _finder(matrix, size - 7, 0)
    # Timing patterns.
    for i in range(8, size - 8):
        _set(matrix, 6, i, 1 if i % 2 == 0 else 0)
        _set(matrix, i, 6, 1 if i % 2 == 0 else 0)
    # Dark module — set AFTER the reserves below, which would
    # otherwise overwrite it (it sits at row size-8, the top of the
    # reserved vertical format strip).
    # Reserve format-info cells (filled in later).
    for i in range(9):
        if matrix[8][i] is None:
            _set(matrix, 8, i, 0)
        if matrix[i][8] is None:
            _set(matrix, i, 8, 0)
    for i in range(8):
        if matrix[8][size - 1 - i] is None:
            _set(matrix, 8, size - 1 - i, 0)
        if matrix[size - 1 - i][8] is None:
            _set(matrix, size - 1 - i, 8, 0)
    # Reserve version-info cells (versions >= 7).
    if version >= 7:
        for i in range(6):
            for j in range(3):
                _set(matrix, i, size - 11 + j, 0)
                _set(matrix, size - 11 + j, i, 0)
    _set(matrix, 4 * version + 9, 8, 1)


def _bch_format(data: int) -> int:
    """15-bit format string for (ec level L, mask) data bits."""
    d = data << 10
    g = 0x537
    rem = d
    while rem.bit_length() > 10:
        rem ^= g << (rem.bit_length() - 11)
    return ((data << 10) | rem) ^ 0x5412


def _bch_version(version: int) -> int:
    d = version << 12
    g = 0x1F25
    rem = d
    while rem.bit_length() > 12:
        rem ^= g << (rem.bit_length() - 13)
    return (version << 12) | rem


def _place_format(matrix, mask: int) -> None:
    """Place the 15 format bits (level L, mask) in both copies.
    Bit order follows ISO/IEC 18004 / the nayuki reference: bit 0
    (LSB) at (8,0), bit 6 at (8,7), bit 7 at (8,8), bit 8 at (7,8),
    bits 9..14 up column 8 to (0,8); second copy mirrored."""
    size = len(matrix)
    fmt = _bch_format(0b01 << 3 | mask)  # level L = 01

    def bit(i: int) -> int:
        return (fmt >> i) & 1

    for i in range(6):
        _set(matrix, 8, i, bit(i))
    _set(matrix, 8, 7, bit(6))
    _set(matrix, 8, 8, bit(7))
    _set(matrix, 7, 8, bit(8))
    for i in range(9, 15):
        _set(matrix, 14 - i, 8, bit(i))
    # Second copy: bits 0..7 along row 8 from the right, bits 8..14
    # down column 8 from row size-7. (The dark module sits at row
    # size-8, just above this strip — no overlap.)
    for i in range(8):
        _set(matrix, 8, size - 1 - i, bit(i))
    for i in range(8, 15):
        _set(matrix, size - 15 + i, 8, bit(i))


def _place_version(matrix, version: int) -> None:
    if version < 7:
        return
    size = len(matrix)
    info = _bch_version(version)
    for i in range(18):
        bit = (info >> i) & 1
        r, c = i // 3, i % 3
        _set(matrix, r, size - 11 + c, bit)
        _set(matrix, size - 11 + c, r, bit)


_MASK_FNS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _place_data(matrix, codewords: list[int], mask: int) -> None:
    size = len(matrix)
    bits: list[int] = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    mask_fn = _MASK_FNS[mask]
    idx = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if matrix[r][c] is None:
                    bit = bits[idx] if idx < len(bits) else 0
                    idx += 1
                    _set(matrix, r, c, bit ^ (1 if mask_fn(r, c) else 0))
        upward = not upward
        col -= 2


def _penalty(matrix: list[list[int]]) -> int:
    size = len(matrix)
    score = 0
    # N1: runs of 5+ in rows and columns.
    for line in matrix + [[matrix[r][c] for r in range(size)] for c in range(size)]:
        run = 1
        for i in range(1, size + 1):
            if i < size and line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
    # N2: 2x2 blocks of one colour.
    for r in range(size - 1):
        for c in range(size - 1):
            v = matrix[r][c]
            if v == matrix[r][c + 1] == matrix[r + 1][c] == matrix[r + 1][c + 1]:
                score += 3
    # N3: finder-like patterns.
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    lines = list(matrix) + [[matrix[r][c] for r in range(size)] for c in range(size)]
    for line in lines:
        for i in range(size - 10):
            seg = line[i:i + 11]
            if seg == pat1 or seg == pat2:
                score += 40
    # N4: dark-module proportion.
    dark = sum(sum(row) for row in matrix)
    total = size * size
    k = abs(dark * 20 - total * 10) // total
    score += k * 10
    return score


def encode(text: str, version: int | None = None) -> tuple[list[list[int]], int, int]:
    """Encode `text` into a QR matrix. Returns (matrix, version, mask).
    The smallest version that fits is chosen unless one is given."""
    nbytes = len(text.encode("utf-8"))
    if nbytes > MAX_BYTES:
        raise ValueError(
            f"text is {nbytes} bytes — too long for a version-10 QR code "
            f"(max {MAX_BYTES})")
    if version is None:
        version = next(
            v for v in range(1, MAX_VERSION + 1)
            if nbytes <= _data_capacity(v) - (3 if v >= 10 else 2))
    codewords = _build_codewords(text, version)
    best: tuple[int, list[list[int]], int] | None = None
    for mask in range(8):
        matrix, _ = _empty_matrix(version)
        _function_patterns(matrix, version)
        _place_data(matrix, codewords, mask)
        _place_format(matrix, mask)
        _place_version(matrix, version)
        filled = [[0 if v is None else v for v in row] for row in matrix]
        score = _penalty(filled)
        if best is None or score < best[0]:
            best = (score, filled, mask)
    assert best is not None
    return best[1], version, best[2]


def to_svg(matrix: list[list[int]], module: int = 8, margin: int = 4) -> str:
    """Render a matrix as an SVG string (vector — never a bitmap).
    `margin` is the quiet zone in modules; the whole code is one
    <path> of dark modules on a white background."""
    size = len(matrix)
    cells = []
    for r, row in enumerate(matrix):
        c = 0
        while c < size:
            if row[c]:
                start = c
                while c < size and row[c]:
                    c += 1
                cells.append(
                    f"M{(start + margin) * module} {(r + margin) * module}"
                    f"h{(c - start) * module}v{module}"
                    f"h{-(c - start) * module}z")
            else:
                c += 1
    total = (size + 2 * margin) * module
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{total}' "
        f"height='{total}' viewBox='0 0 {total} {total}' role='img' "
        f"aria-label='QR code'>"
        f"<rect width='{total}' height='{total}' fill='#ffffff'/>"
        f"<path d='{' '.join(cells)}' fill='#111111'/></svg>"
    )


def qr_svg(text: str, module: int = 8) -> str:
    """One call: text -> SVG string for the QR code."""
    matrix, _, _ = encode(text)
    return to_svg(matrix, module=module)
