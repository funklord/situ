"""Ways of writing a schema fact that no target language decides.

One function so far, and the reason it is here rather than in a backend: how a
delimiter reads is a property of the delimiter. Three headers describing one
schema should spell CRLF the same way, and the second backend to need this
copied it before this module existed.
"""

from __future__ import annotations


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
