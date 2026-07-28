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
}

pub type Result<T> = core::result::Result<T, Error>;

/// A big-endian read of `width` bytes at `at`.
#[inline]
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
