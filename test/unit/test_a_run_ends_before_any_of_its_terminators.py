"""A run of records may end `before` its terminator, and at any of several.

A Matroska Cluster of unknown size runs until the ID of the next element of
the Segment's level, which is the next element's and not the Cluster's, and
there are nine such IDs. Three faults stood in the way, in all six readers:
`before` on a run of records consumed its terminator, so the Cluster swallowed
the next one's ID; several terminators were refused for a run; and the walk was
guarded by the terminator's own room, so an element shorter than a `before` or
`| end` terminator in the frame's last bytes was dropped (26.930).

Every verdict below is worked by hand from the bytes, as agreement among the
six is what went wrong before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, VERDICT, _build_driver, _inline_image,
                           c_verdict, python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;
struct item   { u8 tag; u8 n; u8 body[n]; }
struct upto   { item es[] before "\\xAA\\xBB"; u8 tail[remaining]; }
struct alts   { item es[] until "\\xFF" | "\\xEE\\xEE"; u8 after; }
struct opened { item es[] before "\\xAA\\xBB\\xCC" | "\\xDD" | end; }
struct closed { item es[] until "\\xAA\\xBB\\xCC"; u8 after; }
"""

#: struct, message -> the lines all four print, and `validate`'s verdict.
CASES = {
	# The terminator stays for `tail`: three bytes, not one.
	("upto", "0101aaaabb77"): ("es count=1", "tail len=3 first=170", "0"),
	("upto", "aabb"):         ("es count=0", "tail len=2 first=170", "0"),
	# No terminator: the frame ends a `before` run.
	("upto", "01000101aa"):   ("es count=2", "tail len=0 first=-1", "0"),
	# Either terminator, consumed by its own width.
	("alts", "0100eeee07"):   ("es count=1", "after 7", "0"),
	("alts", "0100ff07"):     ("es count=1", "after 7", "0"),
	("alts", "eeee07"):       ("es count=0", "after 7", "0"),
	# Neither: `07` begins an item the frame cuts, so the run never ended.
	("alts", "010007"):       ("es count=1", "after 7", "2"),
	# `ee 07` is half of one terminator, so an item, cut.
	("alts", "0100ee07"):     ("es count=1", "after 238", "2"),
	# Two two-byte items under a three-byte terminator: the second was
	# dropped by the old guard.
	("opened", "01000200"):   ("es count=2", None, "0"),
	("opened", "0100dd"):     ("es count=1", None, "0"),
	("opened", "0100aabbcc"): ("es count=1", None, "0"),
	("closed", "0100aabbcc07"): ("es count=1", "after 7", "0"),
}


def _section(output: str, struct: str) -> list[str]:
	return output.split(f"-- {struct}\n", 1)[1].split("\n-- ", 1)[0] \
		.splitlines()


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_end_the_run_where_the_bytes_say(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for (struct, message), (count, member, verdict) in CASES.items():
		for name, argv in command.items():
			lines = _section(fourway.answers(argv, bytes.fromhex(message),
			                                 tmp_path), struct)
			assert count in lines, (name, struct, message, lines)
			assert member is None or member in lines, \
				(name, struct, message, lines)
			assert f"validate {verdict}" in lines, \
				(name, struct, message, lines)


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_both_walkers_end_the_run_where_the_bytes_say(tmp_path: Path) -> None:
	blob  = _inline_image(SCHEMA)
	drive = _build_driver(tmp_path, blob, VERDICT)
	for (struct, message), (_count, _member, verdict) in CASES.items():
		shape = shape_named_text(SCHEMA, struct)
		data  = bytes.fromhex(message)
		assert c_verdict(tmp_path, blob, data, shape, drive) \
			== python_verdict(blob, data, shape) == verdict, (struct, message)


def test_only_the_run_that_consumes_one_terminator_is_framed() -> None:
	"""On a prefix, whether such a run ended turns on bytes not yet here."""
	from situc.layout import solve
	from situc.parser import parse_text
	from situc.resolve import resolve
	from situc.traverse import frameable
	parsed   = parse_text(SCHEMA)
	resolved = resolve(parsed, solve(parsed))
	framed   = {name for name, struct in resolved.structs.items()
	            if frameable(resolved.structs, struct)}
	assert "closed" in framed
	assert not framed & {"upto", "alts", "opened"}
