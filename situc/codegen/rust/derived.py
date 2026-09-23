"""Derived codec implementations in Rust (section 13.4).

The arithmetic is `codegen.kernel_math`'s, shared with the C and Python
backends, so three languages cannot disagree about what a polynomial
means. What is here is the rendering.

Every family, now. It began as two -- `polynomial` and `ones_complement`
are what a `checksum ... is <codec>` binding can name (0053), and until
that existed the binding was refused outright for Rust, a schema C
honoured and Rust could not read -- and the other five followed.
Reed-Solomon was the last, and is the one that could not be re-spelled
from C: its encoder and decoder are `codegen.kernel_program`'s statement
tree, which `_RustRenderer` at the foot of this file renders, so
Berlekamp-Massey, the Chien search and Forney's formula exist once for
all three backends rather than three times.

What declines is a particular code rather than a family: a linear or
permutation code this backend has no name for, a CRC whose schema gives
no width or no polynomial, a Reed-Solomon over a field other than
GF(256). A decline carries a note, exactly as C does -- the properties
are still derived and still correct, and an `extern` impl supplies the
code.
"""

from __future__ import annotations

from math import lcm

from situc import ast
from situc.codegen.kernel_math import (accumulator, crc_register, crc_shift,
                                       crc_start, crc_table, crc_width,
                                       gf_tables, number, reverse,
                                       rs_generator_coefficients)
from situc.codegen.kernel_program import (Array, Assign, Binary, Blank,
                                          Comment, Declare, Expr, Gf, If,
                                          Increment, Index, Lit, Loop, Name,
                                          Return, Stmt, XorAssign,
                                          rs_decoder_program,
                                          rs_encoder_program)
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


def _smtp_dot(decl: ast.CodecDecl, prefix: str) -> list[str]:
	"""RFC 5321 section 4.5.2, re-spelled from C including its two
	deliberate departures from a strict reading.

	A line end is tested as LF ALONE, not CRLF: that is what keeps this a
	stream, since a CR ending one call and an LF starting the next would
	otherwise be missed. And the decoder strips a leading period
	unconditionally, where the RFC strips it only when other characters
	follow -- a line of exactly `.` is the terminator, and the framing
	scan stops before it, so it never reaches this. Both are
	preconditions a port must not quietly change.
	"""
	name = _ident(prefix, decl.name)

	return [
		"",
		f"/// `{decl.name}`: SMTP dot-stuffing, RFC 5321 section 4.5.2.",
		"/// A period at the start of a line is doubled. The terminator is",
		"/// neither written nor consumed here: the region is delimited as",
		"/// well as coded, and the scan runs on the encoded bytes.",
		f"pub fn {name}_encode(input: &[u8], len: usize,"
		" out: &mut [u8]) -> usize {",
		"\tlet mut written = 0usize;",
		"\t// The body starts at the start of a line.",
		"\tlet mut at_line_start = true;",
		"",
		"\tfor read in 0..len {",
		"\t\tif at_line_start && input[read] == b'.' {",
		"\t\t\tout[written] = b'.';",
		"\t\t\twritten += 1;",
		"\t\t}",
		"",
		"\t\tout[written] = input[read];",
		"\t\twritten += 1;",
		"\t\tat_line_start = input[read] == b'\\n';",
		"\t}",
		"",
		"\twritten",
		"}",
		"",
		"/// The inverse.",
		f"pub fn {name}_decode(input: &[u8], len: usize,"
		" out: &mut [u8]) -> usize {",
		"\tlet mut written = 0usize;",
		"\tlet mut at_line_start = true;",
		"",
		"\tfor read in 0..len {",
		"\t\tif at_line_start && input[read] == b'.' {",
		"\t\t\t// Drop the added one and keep what follows.",
		"\t\t\tat_line_start = false;",
		"\t\t\tcontinue;",
		"\t\t}",
		"",
		"\t\tout[written] = input[read];",
		"\t\twritten += 1;",
		"\t\tat_line_start = input[read] == b'\\n';",
		"\t}",
		"",
		"\twritten",
		"}",
	]


def _linear_block(decl: ast.CodecDecl, prefix: str) -> list[str] | None:
	"""Hamming(7,4), whose shape is unlike every other family here.

	A nibble in, a byte out -- not `(in, len, out)` -- because it is a
	symbol map with a correction step rather than a stream transform.
	Rust returns the corrected flag in a tuple where C takes an
	out-parameter, there being no null pointer to make optional.

	The tables are computed by C's generator from `_HAMMING_PARITY` and
	imported rather than transcribed: a 16-entry table copied is a table
	nobody checks.

	Single-error CORRECTING and not detecting: the syndrome's "no error"
	entry is unreachable from the error branch, so a double-bit error is
	silently miscorrected and still reports `true`. That is d_min = 3
	behaving as it must, and the flag means "the syndrome was nonzero"
	rather than anything stronger.
	"""
	from situc.codegen.c.derived import DERIVED_LINEAR, _HAMMING_PARITY
	from situc.codegen.c.derived import _named_code

	if _named_code(decl) not in DERIVED_LINEAR:
		return None

	name = _ident(prefix, decl.name)

	syndrome = [7] * 8
	for position in range(7):
		codeword = 1 << position
		value = 0
		for row, mask in enumerate(_HAMMING_PARITY):
			parity = bin((codeword & 0xF) & mask).count("1") & 1
			parity ^= (codeword >> (4 + row)) & 1
			value |= parity << row
		syndrome[value] = position

	table = []
	for nibble in range(16):
		word = nibble
		for row, mask in enumerate(_HAMMING_PARITY):
			word |= (bin(nibble & mask).count("1") & 1) << (4 + row)
		table.append(word)

	masks = ", ".join(f"0x{mask:02X}" for mask in _HAMMING_PARITY)

	return [
		"",
		f"/// `{decl.name}`: Hamming(7,4), systematic. The low nibble of a",
		"/// codeword is the data; the three high bits are parity.",
		f"const {name.upper()}_ENCODE_TABLE: [u8; 16] = [",
		"\t" + ", ".join(f"0x{value:02X}" for value in table),
		"];",
		"",
		"/// Syndrome to the bit it accuses; 7 means no error.",
		f"const {name.upper()}_SYNDROME: [u8; 8] = [",
		"\t" + ", ".join(str(value) for value in syndrome),
		"];",
		"",
		f"pub fn {name}_encode(nibble: u8) -> u8 {{",
		f"\t{name.upper()}_ENCODE_TABLE[(nibble & 0x0F) as usize]",
		"}",
		"",
		"/// Returns the nibble and whether a bit was corrected. A DOUBLE",
		"/// error is miscorrected and still reports true: d_min is 3, so",
		"/// this code corrects one bit and cannot report `detected but",
		"/// not correctable`.",
		f"pub fn {name}_decode(codeword: u8) -> (u8, bool) {{",
		f"\tconst MASKS: [u8; 3] = [{masks}];",
		"",
		"\tlet mut value = codeword & 0x7F;",
		"\tlet mut check = 0u8;",
		"",
		"\tfor row in 0..3 {",
		"\t\tlet mut bits = value & MASKS[row];",
		"\t\tlet mut parity = 0u8;",
		"",
		"\t\twhile bits != 0 {",
		"\t\t\tparity ^= bits & 1;",
		"\t\t\tbits >>= 1;",
		"\t\t}",
		"",
		"\t\tparity ^= (value >> (4 + row)) & 1;",
		"\t\tcheck |= parity << row;",
		"\t}",
		"",
		"\tif check == 0 {",
		"\t\treturn (value & 0x0F, false);",
		"\t}",
		"",
		f"\tlet at = {name.upper()}_SYNDROME[check as usize];",
		"",
		"\tif at < 7 {",
		"\t\tvalue ^= 1 << at;",
		"\t}",
		"",
		"\t(value & 0x0F, true)",
		"}",
	]


def _permutation(decl: ast.CodecDecl, prefix: str) -> list[str] | None:
	"""A block interleaver: written row-major, read column-major.

	`rows` is the discriminator. A permutation declared only by extent
	has a span and no mapping, so the properties follow and the code
	cannot -- C returns None for it in four places that must agree.

	A partial block is REFUSED rather than padded: it has no defined
	permutation. Note 0 is also the answer for an empty input, so a
	caller cannot tell the two apart -- C's behaviour, kept.
	"""
	kernel = decl.kernel
	assert kernel is not None

	if kernel.argument("rows") is None:
		return None

	rows    = number(decl, "rows")
	columns = number(decl, "columns")
	block   = rows * columns
	name    = _ident(prefix, decl.name)

	def loop(target: str, source: str) -> list[str]:
		return [
			f"\t\tfor row in 0..{rows} {{",
			f"\t\t\tfor column in 0..{columns} {{",
			f"\t\t\t\tout[at + {target}] = input[at + {source}];",
			"\t\t\t}",
			"\t\t}",
		]

	forward = f"column * {rows} + row", f"row * {columns} + column"

	return [
		"",
		f"/// `{decl.name}`: a {rows}x{columns} block interleaver, written",
		"/// row-major and read column-major. Length-preserving, and the",
		"/// input must be a whole number of blocks.",
		f"pub const {name.upper()}_BLOCK: usize = {block};",
		"",
		f"pub fn {name}_encode(input: &[u8], len: usize,"
		" out: &mut [u8]) -> usize {",
		"\t// Whole blocks only: a partial one has no defined permutation.",
		f"\tif len % {block} != 0 {{",
		"\t\treturn 0;",
		"\t}",
		"",
		f"\tfor at in (0..len).step_by({block}) {{",
		*loop(*forward),
		"\t}",
		"",
		"\tlen",
		"}",
		"",
		"/// The inverse: the same loop with the two index expressions",
		"/// swapped. At 4x4 that is the SAME permutation -- the interleaver",
		"/// is its own inverse when it is square -- so a round trip cannot",
		"/// tell this from `encode`, and only a non-square case can.",
		f"pub fn {name}_decode(input: &[u8], len: usize,"
		" out: &mut [u8]) -> usize {",
		f"\tif len % {block} != 0 {{",
		"\t\treturn 0;",
		"\t}",
		"",
		f"\tfor at in (0..len).step_by({block}) {{",
		*loop(forward[1], forward[0]),
		"\t}",
		"",
		"\tlen",
		"}",
	]


def _reads_bits(decl: ast.CodecDecl) -> bool:
	"""Whether this codec's body calls the MSB bit helpers.

	An UNPADDED table code walks bits, and so does bit stuffing. The
	padded base codes accumulate whole bytes and call neither, which is
	why the question is asked per codec rather than per family -- a
	base64-only module carrying two dead helpers is a diff its reader
	has to explain (26.452).
	"""
	from situc.codegen.c.derived import BIT_STUFFING, _named_code

	kernel = decl.kernel
	if kernel is None:
		return False
	if kernel.family is ast.KernelFamily.TABLE:
		return not isinstance(kernel.argument("pad"), ast.IntLiteral)
	if kernel.family is ast.KernelFamily.STUFFING:
		return _named_code(decl) in BIT_STUFFING
	return False


def _bit_stuffing(decl: ast.CodecDecl, prefix: str,
		code: str) -> list[str]:
	"""HDLC and USB bit stuffing: a zero after `run` contiguous ones.

	Lengths are in BITS in both directions, not bytes, and the bit order
	is MSB-first and hardcoded -- the codec consumes a bit stream and
	does not consult the schema's bit order. Both standards TRANSMIT
	octets least-significant-bit first, so a vector taken from a real
	capture needs each byte reversed on one side; a vector written as a
	bit string does not.

	Re-spelled from C including two behaviours a port could reasonably
	get wrong. Truncation is NOT an error: an input ending exactly where
	the stuffed bit belongs simply ends. And the decoder's only failure
	is 0, which is also the answer for empty input, so the two are
	indistinguishable -- C's, kept, because a differential compares them.
	"""
	from situc.codegen.c.derived import BIT_STUFFING

	run = BIT_STUFFING[code][0]
	name = _ident(prefix, decl.name)

	return [
		"",
		f"/// `{decl.name}`: a zero after {run} contiguous ones, so"
		f" {run + 1} cannot",
		"/// appear in the body. Lengths are in BITS and so is the return.",
		f"/// `out` needs bits + bits / {run} + 1 bits, and is",
		"/// read-modify-written: bits past the end of the last byte keep",
		"/// whatever the caller left there.",
		f"pub fn {name}_encode(input: &[u8], bits: usize,"
		" out: &mut [u8]) -> usize {",
		"\tlet mut written = 0usize;",
		"\tlet mut ones = 0usize;",
		"",
		"\tfor at in 0..bits {",
		"\t\tlet bit = situ_bits_get_msb(input, at, 1);",
		"",
		"\t\tsitu_bits_set_msb(out, written, 1, bit);",
		"\t\twritten += 1;",
		"",
		"\t\tif bit != 0 {",
		"\t\t\tones += 1;",
		"",
		f"\t\t\tif ones == {run} {{",
		"\t\t\t\tsitu_bits_set_msb(out, written, 1, 0);",
		"\t\t\t\twritten += 1;",
		"\t\t\t\tones = 0;",
		"\t\t\t}",
		"\t\t} else {",
		"\t\t\tones = 0;",
		"\t\t}",
		"\t}",
		"",
		"\twritten",
		"}",
		"",
		f"/// The inverse. Returns 0 where {run + 1} contiguous ones appear,",
		"/// which the encoder cannot produce -- and 0 is also the answer",
		"/// for empty input, so a caller cannot tell those apart.",
		f"pub fn {name}_decode(input: &[u8], bits: usize,"
		" out: &mut [u8]) -> usize {",
		"\tlet mut written = 0usize;",
		"\tlet mut ones = 0usize;",
		"",
		"\tfor at in 0..bits {",
		"\t\tlet bit = situ_bits_get_msb(input, at, 1);",
		"",
		f"\t\tif ones == {run} {{",
		"\t\t\t// A stuffed zero, which the encoder put there.",
		"\t\t\tones = 0;",
		"",
		"\t\t\tif bit != 0 {",
		"\t\t\t\treturn 0;",
		"\t\t\t}",
		"",
		"\t\t\tcontinue;",
		"\t\t}",
		"",
		"\t\tsitu_bits_set_msb(out, written, 1, bit);",
		"\t\twritten += 1;",
		"\t\tones = if bit != 0 { ones + 1 } else { 0 };",
		"\t}",
		"",
		"\twritten",
		"}",
	]


def _stuffing(decl: ast.CodecDecl, prefix: str) -> list[str] | None:
	"""COBS and the escape-stuffed byte codes (SLIP, PPP async).

	Bit stuffing and SMTP dot-stuffing are still declined: they are the
	other two shapes in this family and are their own tranche.

	Re-spelled from C, including its reasons -- COBS flushes a full group
	only when more input follows, because opening one at the very end
	would spend a second overhead byte, which is the one thing COBS
	promises not to do; and PPP's escape is a transformation rather than
	a table, so its decoder reverses escapes this generator never
	enumerated, which a peer with a non-zero ACCM will send.
	"""
	from situc.codegen.c.derived import ESCAPE_STUFFING, _named_code

	code = _named_code(decl)
	name = _ident(prefix, decl.name)

	if code == "cobs":
		return [
			"",
			f"/// `{decl.name}`: COBS (Cheshire and Baker). A zero byte",
			"/// becomes a pointer to the next one, so no zero survives in",
			"/// the body and a zero can delimit the frame.",
			"///",
			"/// `out` needs len + len/254 + 2 bytes.",
			f"pub fn {name}_encode(input: &[u8], len: usize,"
			" out: &mut [u8]) -> usize {",
			"\tlet mut code_at = 0usize;",
			"\tlet mut written = 1usize;",
			"\t// Wider than the byte it is stored as: the invariant keeps",
			"\t// it under 0xFF, and a debug build should not depend on",
			"\t// that to avoid an overflow panic.",
			"\tlet mut code: u32 = 1;",
			"",
			"\tfor read in 0..len {",
			"\t\tif input[read] != 0 {",
			"\t\t\tout[written] = input[read];",
			"\t\t\twritten += 1;",
			"\t\t\tcode += 1;",
			"",
			"\t\t\t// A full group is flushed only when more input",
			"\t\t\t// follows: at the very end there is nothing for a new",
			"\t\t\t// group to hold, and opening one would spend a second",
			"\t\t\t// overhead byte, which COBS promises not to do.",
			"\t\t\tif code != 0xFF || read + 1 >= len {",
			"\t\t\t\tcontinue;",
			"\t\t\t}",
			"\t\t}",
			"",
			"\t\tout[code_at] = code as u8;",
			"\t\tcode_at = written;",
			"\t\twritten += 1;",
			"\t\tcode = 1;",
			"\t}",
			"",
			"\tout[code_at] = code as u8;",
			"\tout[written] = 0;\t// the delimiter",
			"\twritten + 1",
			"}",
			"",
			f"pub fn {name}_decode(input: &[u8], len: usize,"
			" out: &mut [u8]) -> usize {",
			"\tlet mut read = 0usize;",
			"\tlet mut written = 0usize;",
			"",
			"\twhile read < len {",
			"\t\tlet code = input[read];",
			"",
			"\t\tif code == 0 {",
			"\t\t\tbreak;\t// the delimiter ends the frame",
			"\t\t}",
			"",
			"\t\tread += 1;",
			"",
			"\t\tfor _ in 1..code {",
			"\t\t\tif read >= len {",
			"\t\t\t\treturn 0;\t// truncated: a code ran past the end",
			"\t\t\t}",
			"",
			"\t\t\tout[written] = input[read];",
			"\t\t\twritten += 1;",
			"\t\t\tread += 1;",
			"\t\t}",
			"",
			"\t\tif code != 0xFF && read < len && input[read] != 0 {",
			"\t\t\tout[written] = 0;",
			"\t\t\twritten += 1;",
			"\t\t}",
			"\t}",
			"",
			"\twritten",
			"}",
		]

	from situc.codegen.c.derived import BIT_STUFFING

	if code in BIT_STUFFING:
		return _bit_stuffing(decl, prefix, code)

	if code == "smtp_dot":
		return _smtp_dot(decl, prefix)

	if code not in ESCAPE_STUFFING:
		return None

	title, delim, esc, xor, pairs = ESCAPE_STUFFING[code]
	arms = []
	for literal, escaped in pairs:
		arms += [f"\t\t\t0x{literal:02X} => {{",
		         f"\t\t\t\tout[written] = 0x{esc:02X};",
		         f"\t\t\t\tout[written + 1] = 0x{escaped:02X};",
		         "\t\t\t\twritten += 2;",
		         "\t\t\t}"]

	if xor is None:
		undo = ["\t\tmatch input[read] {",
		        *[line for literal, escaped in pairs for line in (
			        f"\t\t0x{escaped:02X} => out[written] ="
			        f" 0x{literal:02X},",)],
		        "\t\t\t// An escape this code does not define. Refusing",
		        "\t\t\t// beats inventing a byte: the frame is not what",
		        "\t\t\t// it claims.",
		        "\t\t\t_ => return 0,",
		        "\t\t}"]
	else:
		undo = [f"\t\t// RFC 1662: an escaped byte is the original",
		        f"\t\t// exclusive-ored with 0x{xor:02X}, so undoing it is",
		        "\t\t// the same operation again -- which reverses escapes",
		        "\t\t// this generator never enumerated, as a peer with a",
		        "\t\t// non-zero ACCM will send.",
		        f"\t\tout[written] = input[read] ^ 0x{xor:02X};"]

	return [
		"",
		f"/// `{decl.name}`: {title}. A payload byte equal to the delimiter",
		f"/// (0x{delim:02X}) or the escape (0x{esc:02X}) is sent as the escape",
		"/// followed by a substitute. `out` needs 2 * len + 1 bytes.",
		f"pub fn {name}_encode(input: &[u8], len: usize,"
		" out: &mut [u8]) -> usize {",
		"\tlet mut written = 0usize;",
		"",
		"\tfor read in 0..len {",
		"\t\tmatch input[read] {",
		*arms,
		"\t\t\tbyte => {",
		"\t\t\t\tout[written] = byte;",
		"\t\t\t\twritten += 1;",
		"\t\t\t}",
		"\t\t}",
		"\t}",
		"",
		f"\tout[written] = 0x{delim:02X};\t// the frame delimiter",
		"\twritten + 1",
		"}",
		"",
		"/// The inverse, stopping at the delimiter.",
		f"pub fn {name}_decode(input: &[u8], len: usize,"
		" out: &mut [u8]) -> usize {",
		"\tlet mut read = 0usize;",
		"\tlet mut written = 0usize;",
		"",
		"\twhile read < len {",
		"\t\tlet byte = input[read];",
		"\t\tread += 1;",
		"",
		f"\t\tif byte == 0x{delim:02X} {{",
		"\t\t\tbreak;\t// the delimiter ends the frame",
		"\t\t}",
		"",
		f"\t\tif byte != 0x{esc:02X} {{",
		"\t\t\tout[written] = byte;",
		"\t\t\twritten += 1;",
		"\t\t\tcontinue;",
		"\t\t}",
		"",
		"\t\tif read >= len {",
		"\t\t\treturn 0;\t// truncated: an escape ended the input",
		"\t\t}",
		"",
		*undo,
		"\t\twritten += 1;",
		"\t\tread += 1;",
		"\t}",
		"",
		"\twritten",
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
			and _reads_bits(decl)
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
		# A polynomial over an extension field is Reed-Solomon: the same
		# family, a different code, and a different generator below. A
		# CRC's register is a machine word and its table is indexed by a
		# byte; a Reed-Solomon symbol is a field element and the division
		# runs over a generator polynomial, so the two share the schema
		# keyword and nothing else.
		if kernel.argument("field") is not None:
			return _reed_solomon(decl, prefix)
		return _polynomial(decl, prefix)
	if kernel.family is ast.KernelFamily.ONES_COMPLEMENT:
		return _ones_complement(decl, prefix)
	if kernel.family is ast.KernelFamily.TABLE:
		return _table(decl, prefix)
	if kernel.family is ast.KernelFamily.SHIFT:
		return _shift_register(decl, prefix)
	if kernel.family is ast.KernelFamily.STUFFING:
		return _stuffing(decl, prefix)
	if kernel.family is ast.KernelFamily.LINEAR:
		return _linear_block(decl, prefix)
	if kernel.family is ast.KernelFamily.PERMUTATION:
		return _permutation(decl, prefix)

	# No fallthrough: the branches above are every member of
	# `KernelFamily`, which mypy proved by calling a trailing
	# `return None` unreachable. This backend now DISPATCHES every
	# family C does -- the declines that remain are inside the handlers
	# (Reed-Solomon in `_polynomial`, an unnamed linear or permutation
	# code) rather than a family nobody wrote. A family added later
	# falls off the end and declines, which is the same answer the
	# unreachable line gave.


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


# ---------------------------------------------------------------------------
# Reed-Solomon over GF(2^m)
#
# The algorithm is `kernel_program`'s and must not be copied here: 26.457
# measured Reed-Solomon as 250 lines of C against 39 of shared derivation,
# and a third backend transcribing Berlekamp-Massey, the Chien search and
# Forney's formula by hand is the act 0017 was written to prevent. What
# this section decides is where Rust puts a `mut`.
#
# The tables are computed from the two parameters rather than copied. A
# transcribed log table is exactly the artefact that is wrong in one entry
# and produces a codec that works on most inputs.
# ---------------------------------------------------------------------------


def _reed_solomon(decl: ast.CodecDecl, prefix: str) -> list[str] | None:
	"""Rust's spelling of `kernel_program`'s encoder and decoder."""
	field = number(decl, "field")
	n     = number(decl, "n")
	k     = number(decl, "k")

	if field != 256 or not n or not k:
		return None

	primitive  = number(decl, "primitive", 0x11D)
	first_root = number(decl, "first_root", 0)
	nroots     = n - k
	size       = field - 1

	exp, log = gf_tables(field, primitive)

	# Constant term first, as C's and Python's are: the division loop
	# indexes from the low end, which is the standard formulation.
	generator = list(reversed(
		rs_generator_coefficients(nroots, first_root, exp, log, size)))

	name   = _ident(prefix, decl.name)
	upper  = name.upper()
	render = _RustRenderer(name)

	return [
		"",
		f"/// `{decl.name}`: Reed-Solomon({n}, {k}) over GF({field}),",
		f"/// primitive polynomial 0x{primitive:X}, first root"
		f" alpha^{first_root}.",
		"///",
		f"/// {nroots} parity symbols, correcting up to {nroots // 2} symbol"
		" errors",
		"/// anywhere in the block. Systematic: the message sits verbatim",
		"/// ahead of the parity, so a reader that trusts the block takes it",
		"/// with no decode at all.",
		"///",
		"/// Every table here is computed from those two numbers rather than",
		"/// copied, so a code nobody has standardised works as well as one",
		"/// that has -- and no transcription can be wrong in one entry.",
		"///",
		"/// This one is the antilog, doubled so a product of logs needs no",
		"/// modulo.",
		f"static {upper}_EXP: [u8; {len(exp)}] = [",
		*_rust_table_rows(exp),
		"];",
		"",
		f"static {upper}_LOG: [u8; {len(log)}] = [",
		*_rust_table_rows(log),
		"];",
		"",
		"/// The generator polynomial, multiplied out from its roots.",
		f"static {upper}_GENERATOR: [u8; {len(generator)}] = [",
		*_rust_table_rows(generator),
		"];",
		"",
		"/// The block length, which `decode` requires of its slice.",
		f"pub const {upper}_BLOCK: usize = {n};",
		"",
		"/// The message length, which `encode` requires of `data`.",
		f"pub const {upper}_DATA: usize = {k};",
		"",
		"/// The parity length: what `encode` writes, and the shortest",
		"/// `parity` slice it will accept.",
		f"pub const {upper}_PARITY: usize = {nroots};",
		"",
		f"fn {name}_mul(a: u8, b: u8) -> u8 {{",
		"\tif a == 0 || b == 0 {",
		"\t\treturn 0;",
		"\t}",
		f"\t{upper}_EXP[{upper}_LOG[a as usize] as usize",
		f"\t\t+ {upper}_LOG[b as usize] as usize]",
		"}",
		"",
		f"fn {name}_inv(a: u8) -> u8 {{",
		f"\t{upper}_EXP[{size} - {upper}_LOG[a as usize] as usize]",
		"}",
		"",
		"/// alpha raised to a power, with the exponent reduced first.",
		f"fn {name}_pow(power: usize) -> u8 {{",
		f"\t{upper}_EXP[power % {size}]",
		"}",
		"",
		"/// Systematic encode: the message is left alone and the parity is",
		"/// the remainder of dividing it, shifted, by the generator.",
		"///",
		f"/// Returns {nroots}, or 0 where the caller passed something this",
		"/// cannot encode.",
		f"pub fn {name}_encode(data: &[u8], parity: &mut [u8]) -> usize {{",
		"\tlet length = data.len();",
		"",
		"\t// C writes the parity through a bare pointer, so a short buffer",
		"\t// is an overflow nobody sees. Rust bounds-checks the slice, so",
		"\t// the same call is a panic instead -- an abort in `no_std`, which",
		"\t// is not an answer either. Refusing is, and 0 is already what",
		"\t// this program returns when the caller passed something wrong",
		"\t// (the length check below). A parity slice too short to hold the",
		"\t// result is that same mistake, so it says so the same way rather",
		"\t// than inventing a second code.",
		f"\tif parity.len() < {upper}_PARITY {{",
		"\t\treturn 0;",
		"\t}",
		"",
		*render.program(rs_encoder_program(nroots, k)),
		"}",
		"",
		"/// Correct `block` in place. Returns the number of symbols",
		"/// corrected, or -1 where the block holds more errors than the code",
		"/// can correct -- which it detects rather than guessing at, because",
		"/// a miscorrection is worse than a refusal.",
		f"pub fn {name}_decode(block: &mut [u8]) -> i32 {{",
		"\tlet length = block.len();",
		*render.program(rs_decoder_program(n, nroots, first_root, size)),
		"}",
	]


def _rust_table_rows(values: list[int]) -> list[str]:
	"""A field table's rows, sixteen to a line as C and Python write them."""
	return ["\t" + ", ".join(f"0x{value:02X}"
	                         for value in values[start:start + 16]) + ","
	        for start in range(0, len(values), 16)]


#: As C's and Python's: the program's arrays that are module tables rather
#: than locals, and so carry the codec's name.
_RUST_SHARED = {"generator"}

#: The program's one array of INDICES rather than field elements. C and
#: Python hold both in the same kind of array; Rust subtracts a `position`
#: entry from a length and indexes `block` with one, so a `u8` there would
#: need three casts and would still meet a mixed-type subtraction rustc
#: refuses. Declaring what it holds is cheaper than casting at every use.
_RUST_INDEX_ARRAYS = {"position"}

#: Rust's types for the two the program names. `u32` is `usize` because
#: every one of them is a subscript somewhere -- `at` is simultaneously a
#: subscript, an argument to `pow` and a comparand -- and a subscript in
#: Rust is a `usize` or it is a compile error.
_RUST_TYPE = {"u8": "u8", "u32": "usize"}


class _RustRenderer:
	"""How Rust spells the statements of `kernel_program`, and nothing else.

	No decision about Reed-Solomon is taken here; everything this class
	knows is which characters Rust uses for a loop, a binding and an
	array. Four differences from C's renderer, each measured:

	No casts. C promotes a `uint8_t` xor to `int`, so it narrows on the
	way back into a byte; Rust does not promote, so every one of C's
	`(uint8_t)` casts disappears and the arithmetic is `u8` throughout.

	No hoisted loop variables. C wants them declared ahead of the block
	and the program does not name them; Rust binds one per loop, which is
	what every other target does.

	`mut` is earned rather than given. `unused_mut` is an error under
	`-D warnings`, which every generated crate is built with, so a
	binding is `mut` only where something later assigns it -- five of the
	program's nineteen declarations are not, and that set is walked out
	of the tree rather than listed here, because a list is a thing to be
	wrong.

	Parentheses go where Rust wants them and not where C does. C's
	renderer brackets every `Binary` on the grounds that its precedence
	table is not worth a reader checking; doing the same here produced 22
	`unused_parens`, which `-D warnings` makes hard errors -- ten around
	subscripts, eight around assigned values, four around arguments.

	Indexing is plain `[i]`, with no `get().unwrap_or(0)` anywhere. The
	program says every index is provably in range and a renderer is
	entitled to rely on it. A fallback would not see a bad index in any
	case, because the one way to make one is an underflowing subtraction
	that panics first; and substituting a zero in an error-correcting
	decoder turns a refusal into a miscorrection, which
	`rs_decoder_program` names as the worse of the two.
	"""

	def __init__(self, name: str) -> None:
		self.name = name
		self.assigned: set[str] = set()

	# -- expressions --------------------------------------------------

	def expr(self, node: Expr) -> str:
		"""An operand of something with a precedence of its own."""
		if isinstance(node, Lit):
			return str(node.value)
		if isinstance(node, Name):
			return node.name
		if isinstance(node, Index):
			return f"{self.array(node.array)}[{self.bare(node.at)}]"
		if isinstance(node, Gf):
			args = ", ".join(self.bare(arg) for arg in node.args)
			return f"{self.name}_{node.op}({args})"
		return f"({self.bare(node)})"

	def bare(self, node: Expr) -> str:
		"""An expression the surrounding syntax already delimits: an
		assigned value, a subscript, an argument, an `if` condition."""
		if isinstance(node, Binary):
			return (f"{self.expr(node.left)} {node.op} "
			        f"{self.expr(node.right)}")
		return self.expr(node)

	def array(self, name: str) -> str:
		"""A local array keeps its name; a module table takes the codec's,
		which is what keeps two codecs' tables apart in one module."""
		if name in _RUST_SHARED:
			return f"{self.name.upper()}_{name.upper()}"
		return name

	# -- statements ---------------------------------------------------

	def program(self, body: tuple[Stmt, ...]) -> list[str]:
		"""A whole function body, at one tab."""
		self.assigned = _assigned_names(body)
		return self.render(body, 1)

	def render(self, body: tuple[Stmt, ...], depth: int) -> list[str]:
		pad = "\t" * depth
		out: list[str] = []

		for statement in body:
			if isinstance(statement, Blank):
				out.append("")
			elif isinstance(statement, Comment):
				out.extend(f"{pad}// {line}" for line in statement.lines)
			elif isinstance(statement, Array):
				kind = ("usize" if statement.name in _RUST_INDEX_ARRAYS
				        else "u8")
				out.append(f"{pad}let {self.binding(statement.name)}"
				           f"{statement.name} = "
				           f"[0{kind}; {statement.length}];")
			elif isinstance(statement, Declare):
				out.append(f"{pad}let {self.binding(statement.name)}"
				           f"{statement.name}: "
				           f"{_RUST_TYPE[statement.kind]} = "
				           f"{self.bare(statement.init)};")
			elif isinstance(statement, Assign):
				out.append(f"{pad}{self.expr(statement.target)} = "
				           f"{self.bare(statement.value)};")
			elif isinstance(statement, XorAssign):
				out.append(f"{pad}{self.expr(statement.target)} ^= "
				           f"{self.bare(statement.value)};")
			elif isinstance(statement, Increment):
				out.append(f"{pad}{statement.name} += 1;")
			elif isinstance(statement, Loop):
				out.extend(self.loop(statement, pad, depth))
			elif isinstance(statement, If):
				out.extend(self.branch(statement, pad, depth))
			elif isinstance(statement, Return):
				out.append(f"{pad}return {self.returned(statement.value)};")
			else:
				out.append(f"{pad}continue;")

		return out

	def binding(self, name: str) -> str:
		"""`mut ` where something later writes to this, and nothing where
		it does not: `unused_mut` is an error here."""
		return "mut " if name in self.assigned else ""

	def loop(self, statement: Loop, pad: str, depth: int) -> list[str]:
		"""A range, which is what Rust has instead of C's three clauses.

		The start carries a `usize` suffix where it is a literal. Every
		loop variable in this program is a subscript or an argument to
		`pow`, so inference would reach `usize` on its own in all of
		them -- but a bare `0..32` is an `i32` until something several
		lines away says otherwise. Pinning it at the range makes the loop
		readable on its own and costs a suffix.
		"""
		start = self.bare(statement.start)
		if isinstance(statement.start, Lit):
			start = f"{start}usize"
		limit = self.bare(statement.limit)
		span = (f"{start}..={limit}" if statement.inclusive
		        else f"{start}..{limit}")
		if statement.step != 1:
			span = f"({span}).step_by({statement.step})"
		return [
			f"{pad}for {statement.var} in {span} {{",
			*self.render(statement.body, depth + 1),
			f"{pad}}}",
		]

	def branch(self, statement: If, pad: str, depth: int) -> list[str]:
		out = [f"{pad}if {self.bare(statement.cond)} {{",
		       *self.render(statement.body, depth + 1)]
		if statement.orelse:
			out.append(f"{pad}}} else {{")
			out.extend(self.render(statement.orelse, depth + 1))
		out.append(f"{pad}}}")
		return out

	def returned(self, node: Expr) -> str:
		"""The decoder returns `i32` and counts in `usize`, so the one
		counter it returns is cast where C casts to `int`."""
		if isinstance(node, Lit):
			return str(node.value)
		if isinstance(node, Name):
			return f"{node.name} as i32"
		raise AssertionError("the decoder returns a literal or a counter")


def _assigned_names(body: tuple[Stmt, ...]) -> set[str]:
	"""Every name this program writes to, arrays included.

	Walked rather than listed: which of the bindings are never assigned
	is a property of the program, and a renderer carrying the answer
	would be a second place for it to be wrong.
	"""
	found: set[str] = set()
	for statement in body:
		if isinstance(statement, (Assign, XorAssign)):
			target = statement.target
			if isinstance(target, Name):
				found.add(target.name)
			elif isinstance(target, Index):
				found.add(target.array)
		elif isinstance(statement, Increment):
			found.add(statement.name)
		elif isinstance(statement, Loop):
			found |= _assigned_names(statement.body)
		elif isinstance(statement, If):
			found |= _assigned_names(statement.body)
			found |= _assigned_names(statement.orelse)
	return found
