"""`situc wire --check` compares a run that `holds` a struct as its members.

fuzznet (inbox 20261120T1400Z) replaced fourteen fields with a run holding
a struct of exactly those fields -- same order, same widths, no byte
moved -- and the check reported fourteen BREAKING removals; the contract
was rewritten on the golden vectors' word, the check unable to say so.
And dropping `max` from the run, which does widen the message, was one
line among those fourteen (26.1118).
"""

from __future__ import annotations

from situc.wire import compare, render
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

FIELDS = """target buffer;
endian big;
struct req { u8 kind; u16 words_len; u8 words[words_len]; u32 id; }
"""

HELD = """target buffer;
endian big;
struct ask { u16 words_len; u8 words[words_len]; u32 id; }
struct req { u8 kind; u8 tail[remaining] max 489 [holds = ask]; }
"""


def _wire(source: str) -> str:
	schema = parse_text(source)
	return render(schema, resolve(schema, solve(schema)), "s.situ")


def test_moving_members_into_a_held_struct_moves_no_byte() -> None:
	verdict = compare(_wire(FIELDS), _wire(HELD))
	kinds = {(finding.kind, finding.subject) for finding in verdict.findings}
	assert not any(kind == "breaking" for kind, _ in kinds), verdict.findings
	assert ("api", "req") in kinds, verdict.findings


def test_dropping_the_runs_bound_is_named_as_itself() -> None:
	unbounded = HELD.replace(" max 489", "")
	verdict = compare(_wire(HELD), _wire(unbounded))
	said = [finding for finding in verdict.findings
	        if finding.subject == "req"]
	assert len(said) == 1, verdict.findings
	assert said[0].kind == "backward"
	assert "tail holds" in said[0].detail and "0..489 -> ask 0.." \
		in said[0].detail


def test_a_held_struct_that_differs_still_breaks() -> None:
	"""Seeing through the run is not seeing past it: a held member of
	another width is a break, as it would be inline."""
	wider = HELD.replace("u32 id; }\nstruct req", "u64 id; }\nstruct req")
	verdict = compare(_wire(HELD), _wire(wider))
	assert any(finding.kind == "breaking"
	           and finding.subject.startswith("req[")
	           for finding in verdict.findings), verdict.findings


SIZED = """target buffer;
endian big;
struct device { u8 id[32]; }
struct act { u16 kind; u16 signer_len; u8 signer[signer_len]; u8 sig[64]; }
"""


def test_a_run_sized_by_a_field_is_not_spliced() -> None:
	"""fuzznet (inbox 20261120T2010Z): a run sized by `signer_len` gained
	`[holds = device]`, a 32-byte struct, and the check spliced the
	struct's members in the run's place -- so `signer` read as narrowed
	to 32 and `sig`, after it, as 64 -> 32, where no byte had moved. The
	run's length is its field's; only the reading of it changed, and that
	is all the check may say."""
	held = SIZED.replace("u8 signer[signer_len];",
	                     "u8 signer[signer_len] [holds = device];")
	findings = compare(_wire(SIZED), _wire(held)).findings
	assert not any("width" in finding.detail for finding in findings), \
		findings
	assert [finding.subject for finding in findings] == ["act[2]"], findings
	assert "holds=device" in findings[0].detail


def test_a_gained_holds_is_a_tightening() -> None:
	"""fuzznet (inbox 20261121T0400Z): `holds` added to a byte run was
	reported BREAKING, "a deployed peer misreads these bytes". No peer
	misreads: the bytes are what they were, and the held struct decides
	which of them `validate` accepts -- a narrowing, as a tightened `max`
	is. Dropped, it is the loosening."""
	held = SIZED.replace("u8 signer[signer_len];",
	                     "u8 signer[signer_len] [holds = device];")
	gained = compare(_wire(SIZED), _wire(held)).findings
	assert [finding.kind for finding in gained] == ["backward"], gained
	assert "misread" not in gained[0].detail
	dropped = compare(_wire(held), _wire(SIZED)).findings
	assert [finding.kind for finding in dropped] == ["forward"], dropped
