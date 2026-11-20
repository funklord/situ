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
