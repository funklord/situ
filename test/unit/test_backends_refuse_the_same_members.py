"""What one backend refuses, all four refuse.

The fallthrough check next door guards the note that *lies* -- "not in the
static subset yet" for a construct the language has. It does not guard the note
that tells the truth. `packet.tag` regressed under an accurate refusal, "this
backend cannot resolve where the tag sits", and nothing caught it: the note was
correct, and C emitted the accessor anyway.

So this asks the other question. For every schema in the repository, which
members does each backend decline to give an accessor to? Where the four
disagree, one of them is ahead and the schema means different things in
different languages -- which is the one property every backend claims.

Three constructs were found this way before this file existed: a member after a
`coded` region, an array of wide scalars, and `opaque` regions. None was on
26.31's list.

It compares *which* members are refused, not the wording -- the wording differs
by design, four languages having four ways to write a comment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve

from every_schema import ROOT, SCHEMAS, ids

#: What a refusal reads like. Taken from the emitters rather than invented --
#: every one of these is a phrase some backend writes when it declines to emit
#: an accessor, and the test below pins that they are still written.
REFUSALS = (
	"cannot resolve",
	"not in the static subset",
	"not emitted by this backend",
	"not emitted yet",
	"has no fixed size",
	"has no extent",
	"no single size",
	"no length this",
	"not a struct",
	"No index for",
	"No offset cache",
)

#: `struct.member`, and the synthesised `<reservedN>` the compiler names an
#: unnamed field. Matched against the schema's own paths afterwards, because
#: generated code is full of dotted identifiers -- `self.DIRTY_TAG`,
#: `owner.clear_dirty` -- and the first version of this reported those as
#: members one backend had refused.
PATH = re.compile(r"\b([A-Za-z_]\w*\.(?:<\w+>|[A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)*)")

#: Members one backend may refuse and another emit, with why. Each entry is a
#: divergence somebody argued for; the list being short is what makes a new one
#: something to think about rather than something to add.
EXEMPT = {
	# Decision 0017: the codec implementation is C's, and calling it from
	# Python means loading a shared object from a path this generator would
	# have to invent. The note names the symbol and the size instead.
	("python", "data_block.body"): "no decode: the codec is C's (0017)",
}


def refused(text: str, paths: set[str]) -> set[str]:
	"""Members this output declines to give an accessor to.

	Comments wrap, so a note's phrase and the path it names often sit on
	different lines. The text is flattened first -- which is why this reads a
	joined blob rather than looping over lines, and why the first version of
	this missed every multi-line note it was written to find.
	"""
	flat  = re.sub(r"\s*\n\s*[/*#]*\s*", " ", text)
	found: set[str] = set()

	for phrase in REFUSALS:
		for match in re.finditer(re.escape(phrase), flat):
			window = flat[max(0, match.start() - 120):match.end() + 40]

			# `required` declines to *frame the struct*, naming no member --
			# "one of its members has no length this can compute" (20.3). The
			# window before it catches whatever accessor happens to precede
			# it, which in Python is the run this note is about and in the
			# other three is not: the same schema then looked like a
			# disagreement about `reports` (26.36).
			if "`required`" in window or "_required`" in window:
				continue

			found.update(name for name in PATH.findall(window)
			             if name in paths)

	return found


def emitted(path: Path) -> tuple[dict[str, str], set[str]]:
	"""Each backend's output, and every member path the schema declares."""
	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	name     = path.stem

	paths = {entry.placement.path
	         for struct in resolved.structs.values()
	         for entry in struct.entries}

	return {
		"c":      generate_c(schema, resolved, name).header,
		"cpp":    generate_cpp(schema, resolved, name).header,
		"python": generate_py(schema, resolved, name).module,
		"rust":   generate_rs(schema, resolved, name).module,
	}, paths


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_the_backends_refuse_the_same_members(path: Path) -> None:
	texts, paths = emitted(path)
	sets  = {backend: refused(text, paths) for backend, text in texts.items()}
	known = {member for backend, member in EXEMPT}

	split: list[str] = []
	for member in sorted(set().union(*sets.values())):
		if member in known:
			continue
		refusing = sorted(b for b, held in sets.items() if member in held)
		if 0 < len(refusing) < len(sets):
			split.append(f"{member}: refused by {refusing}, emitted by "
			             f"{sorted(set(sets) - set(refusing))}")

	assert not split, (
		f"{path.name} means different things in different languages:\n  "
		+ "\n  ".join(split)
		+ "\n\nEvery backend claims to describe the same bytes. Where one "
		"emits an accessor and another declines, that claim is false for this "
		"schema -- emit it everywhere, or record the divergence in EXEMPT with "
		"the reason."
	)


def test_the_refusal_phrases_are_still_written() -> None:
	"""A phrase list that matches nothing makes this file pass forever.

	Checked against the emitters' own source: each phrase has to be one some
	backend still writes. A phrase nobody writes is either a construct that
	became reachable -- good, and the entry should go -- or a rewording that
	slipped past this file.
	"""
	sources = "\n".join(
		(ROOT / "situc" / "codegen" / backend / "emit.py").read_text(
			encoding="ascii")
		for backend in ("c", "cpp", "python", "rust"))

	# Adjacent string literals joined first: an emitter wraps a long note
	# across two of them, so `"...which is not" " a struct this..."` holds a
	# phrase that appears in no single line of the source. Grepping the source
	# unjoined reported it missing, which is the same wrapping problem the
	# reader above has and the reason both flatten first.
	joined = re.sub(r'"\s*\n\s*(?:f?")', "", sources)

	unused = [phrase for phrase in REFUSALS if phrase not in joined]
	assert not unused, (
		f"these refusal phrases are no longer written by any backend: "
		f"{unused}. Either a construct became reachable and the entry should "
		f"go, or a note was reworded and this file stopped seeing it."
	)


#: A schema whose coded region changes length, so no backend can compute the
#: region's encoded extent and none of them emits a decode. That is the case
#: where a consumer most needs to be told what to call.
UNDECODABLE = """target buffer;
endian big;
bit_order msb_first;
codec slip {
	kernel = stuffing(worst_case = 2, per = 1, unit = byte, code = slip);
}
impl slip derived;
struct frame { coded body(slip) { u8 payload[4]; } }
"""


def test_a_declined_decode_names_the_entry_point_in_every_backend() -> None:
	"""Refusing to decode is fine; refusing without naming the remedy is not.

	Measured before this existed: for a length-changing coded region the four
	backends gave three different answers about which entry point a consumer
	could reach. C declared the derived pair because C defines them, C++ and
	Rust declared the tier-1 symbol and not the derived one -- backwards,
	since the declared symbol is the one situ does not control -- and Python
	declared neither. A consumer of the Rust module got framing accessors and
	no way to use them.

	The symbol is decided once in `traverse.codec_entry_point`, so this asks
	each backend whether it says it. Naming it is the weakest thing all four
	can do; whether a module should also *declare* it is a question about the
	public shape of four APIs and is open.
	"""
	source   = Source("<undecodable>", UNDECODABLE)
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))

	outputs = {
		"c":      generate_c(schema, resolved, "u").header,
		"cpp":    generate_cpp(schema, resolved, "u").header,
		"python": generate_py(schema, resolved, "u").module,
		"rust":   generate_rs(schema, resolved, "u").module,
	}

	# The premise: nobody decodes this region, or the test is asking about a
	# case that does not arise and would pass for the wrong reason.
	for backend, text in outputs.items():
		assert "body_decode" not in text, (
			f"{backend} decodes the region after all, so this schema no "
			f"longer exercises a declined decode")

	silent = [backend for backend, text in outputs.items()
	          if "situ_slip_decode" not in text]
	assert not silent, (
		f"{silent}: declined to decode a coded region without naming "
		f"`situ_slip_decode`, so a consumer is told the decode is somebody "
		f"else's job and not whose")


def test_the_exemptions_are_still_divergences() -> None:
	"""An exemption for something no longer split is a note claiming a
	difference that is not there. Invariant 11, one level up."""
	stale: list[str] = []

	for path in SCHEMAS:
		texts, paths = emitted(path)
		sets = {backend: refused(text, paths)
		        for backend, text in texts.items()}
		for (backend, member), why in EXEMPT.items():
			if member in set().union(*sets.values()):
				refusing = {b for b, held in sets.items() if member in held}
				if refusing == set(sets) or not refusing:
					stale.append(f"{backend}/{member}: {why}")

	assert not stale, (
		f"exempted but no longer a divergence: {sorted(set(stale))}"
	)
