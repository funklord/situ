//! The Rust runtime for generated situ accessors (section 26.18).
//!
//! Rust expresses the capability system more naturally than any other target
//! situ has, and most of that costs nothing here because the language already
//! does it:
//!
//!   * **Invalidation (section 12.3) is the borrow checker.** A view holds a
//!     slice of the caller's buffer. Writing through a `&mut` while a `&` view
//!     is outstanding does not compile, so the generation counter the C runtime
//!     carries is not needed -- the check happens before the program runs.
//!   * **An error cannot be dropped.** `Result` is `#[must_use]`.
//!   * **A sealed region's gate cannot be constructed.** Its field is private
//!     to the module that defines it, so no code outside can make one, and the
//!     only thing that does is the open that checks the tag.
//!
//! `no_std`, no allocation, no panics in generated code: every fallible path
//! returns `Result`. That is the same discipline the C backend keeps, for the
//! same targets.
//!
//! Codecs bind the C implementation (decision 0017), which is where the one
//! `unsafe` in a situ program lives. Generated code marks it at the call site
//! rather than burying it.

#![no_std]
#![allow(dead_code)]

/// What can go wrong. The same failure classes the C runtime names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
	/// A view would not fit the buffer, or an index is past the end.
	Bounds,
	/// A field holds a value the schema does not admit.
	Constraint,
	/// A version field names something this build does not parse.
	Version,
	/// A tag has not verified, or a covered write left one stale.
	Tag,
	/// A sealed interior was reached before its stage ran.
	Stage,
	/// Not an error in the way the others are: the bytes so far are a valid
	/// prefix and more are needed. A stream reader gets this on every
	/// partial read, which is why it is separate from `Bounds` -- that one
	/// means a read went outside the buffer, which is a bug or an attack.
	Truncated,
}

impl Error {
	/// The C runtime's code as an `Error`. One place, because a tier-1
	/// codec's ABI reports failure the way every other C boundary here does
	/// -- an `situ_err_t` -- and a caller crossing it needs the same names
	/// the rest of this module uses (13.2a).
	///
	/// Anything unrecognised is `Constraint`: an implementation reporting a
	/// code this build does not name has still refused the input, and the
	/// one thing that must not happen is reading its output anyway.
	pub fn from_code(code: u32) -> Error {
		match code {
			1 => Error::Bounds,
			3 => Error::Version,
			4 => Error::Tag,
			5 => Error::Stage,
			7 => Error::Truncated,
			_ => Error::Constraint,
		}
	}
}

pub type Result<T> = core::result::Result<T, Error>;

/// The answer to "is a whole message here yet, and if not how many bytes?"
///
/// Its own type rather than `Result<usize>`, because both arms carry a number
/// and they mean different things: one is the message's length, the other is
/// a lower bound on it. A `Result` would have to drop one of them or smuggle
/// it through the error, and a caller framing a stream needs both -- the
/// first to consume, the second to size the next read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Framing {
	/// A whole message is present, and this is how long it is. Anything
	/// beyond it belongs to the next one.
	Complete(usize),
	/// Not yet. At least this many bytes are needed in total -- not this
	/// many more.
	Need(usize),
}

/// The obligations outstanding over a buffer: tags that no longer match the
/// bytes (section 14.2), and fields that no longer equal what they derive
/// from (section 16.1). One word for both, because a message is either ready
/// to send or it is not.
///
/// The other three backends hang this off a `message` that also owns the
/// buffer. Here it is a separate value the caller holds and passes, because a
/// message owning the bytes is the one thing this backend cannot have: a view
/// *borrows* the caller's slice, and that borrow is how section 12.3's
/// invalidation rule is enforced at compile time. Putting the buffer behind
/// another object would mean handing out the borrow from inside it, and then
/// the dirty word and the bytes would be borrowed together -- so marking a
/// bit would conflict with holding the view that wrote it.
///
/// Passing it separately also makes the cost visible in the signature, which
/// is what the message parameter buys in C.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Dirty(u32);

impl Dirty {
	#[inline]
	pub const fn new() -> Self {
		Self(0)
	}

	/// A covered write happened; what it invalidates is now stale.
	#[inline]
	pub fn mark(&mut self, bits: u32) {
		self.0 |= bits;
	}

	#[inline]
	pub fn clear(&mut self, bits: u32) {
		self.0 &= !bits;
	}

	#[inline]
	pub fn is_stale(&self, bits: u32) -> bool {
		self.0 & bits != 0
	}

	#[inline]
	pub const fn bits(&self) -> u32 {
		self.0
	}

	/// `Err(Error::Tag)` unless every obligation has been discharged.
	#[inline]
	pub fn transmittable(&self) -> Result<()> {
		if self.0 == 0 {
			Ok(())
		} else {
			Err(Error::Tag)
		}
	}
}

/// A big-endian read of `width` bytes at `at`.
#[inline]
/// Advance an offset by a length the message chose, and stop at the end.
///
/// A member placed after a variable-length region has an offset that is a sum
/// of lengths an attacker fills in: `examples/packet` with `hdr.length =
/// 0xffff` puts its tag 65581 bytes into a 62-byte message. Rust's answer to
/// the slice that follows is a panic, which in a `no_std` build is an abort --
/// a denial of service rather than a mitigation (26.27).
///
/// Saturating rather than wrapping, though `usize` here is wider than the
/// `u32` the C runtime uses: the two agree about where a member is, and
/// agreeing is the property four backends over one layout exist to keep.
pub fn advance(at: usize, by: usize, limit: usize) -> usize {
	let room = limit.saturating_sub(at);

	at + if by < room { by } else { room }
}

pub fn read_be(bytes: &[u8], at: usize, width: usize) -> u64 {
	let mut value: u64 = 0;
	let mut i = 0;
	while i < width {
		value = (value << 8) | bytes[at + i] as u64;
		i += 1;
	}
	value
}

/// The same, little end first.
#[inline]
pub fn read_le(bytes: &[u8], at: usize, width: usize) -> u64 {
	let mut value: u64 = 0;
	let mut i = width;
	while i > 0 {
		i -= 1;
		value = (value << 8) | bytes[at + i] as u64;
	}
	value
}

#[inline]
pub fn write_be(bytes: &mut [u8], at: usize, width: usize, value: u64) {
	let mut i = 0;
	while i < width {
		bytes[at + i] = (value >> (8 * (width - 1 - i))) as u8;
		i += 1;
	}
}

#[inline]
pub fn write_le(bytes: &mut [u8], at: usize, width: usize, value: u64) {
	let mut i = 0;
	while i < width {
		bytes[at + i] = (value >> (8 * i)) as u8;
		i += 1;
	}
}

/// Host order, for `endian native` (8.3).
///
/// `cfg!` rather than a constant situc writes into the output: situc runs on
/// the machine building the code and not on the machine running it, so the
/// order has to be decided by the compiler that has the target in front of it
/// (invariant 8). Both arms compile on both, and the dead one folds away.
///
/// This backend had neither, and read every `native` field big-endian --
/// silently, on every little-endian host. Nothing noticed because no schema in
/// the repository used host order until `examples/netlink`.
#[inline]
pub fn read_ne(bytes: &[u8], at: usize, width: usize) -> u64 {
	if cfg!(target_endian = "big") {
		read_be(bytes, at, width)
	} else {
		read_le(bytes, at, width)
	}
}

#[inline]
pub fn write_ne(bytes: &mut [u8], at: usize, width: usize, value: u64) {
	if cfg!(target_endian = "big") {
		write_be(bytes, at, width, value)
	} else {
		write_le(bytes, at, width, value)
	}
}

/// A bit-packed field, read through the bytes it lives in.
///
/// `msb` selects the bit numbering: most significant first is the wire
/// convention, least significant first is what registers usually want.
#[inline]
pub fn read_bits(bytes: &[u8], offset_bits: usize, width: usize, msb: bool) -> u64 {
	let first = offset_bits / 8;
	let last = (offset_bits + width - 1) / 8;
	let span = (last - first + 1) * 8;
	let raw = read_be(bytes, first, last - first + 1);

	let skip = offset_bits - first * 8;
	let shift = if msb { span - skip - width } else { skip };
	(raw >> shift) & ((1u64 << width) - 1)
}

#[inline]
pub fn write_bits(bytes: &mut [u8], offset_bits: usize, width: usize, msb: bool,
		value: u64) {
	let first = offset_bits / 8;
	let last = (offset_bits + width - 1) / 8;
	let span = (last - first + 1) * 8;
	let count = last - first + 1;
	let raw = read_be(bytes, first, count);

	let skip = offset_bits - first * 8;
	let shift = if msb { span - skip - width } else { skip };
	let mask = ((1u64 << width) - 1) << shift;

	write_be(bytes, first, count, (raw & !mask) | ((value & ((1u64 << width) - 1)) << shift));
}

/// Sign-extend a `width`-bit value held in the low bits of `raw`.
#[inline]
pub fn sign_extend(raw: u64, width: usize) -> i64 {
	if width >= 64 {
		return raw as i64;
	}
	let sign = 1u64 << (width - 1);
	((raw ^ sign).wrapping_sub(sign)) as i64
}

/// Content length of a nul-terminated field, bounded by its capacity.
#[inline]
pub fn nul_len(bytes: &[u8]) -> usize {
	let mut i = 0;
	while i < bytes.len() {
		if bytes[i] == 0 {
			return i;
		}
		i += 1;
	}
	bytes.len()
}

/// A byte position no byte occupies, for a format with no quote or escape.
pub const NO_BYTE: u32 = 0x100;

/// Where a delimited member's content stops (section 8.6.1).
///
/// The offset of the first occurrence of `delim` within `bytes`, or
/// `bytes.len()` where it is not there. The caller distinguishes the two: a
/// member whose delimiter is absent is truncated rather than empty, and a
/// getter is not the place to decide what to do about that.
#[inline]
pub fn scan(bytes: &[u8], delim: &[u8]) -> usize {
	let limit = bytes.len();

	if delim.is_empty() || delim.len() > limit {
		return limit;
	}
	let mut i = 0;
	while i + delim.len() <= limit {
		if &bytes[i..i + delim.len()] == delim {
			return i;
		}
		i += 1;
	}
	limit
}

/// The same, with a byte that makes the delimiter inert.
///
/// `quote` toggles: inside a quoted run the delimiter is content. `escape`
/// applies to the byte after it, including a quote byte and including itself.
/// Either may be `NO_BYTE`.
///
/// A quoted run left open finds no delimiter, which is the same answer as one
/// that is not there -- and the right one, since the content the schema
/// describes has not been terminated.
#[inline]
pub fn scan_relaxed(bytes: &[u8], delim: &[u8], quote: u32, escape: u32) -> usize {
	let limit = bytes.len();

	if delim.is_empty() || delim.len() > limit {
		return limit;
	}

	let mut quoted = false;
	let mut i = 0;
	while i + delim.len() <= limit {
		if escape != NO_BYTE && bytes[i] as u32 == escape {
			i += 2;		// the next byte is content, whatever it is
			continue;
		}
		if quote != NO_BYTE && bytes[i] as u32 == quote {
			quoted = !quoted;
			i += 1;
			continue;
		}
		if !quoted && &bytes[i..i + delim.len()] == delim {
			return i;
		}
		i += 1;
	}
	limit
}

/// A number written as digits, or `None` where it is not one (section 8.6.2).
///
/// `Option` rather than `Result`: there is one way to fail and naming it
/// would be a second error type for the same fact. Refused for the reasons a
/// protocol cares about -- an empty run, because no digits is not the number
/// zero; a byte that is not a digit in this base, including a trailing space;
/// and a value above `max`, which is the declared type's range.
///
/// Overflow is checked before it happens. Detecting it afterwards by looking
/// for a result that got smaller is a wrap, which panics in a debug build and
/// is merely wrong in a release one.
#[inline]
pub fn parse_uint(bytes: &[u8], radix: u32, max: u64) -> Option<u64> {
	if bytes.is_empty() || !(2..=16).contains(&radix) {
		return None;
	}

	let mut value: u64 = 0;
	for &byte in bytes {
		let digit = match byte {
			b'0'..=b'9' => (byte - b'0') as u32,
			b'a'..=b'f' => (byte - b'a') as u32 + 10,
			b'A'..=b'F' => (byte - b'A') as u32 + 10,
			_ => return None,
		};
		if digit >= radix {
			return None;
		}
		if value > (max - digit as u64) / radix as u64 {
			return None;
		}
		value = value * radix as u64 + digit as u64;
	}
	Some(value)
}

/// What `[trim]` removes: space and horizontal tab, and nothing else.
///
/// Not `u8::is_ascii_whitespace`, which also takes CR, LF and FF -- three of
/// which are delimiters in the protocols this is for, so trimming them would
/// eat the framing. This is HTTP's OWS.
#[inline]
pub fn is_ows(byte: u8) -> bool {
	byte == b' ' || byte == b'\t'
}

/// The value with the optional whitespace at either end removed.
#[inline]
pub fn trim(bytes: &[u8]) -> &[u8] {
	let mut start = 0;
	let mut end = bytes.len();

	while start < end && is_ows(bytes[start]) {
		start += 1;
	}
	while end > start && is_ows(bytes[end - 1]) {
		end -= 1;
	}
	&bytes[start..end]
}

/// ASCII case folding, and only ASCII: a protocol token is ASCII by
/// definition, and `to_lowercase` is Unicode.
#[inline]
pub fn ascii_ci_eq(a: &[u8], b: &[u8]) -> bool {
	a.len() == b.len()
		&& a.iter().zip(b).all(|(x, y)| x.eq_ignore_ascii_case(y))
}

/// Whether digits are the one spelling of their value (section 8.6.2).
///
/// A leading zero is another spelling of the same number, and above base ten
/// so is a change of case. `[minimal]` is what asks for this; without it the
/// field is NonCanonical and the map says so, which is the honest default --
/// most formats do permit `007`.
#[inline]
pub fn digits_minimal(bytes: &[u8], radix: u32) -> bool {
	if bytes.is_empty() {
		return false;
	}
	if bytes.len() > 1 && bytes[0] == b'0' {
		return false;
	}
	radix <= 10 || !bytes.iter().any(|&b| (b'A'..=b'F').contains(&b))
}

#[inline]
pub fn ascii_valid(bytes: &[u8]) -> bool {
	let mut i = 0;
	while i < bytes.len() {
		if bytes[i] > 0x7F {
			return false;
		}
		i += 1;
	}
	true
}

/// Strict UTF-8, as RFC 3629 requires: `core::str` already refuses overlong
/// forms and surrogate halves, so there is nothing to write here.
#[inline]
pub fn utf8_valid(bytes: &[u8]) -> bool {
	core::str::from_utf8(bytes).is_ok()
}

#[inline]
pub fn bcd_decode(packed: u64, digits: usize) -> u64 {
	let mut value = 0u64;
	let mut i = digits;
	while i > 0 {
		i -= 1;
		value = value * 10 + ((packed >> (4 * i)) & 0xF);
	}
	value
}

#[inline]
pub fn bcd_encode(mut value: u64, digits: usize) -> u64 {
	let mut packed = 0u64;
	let mut i = 0;
	while i < digits {
		packed |= (value % 10) << (4 * i);
		value /= 10;
		i += 1;
	}
	packed
}

#[inline]
pub fn bcd_valid(packed: u64, digits: usize) -> bool {
	let mut i = 0;
	while i < digits {
		if ((packed >> (4 * i)) & 0xF) > 9 {
			return false;
		}
		i += 1;
	}
	true
}

/// Decode one varint at `at`. Returns the value and the bytes it occupied, or
/// `None` if the slice ends mid-value or the value needs more than `max_bytes`.
///
/// A primitive, not a format: what a `tlv` region does with the number it reads
/// is the region's own grammar (section 9.5), and the generated walk carries
/// that. The C runtime once carried a whole tlv cursor with protobuf's wire
/// types written into it, which is the shape this deliberately is not.
#[inline]
pub fn varint_get(bytes: &[u8], at: usize, max_bytes: usize) -> Option<(u64, usize)> {
	let mut acc: u64 = 0;
	let mut shift = 0;
	let mut i = 0;

	while i < max_bytes && at + i < bytes.len() {
		let byte = bytes[at + i];

		if shift < 64 {
			acc |= ((byte & 0x7F) as u64) << shift;
		}
		shift += 7;
		i += 1;

		if byte & 0x80 == 0 {
			return Some((acc, i));
		}
	}

	None
}

/// The number of bytes `value` needs, encoded minimally. What a `minimal`
/// varint type is held to: a longer encoding of the same value is a second
/// encoding, and a schema that declares `minimal` does not admit one.
#[inline]
pub fn varint_len(mut value: u64) -> usize {
	let mut n = 1;
	while value >= 0x80 {
		value >>= 7;
		n += 1;
	}
	n
}

/// ZigZag, as protobuf's sint32 and sint64 use it: a small magnitude stays
/// short whether it is positive or negative.
#[inline]
pub fn zigzag_decode(raw: u64) -> i64 {
	((raw >> 1) as i64) ^ -((raw & 1) as i64)
}

#[inline]
pub fn zigzag_encode(value: i64) -> u64 {
	((value << 1) ^ (value >> 63)) as u64
}

/// Decode one big-endian base-128 varint: the high group first, otherwise the
/// same shape as leb128. ASN.1's identifier octets, MIDI's delta times and
/// SQLite's record varints are all this.
///
/// `max_bytes` is where the encoding stops and `terminal_bits` is what the last
/// permitted byte carries. Where that is eight there is no spare bit for a
/// continuation flag, so the byte is read whole and ends the value whatever its
/// high bit says -- SQLite's ninth byte, and the reason nine bytes hold
/// sixty-four bits where seven-bit groups would need ten.
#[inline]
pub fn varint_be_get(bytes: &[u8], at: usize, max_bytes: usize,
		terminal_bits: u32) -> Option<(u64, usize)> {
	let mut acc: u64 = 0;
	let mut i = 0;

	while i < max_bytes && at + i < bytes.len() {
		let byte = bytes[at + i];

		if terminal_bits == 8 && i + 1 == max_bytes {
			return Some(((acc << 8) | byte as u64, i + 1));
		}

		acc = (acc << 7) | (byte & 0x7F) as u64;
		i += 1;

		if byte & 0x80 == 0 {
			return Some((acc, i));
		}
	}

	None
}

/// The bytes `value` needs under `varint_be_get`'s rules, for the minimality
/// check: a longer encoding of one value is a second encoding.
#[inline]
pub fn varint_be_len(mut value: u64, max_bytes: usize, terminal_bits: u32) -> usize {
	let mut n = 1;
	while value >= 0x80 {
		value >>= 7;
		n += 1;
	}
	if terminal_bits == 8 && n > max_bytes {
		n = max_bytes;
	}
	n
}
