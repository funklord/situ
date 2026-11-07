"""What each backend calls for a varint's encoding.

One answer for every reader and writer, because each site deciding for itself
read a `be128` length as leb128 in all four backends and held a `minimal` one
to leb128's count in two (26.919).
"""

from __future__ import annotations

from situc import ast


def varint_stem(declared: ast.VarintDecl | None) -> str:
	"""The runtime's name for an encoding: `<stem>_get`, `_len` and `_put`.

	`None` is a tag with no declaration, which has only ever meant the
	protobuf form.
	"""
	if declared is None:
		return "varint"
	return {ast.VarintEncoding.LEB128: "varint",
	        ast.VarintEncoding.BE128:  "varint_be",
	        ast.VarintEncoding.QUIC:   "varint_quic",
	        ast.VarintEncoding.DER:    "varint_der",
	        ast.VarintEncoding.COMPACTSIZE: "varint_compactsize",
	        ast.VarintEncoding.LENENC: "varint_lenenc",
	        ast.VarintEncoding.CBOR:   "varint_cbor",
	        ast.VarintEncoding.HPACK:  "varint_hpack",
	        ast.VarintEncoding.BERTAG: "varint_bertag",
	        ast.VarintEncoding.SLEB128: "varint_sleb",
	        ast.VarintEncoding.EBML:   "varint_ebml",
	        ast.VarintEncoding.EBML_ID: "varint_ebml_id",
	        ast.VarintEncoding.UTF8:   "varint_utf8",
	        ast.VarintEncoding.BIJECTIVE128: "varint_bijective",
	        ast.VarintEncoding.LEBPREFIX: "varint_lebprefix"}[declared.encoding]


def varint_limits(declared: ast.VarintDecl | None, suffix: str = "") -> str:
	"""What `_get` takes after where to read: the longest it may be, and for
	`be128` what its last byte carries. `suffix` is a literal's, `u` in C."""
	if declared is None:
		return f"10{suffix}"
	if declared.encoding is ast.VarintEncoding.BE128:
		return (f"{declared.max_bytes}{suffix},"
		        f" {declared.terminal_bits}{suffix}")
	if declared.encoding in ast.PREFIXED:
		return f"{declared.prefix}{suffix}, {declared.max_bytes}{suffix}"
	return f"{declared.max_bytes}{suffix}"


def varint_measure(declared: ast.VarintDecl | None, value: str,
		suffix: str = "") -> str:
	"""The arguments of `_len`, which only `be128` needs more of."""
	if declared is not None and declared.encoding is ast.VarintEncoding.BE128:
		return f"{value}, {varint_limits(declared, suffix)}"
	if declared is not None and declared.encoding in ast.PREFIXED:
		return f"{value}, {declared.prefix}{suffix}"
	return value


def varint_put_tail(declared: ast.VarintDecl, suffix: str = "") -> str:
	"""What `_put` takes after the value."""
	if declared.encoding is ast.VarintEncoding.BE128:
		return f", {varint_limits(declared, suffix)}"
	if declared.encoding in ast.PREFIXED:
		return f", {declared.prefix}{suffix}"
	return ""
