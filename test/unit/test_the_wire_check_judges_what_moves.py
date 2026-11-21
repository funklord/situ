"""`situc wire --check` calls BREAKING only what moves a byte or its meaning
(26.1129).

fuzznet (inbox 20261121T2000Z) took four contracts the check called
breaking where no byte moved: a trailing run gaining a bound, a trailing
counted run whose count gained `[min = 1, max = 8]`, an enum renamed with
its values unchanged, and a roster bundle whose `record` gained `[holds]`
and whose opaque 179-byte hop became a typed one -- fourteen BREAKING.
"""

from __future__ import annotations

import pytest

from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from situc.wire import compare, render


def _wire(source: str) -> str:
	schema = parse_text(source)
	return render(schema, resolve(schema, solve(schema)), "s.situ")


def _kinds(before: str, after: str) -> list[tuple[str, str, str]]:
	verdict = compare(_wire(before), _wire(after))
	return [(finding.kind, finding.subject, finding.detail)
	        for finding in verdict.findings]


def _breaking(found: list[tuple[str, str, str]]) -> list[str]:
	return [detail for kind, _, detail in found
	        if kind in ("breaking", "coverage")]


ROW = """endian big;
struct row { u16 content_type; u8 content[remaining]; }
"""

CHAIN = """endian big;
struct hop { u8 b[179]; }
struct chain { u8 hop_count; hop hops[hop_count]; }
"""


def test_a_trailing_run_gaining_a_bound_narrows() -> None:
	found = _kinds(ROW, ROW.replace("[remaining]", "[remaining] max 470"))
	assert not _breaking(found), found
	assert [kind for kind, _, _ in found] == ["backward"], found
	assert "content narrows 0.. -> 0..470" in found[0][2]


def test_a_trailing_run_losing_its_bound_widens() -> None:
	found = _kinds(ROW.replace("[remaining]", "[remaining] max 470"), ROW)
	assert [kind for kind, _, _ in found] == ["forward"], found


def test_a_trailing_count_gaining_bounds_narrows() -> None:
	"""`hops` behind the count goes `0..` -> `179..`, past a byte, and the
	signature names a byte order only past a byte: the `big` that appears
	is the renderer's, not the wire's."""
	found = _kinds(CHAIN, CHAIN.replace("u8 hop_count;",
	                                    "u8 hop_count [min = 1, max = 8];"))
	assert not _breaking(found), found
	assert any("hops narrows" in detail for _, _, detail in found), found


def test_a_member_that_is_not_last_still_shifts_what_follows() -> None:
	before = "endian big;\nstruct s { u8 n; u8 a[n]; u8 after; }\n"
	found = _kinds(before, before.replace("u8 n;", "u8 n [max = 4];"))
	assert any("every later field shifts" in detail
	           for detail in _breaking(found)), found


def test_a_trailing_width_that_moves_both_ways_still_breaks() -> None:
	before = "endian big;\nstruct s { u8 n [min = 2, max = 4]; u8 a[n]; }\n"
	found = _kinds(before, before.replace("[min = 2, max = 4]",
	                                      "[min = 3, max = 9]"))
	assert any("changes width" in detail for detail in _breaking(found)), \
		found


ENUM = """endian big;
enum fzn_revocation_object : u8 { a = 129, b = 133 }
struct r { fzn_revocation_object kind; u8 x; }
"""


def test_an_enum_renamed_with_its_values_is_one_enum() -> None:
	renamed = ENUM.replace("fzn_revocation_object", "fzn_revocation_kind")
	found = _kinds(ENUM, renamed)
	assert not _breaking(found), found
	assert any(kind == "api" and "one layout under two names" in detail
	           for kind, _, detail in found), found


@pytest.mark.parametrize("change", [("b = 133", "b = 134"),
                                    ("b = 133 }", "b = 133, c = 140 }")])
def test_an_enum_renamed_with_other_values_still_breaks(
		change: tuple[str, str]) -> None:
	renamed = ENUM.replace("fzn_revocation_object", "fzn_revocation_kind")
	found = _kinds(ENUM, renamed.replace(*change))
	assert any("mean something else" in detail
	           for detail in _breaking(found)), found


BUNDLE = """endian big;
struct roster { u8 version [must_eq = 1]; u16 n; u8 body[n]; }
struct roster_hop { u8 bytes[96]; }
struct chain_hop {
	authenticated body { u8 version [must_eq = 1]; u8 key[31]; }
	checksum u8 sig[64] covers(body);
}
struct bundle {
	u8 hop_count [max = 8];
	u16 record_len [min = 3, max = 300];
	u8 record[record_len];
	roster_hop hops[hop_count];
}
"""

TYPED = BUNDLE.replace("u8 record[record_len];",
                       "u8 record[record_len] [holds = roster];") \
              .replace("roster_hop hops", "chain_hop hops") \
              .replace("struct roster_hop { u8 bytes[96]; }\n", "")


def test_a_held_record_and_a_typed_hop_move_no_byte() -> None:
	found = _kinds(BUNDLE, TYPED)
	assert not _breaking(found), found
	said = {subject: (kind, detail) for kind, subject, detail in found}
	assert said["bundle[2]"][0] == "backward", found
	assert "gains `holds=roster`" in said["bundle[2]"][1]
	assert said["bundle[3]"][0] == "backward", found
	assert "reads its bytes as chain_hop" in said["bundle[3]"][1]
	assert said["roster_hop"][0] == "api", found


def test_a_typed_hop_turned_opaque_is_the_other_direction() -> None:
	found = _kinds(TYPED.replace("u8 record[record_len] [holds = roster];",
	                             "u8 record[record_len];"),
	               BUNDLE)
	said = {subject: kind for kind, subject, _ in found}
	assert said["bundle[3]"] == "forward", found


def test_a_hop_of_another_width_still_breaks() -> None:
	wider = TYPED.replace("u8 key[31];", "u8 key[32];")
	assert _breaking(_kinds(BUNDLE, wider))


def test_a_struct_sent_on_its_own_and_removed_still_breaks() -> None:
	before = "endian big;\nstruct a { u8 x; }\nstruct b { u8 y; }\n"
	found = _kinds(before, "endian big;\nstruct b { u8 y; }\n")
	assert ("breaking", "a") in [(kind, subject)
	                             for kind, subject, _ in found], found
