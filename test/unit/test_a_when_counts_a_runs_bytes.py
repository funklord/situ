"""`count(s.key)` of a run of bytes: its bytes, no terminator (26.1171).

fuzznet's remote permit holds a host key of exactly 64 hex digits, ended by
a space or by the value's end. `size` counts a terminator that is there, so
64 digits and a space measure 65, like 65 digits at the end, and no `size`
rule tells them apart. `count` is the run's content -- each backend's `_len`
-- whether or not a terminator follows. Its group list bounds each name to
1 to 32 characters the same way. Both schemas are fuzznet's (inbox
20261124T1616Z), the verdicts worked by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.parser import parse_text

PERMIT = """target buffer;
endian big;
tokens rule_word { member = "member", admin = "admin", root = "root", none = "none", default = error, }
struct host_rule {
	u8 scheme[5] [must_eq = "host:"];
	u8 key[] until " " | end max 65 [chars = "0-9a-f"];
}
struct rule {
	peek u8 kind;
	variant body switch (kind) {
		case 'h': host_rule host;
		default: rule_word word until " " | end;
	}
}
struct remote_permit { rule rules[] while (1) | end; }
when any(r in remote_permit.rules: r.kind == 'n') && count(remote_permit.rules) > 1
	refuse none_alone "none stands alone";
when count(host_rule.key) != 64 refuse key_length "a host is host: and 64 hex digits";
"""

KEY = b"0123456789abcdef" * 4

PERMIT_CASES = [
	("a host at the end", b"host:" + KEY, "0"),
	("a host, then a word", b"host:" + KEY + b" root", "0"),
	("a word, then a host", b"admin host:" + KEY, "0"),
	("65 digits at the end", b"host:" + KEY + b"a", "2"),
	("63 digits, then a word", b"host:" + KEY[:63] + b" root", "2"),
	("63 digits at the end", b"host:" + KEY[:63], "2"),
	("none alone", b"none", "0"),
	("none beside root", b"none root", "2"),
	("two words", b"root admin", "0"),
]

GROUP = """target buffer;
endian big;
struct member_name { u8 name[] until " " | end max 33 [chars = "a-z0-9._-"]; }
struct member_list { member_name names[] while (1) | end max 16; }
when !unique(g in member_list.names: g.name) refuse twice "a name twice";
when count(member_name.name) < 1 || count(member_name.name) > 32
	refuse name_length "a name is 1 to 32 characters";
"""

GROUP_CASES = [
	("two names", b"a b", "0"),
	("a name twice", b"a a", "2"),
	("an empty name between", b"a  b", "2"),
	("32, then another", b"x" * 32 + b" y", "0"),
	("32 at the end", b"x" * 32, "0"),
	("33 at the end", b"x" * 33, "2"),
	("33, then another", b"x" * 33 + b" y", "2"),
	# A name ended by a space and the same name at the end are one name:
	# `unique` compares content, which the C walker compared with its
	# terminator and the Python walker declined (found here).
	("first and last alike", b"a b a", "2"),
	("a prefix between", b"ab a ab", "2"),
	("a prefix at the end", b"ab a", "0"),
]


def _verdicts(tmp_path: Path, schema: str, top: str,
              cases: list[tuple[str, bytes, str]]) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(schema, encoding="ascii")
	command = fourway.build(tmp_path, source)
	for label, data, verdict in cases:
		for name, argv in command.items():
			held = fourway.answers(argv, data, tmp_path) \
				.split(f"-- {top}\n")[1].split("\n-- ")[0]
			line = next(one for one in held.splitlines()
			            if one.startswith("validate "))
			assert line.split()[1] == verdict, (label, name, held)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize(("schema", "top", "cases"), [
	(PERMIT, "remote_permit", PERMIT_CASES),
	(GROUP, "member_list", GROUP_CASES),
], ids=["permit", "group"])
def test_all_four_count_a_runs_bytes(tmp_path: Path, schema: str, top: str,
                                     cases: list[tuple[str, bytes, str]]) -> None:
	_verdicts(tmp_path, schema, top, cases)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs a C compiler")
@pytest.mark.parametrize(("schema", "top", "cases"), [
	(PERMIT, "remote_permit", PERMIT_CASES),
	(GROUP, "member_list", GROUP_CASES),
], ids=["permit", "group"])
def test_both_walkers_count_a_runs_bytes(tmp_path: Path, schema: str, top: str,
                                         cases: list[tuple[str, bytes, str]]) -> None:
	from test_walker_c import (c_verdict, packed_text, python_verdict,
	                           shape_named_text)
	blob  = packed_text(schema)
	shape = shape_named_text(schema, top)
	for label, data, verdict in cases:
		assert python_verdict(blob, data, shape) == verdict, label
		assert c_verdict(tmp_path, blob, data, shape) == verdict, label


def test_a_count_no_reader_takes_is_refused() -> None:
	"""A text number's bracket is its digits, and `count` of one is no
	reader's question; nor is a run of wider elements, yet."""
	text = """target buffer;
endian big;
struct s { decimal u32 n[4]; u16 w[2]; }
when count(s.n) > 1 refuse bad "no";
"""
	with pytest.raises(SituError):
		parse_text(text, path="s.situ")


#: (schema, struct, packet, whether the length expert fires), for the
#: dissector, which reads the content length its own scan left behind.
DISSECTED = [
	(PERMIT, "host_rule", b"host:" + KEY, False),
	(PERMIT, "host_rule", b"host:" + KEY + b" root", False),
	(PERMIT, "host_rule", b"host:" + KEY + b"a", True),
	(PERMIT, "host_rule", b"host:" + KEY[:63], True),
	(PERMIT, "host_rule", b"host:" + KEY[:63] + b" root", True),
	(GROUP, "member_name", b"x" * 32, False),
	(GROUP, "member_name", b"x" * 32 + b" y", False),
	(GROUP, "member_name", b"x" * 33, True),
	(GROUP, "member_name", b" y", True),
]


def test_the_dissector_counts_a_runs_bytes(tmp_path: Path) -> None:
	"""The dissector holds the rule too: `count(key)` is the `key_len` its
	scan of the member declared, asked to exist first, so a member it did
	not reach leaves the rule silent rather than failing to run."""
	from situc.dissector import generate
	from situc.layout import solve
	from situc.resolve import resolve
	from test_dissector import LUA, read_back_experts
	if LUA is None:
		pytest.skip("no Lua interpreter")
	for schema, struct, packet, fires in DISSECTED:
		parsed = parse_text(schema, path="s.situ")
		lua = tmp_path / "s.lua"
		lua.write_text(generate(parsed, resolve(parsed, solve(parsed)), "s"),
		               encoding="ascii")
		experts = read_back_experts(lua, struct, packet)
		said = [abbrev for abbrev, _, _ in experts
		        if abbrev.endswith(("key_length", "name_length"))]
		assert bool(said) == fires, (struct, packet, experts)
