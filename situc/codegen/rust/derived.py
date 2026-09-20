"""Derived codec implementations in Rust (section 13.4).

The arithmetic is `codegen.kernel_math`'s, shared with the C and Python
backends, so three languages cannot disagree about what a polynomial
means. What is here is the rendering.

Two families, deliberately. `polynomial` and `ones_complement` are what a
`checksum ... is <codec>` binding can name (0053), and until this existed
that binding was refused outright for Rust -- a schema C honoured and Rust
could not read. The other five decline with a note, exactly as C does for
a kernel it cannot generate: the properties are still derived and still
correct, and an `extern` impl supplies the code.
"""

from __future__ import annotations

from math import lcm

from situc import ast
from situc.codegen.kernel_math import (accumulator, crc_register, crc_shift,
                                       crc_start, crc_table, crc_width, number,
                                       reverse)
from situc import __version__


def _ident(prefix: str, name: str) -> str:
	"""`situ_crc32` in C is `crc32` in a module that is already `situ_`.

	Rust has modules, so the prefix a C symbol carries to stay out of the
	linker's way is noise here. The name is the codec's, lowercased, which
	is what a Rust caller would have written anyway.
	"""
	del prefix
	return name.lower()


def span_byte_helper() -> list[str]:
	"""The one byte-reader every `_spans` entry point goes through.

	A checksum that covers its own field runs the algorithm with those
	bytes taken as a constant (14.2, `[self_as]`). The bytes are still
	there and generated code never allocates a copy, so the hole is a
	parameter rather than an edited buffer.

	Two spans rather than one, because a checksum may cover bytes the
	message does not contain: UDP's and TCP's pseudo-headers are built by
	the caller and summed before the datagram (14.2a). The index walks the
	concatenation of the two, which is what lets one loop serve both a CRC
	and a sum without exposing any intermediate state -- and it is what
	makes the hole's index meaningful with a prefix in front of it. C
	spells the absent span as a null pointer with a zero length; Rust has
	no null pointer, so it is an empty slice.

	Emitted once per module and shared by both families, rather than
	once per codec: two copies of a six-line function are two things
	to be wrong. It is a free function rather than an inner one because
	`emit.py` puts these codecs in a module of its own making, where an
	inner definition would be repeated per generated function.
	"""
	return [
		"",
		"/// The byte at `i` of `a` followed by `b`, or `fill` where `i` is",
		"/// one of the `hole_len` bytes at `hole_at`. A checksum defined",
		"/// over its own field takes those bytes as a constant while it",
		"/// runs (14.2, `[self_as]`); generated code never allocates, so",
		"/// the hole is a parameter rather than an edited buffer.",
		"///",
		"/// Two spans, because a checksum may cover bytes the message does",
		"/// not contain -- UDP's and TCP's pseudo-headers are built by the",
		"/// caller and summed before the datagram (14.2a). `i` indexes the",
		"/// concatenation, so the hole is measured against it too. The",
		"/// simpler entry points below pass an empty second span, which is",
		"/// where C passes a null pointer.",
		# One line, however long: a continuation aligned under an open paren
		# at column zero is leading spaces with no tab in front, which the
		# generated-source convention gate reads as space indentation --
		# correctly, since there is no indentation for it to align against.
		"fn span_byte(a: &[u8], b: &[u8], i: usize, hole_at: usize,"
		" hole_len: usize, fill: u8) -> u8 {",
		"\tif i >= hole_at && i - hole_at < hole_len {",
		"\t\tfill",
		"\t} else if i < a.len() {",
		"\t\ta[i]",
		"\t} else {",
		"\t\tb[i - a.len()]",
		"\t}",
		"}",
	]


def bits_helper() -> list[str]:
	"""MSB-first bit access, which a symbol map needs and a CRC does not.

	A table code's symbols are not byte-aligned -- 4b5b is five bits out
	for four in -- so encoding walks bits rather than bytes. C has these
	in its runtime as `situ_bits_get_msb` and `situ_bits_set_msb`; Rust's
	generated modules have no runtime to reach into, so they are emitted
	here, once per module for the reason `span_byte_helper` gives.

	MSB-first because that is the order a line code is transmitted in and
	the order C's runtime uses: a decoder that agreed on the table and
	disagreed on the bit order would produce a plausible stream that is
	not the one sent.
	"""
	return [
		"",
		"/// `width` bits starting at bit `at`, most significant first.",
		"fn situ_bits_get_msb(data: &[u8], at: usize, width: usize) -> u64 {",
		"\tlet mut value: u64 = 0;",
		"",
		"\tfor i in 0..width {",
		"\t\tlet bit = at + i;",
		"\t\tlet byte = data.get(bit / 8).copied().unwrap_or(0);",
		"\t\tvalue = (value << 1) | u64::from((byte >> (7 - bit % 8)) & 1);",
		"\t}",
		"",
		"\tvalue",
		"}",
		"",
		"/// Write `width` bits of `value` at bit `at`, most significant first.",
		"fn situ_bits_set_msb(out: &mut [u8], at: usize, width: usize,",
		"\t\tvalue: u64) {",
		"\tfor i in 0..width {",
		"\t\tlet bit = at + i;",
		"",
		"\t\tif bit / 8 >= out.len() {",
		"\t\t\treturn;",
		"\t\t}",
		"",
		"\t\tlet mask = 1u8 << (7 - bit % 8);",
		"",
		"\t\tif (value >> (width - 1 - i)) & 1 == 1 {",
		"\t\t\tout[bit / 8] |= mask;",
		"\t\t} else {",
		"\t\t\tout[bit / 8] &= !mask;",
		"\t\t}",
		"\t}",
		"}",
	]


def _shift_register(decl: ast.CodecDecl, prefix: str) -> list[str] | None:
	"""An LFSR, and where its feedback comes from decides everything.

	Additive: the register runs on its own state and the data is XORed
	with the keystream, so it is startable anywhere, is its own inverse,
	and a corrupt bit spoils only itself. Multiplicative: the register is
	fed from the scrambled output, so a receiver synchronises itself
	without being told the state and pays for it with error propagation.

	Both are here because the pair is the point: one word of the
	description separates them. Re-spelled from C's `_shift_register`,
	which is where the width handling and the complement rule are argued.
	"""
	kernel = decl.kernel
	assert kernel is not None

	taps  = number(decl, "taps")
	width = number(decl, "width", 16)
	seed  = number(decl, "seed", (1 << width) - 1)

	if not taps or not 1 <= width <= 64:
		return None

	source       = kernel.argument("feedback")
	additive     = isinstance(source, ast.NameRef) and source.name == "input"
	complemented = kernel.flag("complement_feedback")
	name         = _ident(prefix, decl.name)
	held         = accumulator(width)
	word         = f"u{held}"
	mask         = (1 << width) - 1

	# The one XOR separating the two differential conventions, built once
	# because the encoder and decoder must complement or not complement
	# together: a decoder disagreeing with its encoder here returns the
	# complement of what was sent, which is plausible bytes with nothing
	# at run time to notice.
	feedback = f"{name}_parity(state & 0x{taps:X})"
	if complemented:
		feedback = f"({feedback} ^ 1)"

	shifted = (f"state = ((state << 1) | u{held}::from(output)) & 0x{mask:X};"
	           if held != width else
	           f"state = (state << 1) | u{held}::from(output);")
	shifted_in = (f"state = ((state << 1) | u{held}::from(coded)) & 0x{mask:X};"
	              if held != width else
	              f"state = (state << 1) | u{held}::from(coded);")

	head = [
		"",
		f"/// `{decl.name}`: a {width}-bit LFSR, taps 0x{taps:X}, seed"
		f" 0x{seed:X}.",
		("/// Additive: the register runs on its own state, so this is"
		 if additive else
		 "/// Multiplicative: the register is fed from the scrambled output,"),
		("/// startable anywhere and its own inverse."
		 if additive else
		 "/// so a receiver synchronises without being told the state."),
	]

	if additive:
		return head + [
			f"fn {name}_step(state: {word}) -> {word} {{",
			"	let bit = state & 1;",
			"	let next = state >> 1;",
			"",
			"	if bit != 0 {",
			f"		next ^ 0x{taps:X}",
			"	} else {",
			"		next",
			"	}",
			"}",
			"",
			f"pub fn {name}_encode(input: &[u8], len: usize,"
			" out: &mut [u8]) -> usize {",
			f"	let mut state: {word} = 0x{seed:X};",
			"",
			"	for at in 0..len {",
			"		let mut key: u8 = 0;",
			"",
			"		for bit in 0..8 {",
			"			key |= ((state & 1) as u8) << bit;",
			f"			state = {name}_step(state);",
			"		}",
			"",
			"		out[at] = input[at] ^ key;",
			"	}",
			"",
			"	len",
			"}",
			"",
			"/// Its own inverse: the keystream does not depend on the data.",
			f"pub fn {name}_decode(input: &[u8], len: usize,"
			" out: &mut [u8]) -> usize {",
			f"	{name}_encode(input, len, out)",
			"}",
		]

	return head + [
		f"fn {name}_parity(value: {word}) -> u8 {{",
		"	let mut bits: u8 = 0;",
		"	let mut left = value;",
		"",
		"	while left != 0 {",
		"		bits ^= (left & 1) as u8;",
		"		left >>= 1;",
		"	}",
		"",
		"	bits",
		"}",
		"",
		f"pub fn {name}_encode(input: &[u8], len: usize,"
		" out: &mut [u8]) -> usize {",
		f"	let mut state: {word} = 0x{seed:X};",
		"",
		"	for at in 0..len {",
		"		let mut coded: u8 = 0;",
		"",
		"		for bit in 0..8 {",
		"			let plain = (input[at] >> bit) & 1;",
		f"			let output = plain ^ {feedback};",
		"",
		"			coded |= output << bit;",
		"			// The scrambled bit goes into the register: that is what",
		"			// makes a receiver self-synchronising.",
		f"			{shifted}",
		"		}",
		"",
		"		out[at] = coded;",
		"	}",
		"",
		"	len",
		"}",
		"",
		"/// Not its own inverse: the register is fed from the scrambled",
		"/// side, so decoding shifts in what it received.",
		f"pub fn {name}_decode(input: &[u8], len: usize,"
		" out: &mut [u8]) -> usize {",
		f"	let mut state: {word} = 0x{seed:X};",
		"",
		"	for at in 0..len {",
		"		let mut plain: u8 = 0;",
		"",
		"		for bit in 0..8 {",
		"			let coded = (input[at] >> bit) & 1;",
		"",
		f"			plain |= (coded ^ {feedback}) << bit;",
		f"			{shifted_in}",
		"		}",
		"",
		"		out[at] = plain;",
		"	}",
		"",
		"	len",
		"}",
	]


def _padded_table(decl: ast.CodecDecl, prefix: str, inputs: int,
		outputs: int, mapping: list[int], pad: int) -> list[str] | None:
	"""A base-N code: whole groups, with a partial one filled out.

	base32 and base64. A group is the smallest run of input that is both a
	whole number of bytes and a whole number of symbols -- five bytes and
	three respectively -- so an input not ending on one has its last group
	padded rather than truncated. base16 never meets the question, four
	bits dividing a byte exactly, which is why it is the unpadded path.

	The arithmetic is C's, re-spelled: same group size, same fill rule,
	same reverse table. C's own note is worth repeating here because it
	says what the tests have to cover -- an encoder wrong only for inputs
	of length 3n+1 looks right in casual testing.
	"""
	if outputs != 8 or inputs >= 8:
		return None

	name       = _ident(prefix, decl.name)
	group_bits = lcm(8, inputs)
	group_in   = group_bits // 8
	symbols    = group_bits // inputs
	mask       = (1 << inputs) - 1

	reverse = [0xFF] * 256
	for symbol, value in enumerate(mapping):
		reverse[value] = symbol

	return [
		"",
		f"/// `{decl.name}`: RFC 4648 base{1 << inputs}. {group_in} input",
		f"/// bytes make {symbols} output bytes; a shorter final group is",
		f"/// filled with 0x{pad:02X}, so the output is always a whole number",
		"/// of groups -- which is what `ratio_padded` in the capability map",
		"/// means.",
		f"const {name.upper()}_ALPHABET: [u8; {len(mapping)}] = [",
		"\t" + ", ".join(f"0x{value:02X}" for value in mapping),
		"];",
		"",
		"/// Symbol for each byte, 0xFF where the byte is not in the alphabet.",
		f"const {name.upper()}_SYMBOL: [u8; 256] = [",
		"\t" + ", ".join(f"0x{value:02X}" for value in reverse),
		"];",
		"",
		f"pub fn {name}_encode(input: &[u8], len: usize,"
		" out: &mut [u8]) -> usize {",
		"\tlet mut written = 0usize;",
		"\tlet mut start = 0usize;",
		"",
		"\twhile start < len {",
		f"\t\tlet have = core::cmp::min(len - start, {group_in});",
		"\t\tlet mut acc: u64 = 0;",
		"",
		f"\t\tfor i in 0..{group_in} {{",
		"\t\t\tlet byte = if i < have { input[start + i] } else { 0 };",
		"\t\t\tacc = (acc << 8) | u64::from(byte);",
		"\t\t}",
		"",
		"\t\t// Symbols that carry data; the rest are padding.",
		f"\t\tlet full = (have * 8 + {inputs - 1}) / {inputs};",
		"",
		f"\t\tfor i in 0..{symbols} {{",
		f"\t\t\tlet shift = {group_bits} - {inputs} * (i + 1);",
		"",
		"\t\t\tout[written + i] = if i < full {",
		f"\t\t\t\t{name.upper()}_ALPHABET"
		f"[((acc >> shift) & 0x{mask:02X}) as usize]",
		"\t\t\t} else {",
		f"\t\t\t\t0x{pad:02X}",
		"\t\t\t};",
		"\t\t}",
		"",
		f"\t\twritten += {symbols};",
		f"\t\tstart += {group_in};",
		"\t}",
		"",
		"\twritten",
		"}",
		"",
		"/// Returns the number of bytes decoded, or 0 for input that is not",
		"/// a whole number of groups or carries a byte outside the alphabet.",
		f"pub fn {name}_decode(input: &[u8], len: usize,"
		" out: &mut [u8]) -> usize {",
		"\tlet mut written = 0usize;",
		"\tlet mut start = 0usize;",
		"",
		f"\tif len % {symbols} != 0 {{",
		"\t\treturn 0;",
		"\t}",
		"",
		"\twhile start < len {",
		"\t\tlet mut acc: u64 = 0;",
		"\t\tlet mut kept = 0usize;",
		"",
		f"\t\twhile kept < {symbols}"
		f" && input[start + kept] != 0x{pad:02X} {{",
		"\t\t\tkept += 1;",
		"\t\t}",
		"",
		f"\t\tfor i in 0..{symbols} {{",
		"\t\t\tlet mut symbol = 0u8;",
		"",
		"\t\t\tif i < kept {",
		f"\t\t\t\tsymbol = {name.upper()}_SYMBOL"
		"[input[start + i] as usize];",
		"",
		"\t\t\t\tif symbol == 0xFF {",
		"\t\t\t\t\treturn 0;",
		"\t\t\t\t}",
		"\t\t\t}",
		"",
		f"\t\t\tacc = (acc << {inputs}) | u64::from(symbol);",
		"\t\t}",
		"",
		f"\t\tlet bytes = kept * {inputs} / 8;",
		"",
		"\t\tfor i in 0..bytes {",
		f"\t\t\tlet shift = {group_bits} - 8 * (i + 1);",
		"",
		"\t\t\tout[written + i] = ((acc >> shift) & 0xFF) as u8;",
		"\t\t}",
		"",
		"\t\twritten += bytes;",
		f"\t\tstart += {symbols};",
		"\t}",
		"",
		"\twritten",
		"}",
	]


def _table(decl: ast.CodecDecl, prefix: str) -> list[str] | None:
	"""A symbol map, encoded and decoded a symbol at a time.

	The derivation is `c.derived._symbol_map` and `NAMED_CODES`, imported
	rather than repeated: decision 0017's amendment says a second backend
	re-spells and does not re-derive, and a second copy of the 4b5b table
	is a second table to be wrong. Only the spelling below is Rust's.

	A PADDED code is declined here as C's own path declines it in a
	different function: the last group's symbol count depends on how much
	input was left, which symbol-at-a-time cannot express. base32 and
	base64 are that shape; base16 is not, and is generated.
	"""
	from situc.codegen.c.derived import _symbol_map

	kernel = decl.kernel
	assert kernel is not None

	inputs  = number(decl, "input_bits")
	outputs = number(decl, "output_bits")
	mapping = _symbol_map(decl, inputs, outputs)

	if mapping is None or inputs > 8 or outputs > 16:
		return None
	pad = kernel.argument("pad")
	if isinstance(pad, ast.IntLiteral):
		return _padded_table(decl, prefix, inputs, outputs, mapping,
		                     pad.value)

	name = _ident(prefix, decl.name)
	size = 1 << inputs
	whole = ([f"\tif bits % {inputs} != 0 {{", "\t\treturn 0;", "\t}", ""]
	         if inputs > 1 else [])

	return [
		"",
		f"/// `{decl.name}`: {inputs} bits in, {outputs} bits out, ratio",
		f"/// {outputs}:{inputs}. The ratio is exact, so an output position is",
		"/// a linear function of an input one.",
		f"const {name.upper()}_ENCODE_TABLE: [u16; {size}] = [",
		"\t" + ", ".join(f"0x{mapping[symbol]:X}" for symbol in range(size)),
		"];",
		"",
		"/// Encode `bits` input bits from `input` into `out`. Returns the",
		f"/// number of output bits written, exactly bits * {outputs} /"
		f" {inputs}",
		*(["/// -- or 0 if `bits` is not a whole number of symbols, which has",
		   "/// no encoding the way a partial block has no permutation."]
		  if inputs > 1 else ["/// bits."]),
		f"pub fn {name}_encode(input: &[u8], bits: usize,"
		" out: &mut [u8]) -> usize {",
		"\tlet mut written = 0usize;",
		"\tlet mut at = 0usize;",
		"",
		*whole,
		f"\twhile at + {inputs} <= bits {{",
		f"\t\tlet symbol = situ_bits_get_msb(input, at, {inputs}) as usize;",
		"",
		f"\t\tsitu_bits_set_msb(out, written, {outputs},",
		f"\t\t\tu64::from({name.upper()}_ENCODE_TABLE[symbol]));",
		f"\t\twritten += {outputs};",
		f"\t\tat += {inputs};",
		"\t}",
		"",
		"\twritten",
		"}",
		"",
		"/// Decode. Returns the number of input bits recovered, or 0 on a",
		"/// symbol the code does not define, or on a length that is not a",
		"/// whole number of symbols -- both are a corrupted stream rather",
		"/// than a decodable one, and neither is something to decode part of.",
		f"pub fn {name}_decode(input: &[u8], bits: usize,"
		" out: &mut [u8]) -> usize {",
		"\tlet mut written = 0usize;",
		"\tlet mut at = 0usize;",
		"",
		f"\tif bits % {outputs} != 0 {{",
		"\t\treturn 0;",
		"\t}",
		"",
		f"\twhile at + {outputs} <= bits {{",
		f"\t\tlet code = situ_bits_get_msb(input, at, {outputs}) as u16;",
		"\t\tlet mut found = false;",
		"",
		f"\t\tfor symbol in 0..{size} {{",
		f"\t\t\tif {name.upper()}_ENCODE_TABLE[symbol] == code {{",
		f"\t\t\t\tsitu_bits_set_msb(out, written, {inputs},"
		" symbol as u64);",
		f"\t\t\t\twritten += {inputs};",
		"\t\t\t\tfound = true;",
		"\t\t\t\tbreak;",
		"\t\t\t}",
		"\t\t}",
		"",
		"\t\tif !found {",
		"\t\t\treturn 0;",
		"\t\t}",
		"",
		f"\t\tat += {outputs};",
		"\t}",
		"",
		"\twritten",
		"}",
	]


def generate(schema: ast.Schema, basename: str, prefix: str = "situ") -> str:
	"""Emit every derived implementation the schema binds, as Rust."""
	# `impls()` and `codecs()` decide everything this emits, and every
	# helper below takes a `CodecDecl`: no struct and no member is read
	# here, so a `parameter` -- an argument the caller supplies (0050) --
	# has no path into the output and needs no refusal.
	bound = {impl.codec for impl in schema.impls()
	         if impl.kind is ast.ImplKind.DERIVED}

	lines = [
		f"//! Generated by situc {__version__} from {basename}.situ"
		" -- do not edit.",
		"//!",
		"//! Implementations derived from kernel descriptions (section 13.4).",
		"//! The properties in the capability map and the code below come from",
		"//! one description, so they cannot disagree.",
		"",
		"#![allow(dead_code)]",
		*span_byte_helper(),
		# Only where a TABLE codec is bound. `span_byte_helper` is emitted
		# unconditionally because both existing families use it; these are
		# used by one family, and emitting them anyway changed every
		# derived module in the corpus -- 40 files gaining 30 lines of
		# helper nothing in them calls. A generated file growing for a
		# feature its schema does not use is a diff its reader has to
		# explain (26.451).
		# ... and an UNPADDED one: the padded base codes accumulate whole bytes and call neither helper, so a base64-only schema was carrying two dead functions (26.452).
		*(bits_helper() if any(
			decl.name in bound and decl.kernel is not None
			and decl.kernel.family is ast.KernelFamily.TABLE
			and not isinstance(decl.kernel.argument("pad"), ast.IntLiteral)
			for decl in schema.codecs()) else []),
	]

	emitted = 0
	for decl in schema.codecs():
		if decl.name not in bound or decl.kernel is None:
			continue

		body = _for_kernel(decl, prefix)
		if body is None:
			lines.extend([
				"",
				f"// No implementation for `{decl.name}`: a "
				f"{decl.kernel.family.value} kernel is described but not",
				"// yet generated in Rust. Its properties are derived and",
				f"// correct; bind an `impl {decl.name} extern \"...\"` to",
				"// supply the code.",
			])
			continue

		lines.extend(body)
		emitted += 1

	return "\n".join(lines) + "\n"


def _for_kernel(decl: ast.CodecDecl, prefix: str) -> list[str] | None:
	kernel = decl.kernel
	assert kernel is not None

	if kernel.family is ast.KernelFamily.POLYNOMIAL:
		# A polynomial over an extension field is Reed-Solomon, which is a
		# different code and not one of the two this backend generates.
		if kernel.argument("field") is not None:
			return None
		return _polynomial(decl, prefix)
	if kernel.family is ast.KernelFamily.ONES_COMPLEMENT:
		return _ones_complement(decl, prefix)
	if kernel.family is ast.KernelFamily.TABLE:
		return _table(decl, prefix)
	if kernel.family is ast.KernelFamily.SHIFT:
		return _shift_register(decl, prefix)
	return None


def _polynomial(decl: ast.CodecDecl, prefix: str) -> list[str] | None:
	"""A table-driven CRC, with the table computed rather than copied."""
	width = crc_width(decl)
	if width is None:
		return None

	poly = number(decl, "poly")
	if not poly:
		return None

	kernel = decl.kernel
	assert kernel is not None
	reflect = kernel.flag("reflect")
	init    = number(decl, "init")
	xorout  = number(decl, "xorout")

	table  = crc_table(width, poly, reflect)
	# A non-reflected code narrower than a byte runs left-aligned at the top
	# of one, so its register is a byte whatever the code's width and its
	# table and initial value are written in the register's digits (26.369).
	held   = crc_register(width, reflect)
	shift  = crc_shift(width, reflect)
	word   = f"u{held}"
	name   = _ident(prefix, decl.name)
	mask   = (1 << width) - 1
	digits = width // 4
	holds  = held // 4

	# A width narrower than the word holding it is masked back after every
	# shift, or the bits above it survive into the next lookup.
	# Not where the register IS the word: a left-aligned code's register is
	# a whole byte and masking it to the code's width takes the top bits off
	# mid-loop. `crc7_mmc` came out as 0x1F that way, against 0x75 (26.369).
	narrow = f" & 0x{mask:X}" if held != width and not shift else ""

	lines = [
		"",
		f"/// {decl.name}: width={width} poly=0x{poly:0{digits}X} "
		f"init=0x{init:0{digits}X}",
		f"/// xorout=0x{xorout:0{digits}X} "
		f"reflect={'yes' if reflect else 'no'}",
		"///",
		"/// The table is computed from the polynomial, not copied from one.",
		f"static {name.upper()}_TABLE: [{word}; 256] = [",
	]
	for row in range(0, 256, 4):
		entries = ", ".join(f"0x{table[row + column]:0{holds}X}"
		                    for column in range(4))
		lines.append(f"\t{entries},")
	lines.extend(["];", ""])

	started = crc_start(init, width, reflect)
	# Three entry points, as C has: the plain one is the holed one with a
	# zero-length hole, and the holed one is the two-span one with an empty
	# second span. One loop, so there is one place for the arithmetic to be
	# wrong.
	lines.extend([
		"#[must_use]",
		f"pub fn {name}(data: &[u8]) -> {word} {{",
		f"\t{name}_holed(data, 0, 0, 0)",
		"}",
		"",
		"/// The same digest with a hole in it: the `hole_len` bytes at",
		"/// `hole_at` read as `fill`, which is what `[self_as]` asks for",
		"/// (14.2). The bytes are still in `data`; nothing is copied.",
		"#[must_use]",
		f"pub fn {name}_holed(data: &[u8], hole_at: usize,"
		f" hole_len: usize, fill: u8) -> {word} {{",
		f"\t{name}_spans(data, &[], hole_at, hole_len, fill)",
		"}",
		"",
		"/// The digest over `a` followed by `b`, with the hole indexed",
		"/// against that concatenation. `b` is the message and `a` the",
		"/// bytes covered ahead of it -- a pseudo-header the caller built",
		"/// and this message does not contain (14.2a).",
		"#[must_use]",
		f"pub fn {name}_spans(a: &[u8], b: &[u8], hole_at: usize,"
		f" hole_len: usize, fill: u8) -> {word} {{",
		f"\tlet mut crc: {word} = 0x{started:0{holds}X};",
		"\tlet len = a.len() + b.len();",
		"",
		"\tfor i in 0..len {",
		"\t\tlet byte = span_byte(a, b, i, hole_at, hole_len, fill);",
		"",
	])
	# Parenthesised only where the mask needs it: `-D warnings` refuses an
	# unnecessary pair around an assigned value, and every generated crate
	# is built with it.
	# Width 8 first, and in BOTH directions. At that width the shift term is
	# zero -- an eight-bit register shifted eight ways is nothing -- so the
	# expression reduces to one lookup. C emits the shift anyway and is
	# correct, because `crc >> 8` on a `uint8_t` promotes to `int` and
	# evaluates to zero. Rust does not promote: `u8 >> 8` is a compile-time
	# overflow, and rustc refused the tree's one reflected eight-bit CRC.
	# Keyed on the REGISTER's width rather than the code's, which is the
	# fact the shift term depends on: at eight bits `crc >> 8` and
	# `crc << 8` are both zero, so the expression reduces to one lookup
	# whichever direction the code runs. C emits the shift anyway and is
	# correct, because `crc >> 8` on a `uint8_t` promotes to `int`. Rust
	# does not promote and rustc refuses it as an overflow -- which it did
	# for `crc5_usb`, a reflected five-bit code, so the one CRC 0046 says
	# derives and is checked did not compile in this backend at all. The
	# check-value test reaches it through C (26.369).
	if held == 8:
		step = f"{name.upper()}_TABLE[(crc ^ byte) as usize]"
	elif reflect:
		step = (f"{name.upper()}_TABLE"
		        f"[((crc ^ {word}::from(byte)) & 0xFF) as usize]"
		        f" ^ (crc >> 8)")
	else:
		step = (f"{name.upper()}_TABLE[(((crc >> {width - 8})"
		        f" ^ {word}::from(byte)) & 0xFF) as usize]"
		        f" ^ (crc << 8)")
	lines.append(f"\t\tcrc = ({step}){narrow};" if narrow
	             else f"\t\tcrc = {step};")
	lines.extend([
		"\t}",
		"",
		(f"\t((crc >> {shift}) ^ 0x{xorout:0{digits}X}) & 0x{mask:X}" if shift
		 else f"\t(crc ^ 0x{xorout:0{digits}X}) & 0x{mask:X}" if held != width
		 else f"\tcrc ^ 0x{xorout:0{digits}X}"),
		"}",
	])
	lines.extend(_polynomial_bits(decl, name, width, poly, init, xorout,
	                              reflect, digits))
	return lines


def _polynomial_bits(decl: ast.CodecDecl, name: str, width: int, poly: int,
		init: int, xorout: int, reflect: bool, digits: int) -> list[str]:
	"""The same code over a span that is not a whole number of bytes.

	USB's token is eleven bits and its CRC covers all of them, so a
	byte-length entry point cannot say what the algorithm runs over (0046).
	One bit at a time rather than a second table, and checked against the
	table function at every whole-byte length (26.373).
	"""
	word = f"u{accumulator(width)}"
	mask = (1 << width) - 1
	if reflect:
		started = reverse(init, width)
		taken   = ("\t\tlet bit = (data[(i >> 3) as usize]"
		           " >> (i & 7)) & 1;")
		body    = [
			f"\t\tlet low = (crc ^ {word}::from(bit)) & 1;",
			"\t\tcrc >>= 1;",
			"\t\tif low != 0 {",
			f"\t\t\tcrc ^= 0x{reverse(poly, width):0{digits}X};",
			"\t\t}",
		]
	else:
		started = init
		taken   = ("\t\tlet bit = (data[(i >> 3) as usize]"
		           " >> (7 - (i & 7))) & 1;")
		body    = [
			f"\t\tlet fire = ((crc >> {width - 1}) ^ {word}::from(bit)) & 1;",
			f"\t\tcrc = (crc << 1) & 0x{mask:X};",
			"\t\tif fire != 0 {",
			f"\t\t\tcrc ^= 0x{poly:0{digits}X};",
			"\t\t}",
		]

	return [
		"",
		f"/// `{decl.name}` over `bit_len` bits from `bit_at`, for a span that",
		"/// is not a whole number of bytes. The bits are taken in the order",
		"/// the code runs them: least significant first where it is",
		"/// reflected, most significant first where it is not.",
		"#[must_use]",
		f"pub fn {name}_bits(data: &[u8], bit_at: u32, bit_len: u32)"
		f" -> {word} {{",
		f"\tlet mut crc: {word} = 0x{started:0{digits}X};",
		"",
		"\tfor i in bit_at..bit_at + bit_len {",
		taken,
		*body,
		"\t}",
		"",
		f"\t(crc ^ 0x{xorout:0{digits}X}) & 0x{mask:X}",
		"}",
	]


def _ones_complement(decl: ast.CodecDecl, prefix: str) -> list[str] | None:
	"""RFC 1071's sum. The C backend's comments carry the reasoning."""
	kernel = decl.kernel
	assert kernel is not None

	name  = _ident(prefix, decl.name)
	final = "!(sum as u16)" if kernel.flag("complement") else "sum as u16"

	return [
		"",
		f"/// {decl.name}: RFC 1071, one's-complement sum with end-around",
		"/// carry" + (", complemented." if kernel.flag("complement")
		              else "."),
		"#[must_use]",
		f"pub fn {name}(data: &[u8]) -> u16 {{",
		f"\t{name}_holed(data, 0, 0, 0)",
		"}",
		"",
		"/// The same sum with a hole in it: the `hole_len` bytes at",
		"/// `hole_at` read as `fill`. That is what a checksum covering its",
		"/// own field asks for (14.2, `[self_as]`), and it is the shape",
		"/// RFC 1071 itself describes -- the field is summed as zero.",
		"#[must_use]",
		f"pub fn {name}_holed(data: &[u8], hole_at: usize,"
		f" hole_len: usize, fill: u8) -> u16 {{",
		f"\t{name}_spans(data, &[], hole_at, hole_len, fill)",
		"}",
		"",
		"/// The sum over `a` followed by `b`, with the hole indexed against",
		"/// that concatenation. `b` is the message and `a` the bytes",
		"/// covered ahead of it -- UDP's and TCP's pseudo-header, which the",
		"/// caller builds and the datagram does not contain (14.2a).",
		"///",
		"/// The sum is order-independent over 16-bit words, which is why a",
		"/// protocol may add the two halves separately. Walking them as one",
		"/// span is what keeps an odd-length `a` correct anyway: the first",
		"/// byte of `b` is then the low half of a word begun in `a`, which",
		"/// summing the halves apart would get wrong.",
		"#[must_use]",
		f"pub fn {name}_spans(a: &[u8], b: &[u8], hole_at: usize,"
		f" hole_len: usize, fill: u8) -> u16 {{",
		"\t// 64 bits, so the accumulator cannot overflow for any slice a",
		"\t// u32 length can express. A u32 holds 65537 words of 0xFFFF and",
		"\t// wraps on the next one.",
		"\tlet len = a.len() + b.len();",
		"\tlet mut sum: u64 = 0;",
		"\tlet mut i = 0usize;",
		"",
		"\twhile i + 1 < len {",
		"\t\tsum += u64::from(u16::from_be_bytes([",
		"\t\t\tspan_byte(a, b, i, hole_at, hole_len, fill),",
		"\t\t\tspan_byte(a, b, i + 1, hole_at, hole_len, fill),",
		"\t\t]));",
		"\t\ti += 2;",
		"\t}",
		"\tif i < len {",
		"\t\t// The odd byte is the HIGH half of a final word.",
		"\t\tsum += u64::from("
		"span_byte(a, b, i, hole_at, hole_len, fill)) << 8;",
		"\t}",
		"",
		"\t// A loop, because a fold can carry again.",
		"\twhile sum >> 16 != 0 {",
		"\t\tsum = (sum & 0xFFFF) + (sum >> 16);",
		"\t}",
		f"\t{final}",
		"}",
	]
