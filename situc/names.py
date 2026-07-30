"""Ways of writing a schema fact that no target language decides.

One function so far, and the reason it is here rather than in a backend: how a
delimiter reads is a property of the delimiter. Three headers describing one
schema should spell CRLF the same way, and the second backend to need this
copied it before this module existed.
"""

from __future__ import annotations

import re
from collections.abc import Callable


def render_delimiter(delim: bytes) -> str:
	"""A delimiter as a reader of the generated code would write it.

	`"\\r\\n"` rather than `{0x0D, 0x0A}`: the comment is there to be checked
	against the specification somebody is implementing, and that specification
	says CRLF.
	"""
	named = {0x0D: "\\r", 0x0A: "\\n", 0x09: "\\t", 0x00: "\\0"}
	shown = "".join(named.get(byte, chr(byte) if 0x20 <= byte < 0x7F
	                          else f"\\x{byte:02x}")
	                for byte in delim)
	return f'`"{shown}"`'


def over_fields(names: list[str], source: str,
		getter: Callable[[str], str]) -> str:
	"""A schema expression rewritten so each field name is a read of it.

	`(hdr_ext_len + 1) * 8 - 2` becomes the same arithmetic over whatever the
	backend calls reading `hdr_ext_len`. The expression's shape is the
	schema's and identical in every target; only the read differs, which is
	the same split `situc.invariant` makes for the same reason.

	**Longest name first**, or `len` rewrites the `len` inside `hdr_ext_len`
	and the result names a getter that does not exist. That is not
	hypothetical: those two fields sit side by side in an IPv6 extension
	header, which is the schema this was written for.
	"""
	for name in sorted(names, key=len, reverse=True):
		source = re.sub(rf"\b{re.escape(name)}\b", getter(name), source)
	return source


def translate_operators(source: str, *, conj: str, disj: str,
		ne: str, neg: str) -> str:
	"""A schema condition in a target that does not spell C's operators.

	The schema's operators are C's, which is a choice the language made once
	and C, C++ and Rust are all happy with. Python spells three of them in
	words and Lua spells four, so both needed this -- and both needed it in
	the same order, which is the reason it is one function.

	**`!=` before `!`.** Rewriting negation first eats the `!` of an
	inequality and leaves `not =`, which is not an expression in either
	language. It is one line either way and the wrong line reads fine.
	"""
	source = source.replace("!=", "\x00")		# park it out of reach of `!`
	source = re.sub(r"!", neg, source)
	source = source.replace("\x00", ne)
	source = source.replace("&&", conj).replace("||", disj)
	return re.sub(r"\s+", " ", source).strip()
