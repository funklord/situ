"""The arithmetic a derived kernel implies, with no language in it.

Every backend that generates a codec needs the same numbers: the CRC table
a polynomial defines, the accumulator a width needs, the bit-reversal a
reflected variant starts from. Those are facts about the kernel, not about
C or Rust or Python, and the first two backends to want them would
otherwise have carried a copy each.

That is invariant 13's rule and it has already been paid for here: four
copies of one struct walk became `traverse`, and the four bugs of 26.225
to 26.228 were what a fifth copy cost. A CRC table computed twice is the
same shape with a worse failure -- two backends whose digests differ for a
reason no test would name.
"""

from __future__ import annotations

from situc import ast

#: The machine words a generated accumulator may use. A width narrower than
#: its word is masked back after every shift.
WORD_WIDTHS = (8, 16, 32, 64)


def crc_width(decl: ast.CodecDecl) -> int | None:
	"""A CRC width a backend can generate: whole bytes, 8 through 64.

	`None` where the width is not a literal or not in range, which is a
	kernel described correctly and not generated -- the caller emits a note
	rather than wrong code.
	"""
	kernel = decl.kernel
	assert kernel is not None
	value = kernel.argument("width")
	width = value.value if isinstance(value, ast.IntLiteral) else None
	if width is None or width % 8 or not 8 <= width <= 64:
		return None
	return width


def accumulator(width: int) -> int:
	"""The word that holds a `width`-bit CRC: the next one up, or its own.

	A 24-bit CRC accumulates in a 32-bit word and every shift is masked back
	to 24 bits. No language here has a 24-bit integer, and inventing one
	would buy nothing a mask does not.
	"""
	return next(word for word in WORD_WIDTHS if word >= width)


def number(decl: ast.CodecDecl, name: str, default: int = 0) -> int:
	"""One of the kernel's integer arguments, or a default."""
	kernel = decl.kernel
	assert kernel is not None
	value = kernel.argument(name)
	return value.value if isinstance(value, ast.IntLiteral) else default


def reverse(value: int, width: int) -> int:
	"""`value` with its low `width` bits in the opposite order."""
	result = 0
	for _ in range(width):
		result = (result << 1) | (value & 1)
		value >>= 1
	return result


def crc_table(width: int, poly: int, reflect: bool) -> list[int]:
	"""One byte's worth of the polynomial division, for each possible byte.

	Reflected variants run the division the other way round, which is the
	same arithmetic over the bit-reversed polynomial. Doing it here rather
	than reversing bits at run time is what keeps the generated loop one
	table lookup wide.
	"""
	mask  = (1 << width) - 1
	table = []

	if reflect:
		reversed_poly = reverse(poly, width)
		for byte in range(256):
			crc = byte
			for _ in range(8):
				crc = (crc >> 1) ^ (reversed_poly if crc & 1 else 0)
			table.append(crc & mask)
		return table

	top = 1 << (width - 1)
	for byte in range(256):
		crc = (byte << (width - 8)) & mask
		for _ in range(8):
			crc = ((crc << 1) ^ poly) & mask if crc & top else (crc << 1) & mask
		table.append(crc)
	return table


def crc_start(init: int, width: int, reflect: bool) -> int:
	"""The register's initial value, in the direction the loop runs it.

	A reflected CRC runs the register the other way round, so it *starts*
	the other way round. Emitting the catalogue's value as written was right
	for every reflected CRC in the library -- CRC-32 and CRC-16/MODBUS both
	start all-ones, which is its own reverse -- and wrong for the first one
	that does not. CRC-24/BLE starts at 0x555555 and came out as 0xD39857
	where the catalogue says 0xC25A56.
	"""
	return reverse(init, width) if reflect else init
