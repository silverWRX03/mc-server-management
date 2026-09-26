"""A small QR code encoder (byte mode), for pairing a phone by scanning the screen.

mcsm has no third-party dependencies, so this follows the QR Code specification (ISO/IEC
18004) the way Project Nayuki's MIT-licensed reference generator explains it: pick the
smallest version that fits, add Reed-Solomon error correction, draw the fixed patterns,
place the data in the zig-zag order and pick the mask that reads best.
"""

from __future__ import annotations

# Error correction codewords per block, and number of blocks, by level (L, M, Q, H) and version.
_ECC_PER_BLOCK = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)
_BLOCKS = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)
_FORMAT_BITS = (1, 0, 3, 2)  # how L, M, Q, H are written in the format information
LEVELS = {"L": 0, "M": 1, "Q": 2, "H": 3}
_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _raw_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver: int, ecl: int) -> int:
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ecl][ver] * _BLOCKS[ecl][ver]


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


class QrCode:
    def __init__(self, text: str, level: str = "M"):
        data = text.encode("utf-8")
        self.ecl = LEVELS[level]
        for ver in range(1, 41):
            count_bits = 8 if ver < 10 else 16
            if 4 + count_bits + 8 * len(data) <= _data_codewords(ver, self.ecl) * 8:
                break
        else:
            raise ValueError("too much data for a QR code")
        self.version = ver
        self.size = ver * 4 + 17
        bits: list[int] = []

        def put(value: int, n: int) -> None:
            bits.extend((value >> i) & 1 for i in reversed(range(n)))

        put(0b0100, 4)  # byte mode
        put(len(data), count_bits)
        for b in data:
            put(b, 8)
        capacity = _data_codewords(ver, self.ecl) * 8
        put(0, min(4, capacity - len(bits)))
        put(0, -len(bits) % 8)
        pad = 0xEC
        while len(bits) < capacity:
            put(pad, 8)
            pad ^= 0xEC ^ 0x11
        codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

        n = self.size
        self.modules = [[False] * n for _ in range(n)]
        self.function = [[False] * n for _ in range(n)]
        self._function_patterns()
        self._place(self._interleave(codewords))
        best, best_score = 0, None
        for mask in range(8):
            self._mask(mask)
            self._format_bits(mask)
            score = self._penalty()
            if best_score is None or score < best_score:
                best, best_score = mask, score
            self._mask(mask)  # undo
        self._mask(best)
        self._format_bits(best)

    # ------------------------------------------------------------ drawing
    def _set(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self.function[y][x] = True

    def _function_patterns(self) -> None:
        n = self.size
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self._alignment_positions()
        last = len(pos) - 1
        for i, ay in enumerate(pos):
            for j, ax in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue  # the finder patterns are there
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self._format_bits(0)
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self._set(a, b, bit)
                self._set(b, a, bit)

    def _alignment_positions(self) -> list[int]:
        if self.version == 1:
            return []
        count = self.version // 7 + 2
        step = (self.version * 8 + count * 3 + 5) // (count * 4 - 4) * 2
        return [6] + list(reversed([self.size - 7 - i * step for i in range(count - 1)]))

    def _format_bits(self, mask: int) -> None:
        data = _FORMAT_BITS[self.ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        n = self.size
        for i in range(6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(8):
            self._set(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, n - 15 + i, bit(i))
        self._set(8, n - 8, True)  # always dark

    def _interleave(self, data: list[int]) -> list[int]:
        ver, ecl = self.version, self.ecl
        blocks_n, ecc_len = _BLOCKS[ecl][ver], _ECC_PER_BLOCK[ecl][ver]
        raw = _raw_modules(ver) // 8
        short_n = blocks_n - raw % blocks_n
        short_len = raw // blocks_n
        divisor = _rs_divisor(ecc_len)
        blocks, k = [], 0
        for i in range(blocks_n):
            dat = data[k:k + short_len - ecc_len + (0 if i < short_n else 1)]
            k += len(dat)
            ecc = _rs_remainder(dat, divisor)
            if i < short_n:
                dat = dat + [0]
            blocks.append(dat + ecc)
        out = []
        for i in range(len(blocks[0])):
            for j, block in enumerate(blocks):
                if i != short_len - ecc_len or j >= short_n:
                    out.append(block[i])
        return out

    def _place(self, data: list[int]) -> None:
        n, i = self.size, 0
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = n - 1 - vert if upward else vert
                    if not self.function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def _mask(self, mask: int) -> None:
        fn = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and fn(x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def _penalty(self) -> int:
        """Long runs, 2x2 blocks and an uneven dark/light balance read worse."""
        n, m, score = self.size, self.modules, 0
        for lines in (m, list(zip(*m))):
            for line in lines:
                run, prev = 0, None
                for cell in line:
                    run = run + 1 if cell == prev else 1
                    prev = cell
                    if run == 5:
                        score += 3
                    elif run > 5:
                        score += 1
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)
        score += abs(dark * 20 - n * n * 10) // (n * n) * 10
        return score

    # ------------------------------------------------------------ output
    def svg(self, border: int = 4) -> str:
        """An SVG drawing (black on white, scalable; the page sizes it)."""
        full = self.size + border * 2
        path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y in range(self.size) for x in range(self.size)
                       if self.modules[y][x])
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {full} {full}" shape-rendering="crispEdges">'
                f'<rect width="100%" height="100%" fill="#fff"/><path d="{path}" fill="#000"/></svg>')


def svg(text: str, level: str = "M") -> str:
    return QrCode(text, level).svg()
