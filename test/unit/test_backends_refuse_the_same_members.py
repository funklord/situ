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
from situc.codegen.c.names import c_name
from situc.codegen.cpp.names import class_name
from situc.codegen.rust.emit import _pascal
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse, parse_text
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
	# Python and Rust wrote *nothing* for a variant arm whose type has no
	# computable extent, so their refusal sets were empty, the comparison
	# compared nothing, and the assertion passed -- a gate over an empty list
	# reporting success as loudly as a real pass. They say this now (26.190).
	#
	# Only this one was added. C's "cannot be measured" and C++'s "not a
	# shape this backend reaches into yet" are also unlisted, and adding them
	# reported divergences that are not there: the scoring takes any path
	# within 120 characters before a phrase, and those two notes name a
	# *type* rather than the member, so a neighbouring arm's path is swept in
	# -- `packet.body.puback` was reported as refused by C++ while C++ emits
	# two definitions for it. This phrase names its own member, so it cannot
	# bleed. What the gate still cannot see is recorded in 26.190 rather than
	# papered over by widening the window.
	"No accessor for",
)

#: Phrases that name their member *after* themselves rather than before.
#:
#: The window below reaches 120 characters back and 40 forward, because a
#: note like "`x` cannot be measured" trails the path it is about. A note
#: that opens with the path -- "No accessor for `x`: ..." -- is the other
#: shape, and reading backwards from it sweeps in whatever member happened
#: to be emitted just above. That reported `nl_message.body.terminator` as
#: refused by Python while all four backends define it, and
#: `packet.body.puback` as refused by C++ while C++ defines two (26.190).
NAMES_ITS_MEMBER = frozenset({"No accessor for"})

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

	# The five arms whose type has no computable extent used to sit here:
	# C emitted an offset accessor and the other three emitted nothing, and
	# 26.190 left it exempt because "either C's offset accessor is a
	# capability the other three should have, or it is one C should not
	# offer". The capability map answers it -- `packet.body.publish` is
	# `offset=Dynamic` and its members are `FrameStatic` from there -- so
	# the other three emit it too now (26.209), and the exemption is gone
	# rather than reworded.
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
			window = (flat[match.end():match.end() + 80]
			          if phrase in NAMES_ITS_MEMBER
			          else flat[max(0, match.start() - 120):match.end() + 40])

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


#: A checksum whose coverage has no single byte range, and the same schema
#: with one. Two regions with a member between them, which no committed
#: schema has together with an `is <codec>` clause -- so nothing generated
#: this until 26.367 went looking.
GAPPED = """endian big;
bit_order msb_first;
codec ic { kernel = ones_complement(width = 16); }
impl ic derived;
struct S {
	authenticated one { u8 a; }
	u8 gap;
	authenticated two { u8 b; }
	checksum u8 c[2] covers(one, two) is ic;
}
"""

CONTIGUOUS = """endian big;
bit_order msb_first;
codec ic { kernel = ones_complement(width = 16); }
impl ic derived;
struct S {
	authenticated body { u8 a; u8 b; }
	checksum u8 c[2] covers(body) is ic;
}
"""


def four_ways(text: str) -> dict[str, str]:
	schema   = parse(Source("<gap>", text))
	resolved = resolve(schema, solve(schema))
	return {
		"c":      generate_c(schema, resolved, "u").header,
		"cpp":    generate_cpp(schema, resolved, "u").header,
		"python": generate_py(schema, resolved, "u").module,
		"rust":   generate_rs(schema, resolved, "u").module,
	}


#: A checksum over a codec, and the codec's name. Reed-Solomon is a
#: polynomial over an extension field: the parity is symbols rather than a
#: digest and it comes back, so it is a different code from a CRC.
#:
#: This was `crc7_mmc` when the guard was written and 26.369 implemented that
#: loop the next day, which is the hazard a fixture like this carries -- a
#: test whose subject acquires an implementation stops asking its question in
#: silence. The check below no longer depends on the fixture being
#: underivable: it asks each backend whether it CAN write the codec and holds
#: the output to that answer, so a backend that learns a family is covered
#: rather than being reported as a divergence.
CODEC_CASES = (
	("rs_255_223", """endian big;
bit_order msb_first;
codec rs_255_223 {
	kernel = polynomial(width = 8, poly = 0x11D, field = 256, n = 255, k = 223);
}
impl rs_255_223 derived;
struct S {
	authenticated body { u8 a; u8 b; }
	checksum u8 c[2] covers(body) is rs_255_223;
}
"""),
	("crc16", """endian big;
bit_order msb_first;
codec crc16 { kernel = polynomial(width = 16, poly = 0x8005, reflect); }
impl crc16 derived;
struct S {
	authenticated body { u8 a; u8 b; }
	checksum u8 c[2] covers(body) is crc16;
}
"""),
)

#: Whose `_for_kernel` decides for each backend. C++ calls the C
#: implementation `gen-derived` emits, which is why it reads C's: the two
#: cannot disagree without C++ calling something no file defines.
WRITES_THE_KERNEL = {
	"c":      "situc.codegen.c.derived",
	"cpp":    "situc.codegen.c.derived",
	"python": "situc.codegen.python.derived",
	"rust":   "situc.codegen.rust.derived",
}


def test_a_backend_names_a_codec_only_if_it_writes_it() -> None:
	"""A checksum may only name a DERIVED codec -- wellformed refuses an
	`extern` one -- so a kernel a backend declines to write has no other
	provider, and naming it anyway is a call to nothing. Rust and Python
	crashed instead, on an `assert body is not None` whose message blamed
	the wrong thing; C and C++ declared the symbol and called it, which is a
	link error at the far end of somebody's build (26.368).

	Asked as a relationship rather than as two lists, because the backends
	genuinely differ: C writes every kernel family and Rust and Python write
	polynomial and ones-complement only. A fixture that is underivable
	everywhere does not exist, and one that is underivable *today* stops
	testing anything the day somebody implements it.
	"""
	from importlib import import_module

	for codec, source in CODEC_CASES:
		schema = parse(Source("<codec>", source))
		decl   = next(held for held in schema.codecs() if held.name == codec)

		for backend, text in four_ways(source).items():
			writes = import_module(WRITES_THE_KERNEL[backend])._for_kernel(
				decl, "situ") is not None
			named  = f"{codec}(" in text
			assert named == writes, (
				f"{backend} {'names' if named else 'does not name'} "
				f"`{codec}` and {'writes' if writes else 'does not write'} "
				f"it: a backend may name a codec only if it writes it")


def test_no_backend_calls_a_covered_accessor_it_did_not_emit() -> None:
	"""`compute` and `check` read the span through `_covered`, and all four
	emitted them whether or not that helper existed. The call is then to
	something nothing defines: C, C++ and Rust do not compile, and Python
	raises `AttributeError` the first time a caller asks.

	Both halves, because the first alone passes for a backend that emits
	nothing at all -- the contiguous schema is what says the four can write
	these helpers, and the gapped one that none of them writes half of them.
	"""
	for backend, text in four_ways(CONTIGUOUS).items():
		assert "c_covered" in text, (
			f"{backend} no longer emits a covered accessor, so the gapped "
			f"case below would pass for the wrong reason")

	for backend, text in four_ways(GAPPED).items():
		assert "c_covered" not in text, (
			f"{backend} names `c_covered` for a coverage with no single "
			f"range, and does not define it")


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


def test_no_backend_declines_a_member_in_silence() -> None:
	"""A member that simply vanishes is the shape a reader cannot ask about.

	Python and Rust ended `_arm_member` in a bare `return []`, so an arm
	whose type has no computable extent -- `packet.body.publish` and four
	more -- appeared in their output as neither an accessor nor a note,
	while C and C++ both wrote one. `project.md` names that as the worst
	case: "not an accessor, and not the note saying why".

	Checked directly rather than through the comparison above, because those
	five members are `EXEMPT` there and an exemption skips the member
	entirely -- so the comparison can no longer see whether they are
	mentioned at all.
	"""
	texts, paths = emitted(ROOT / "example/mqtt/mqtt.situ")

	for member in ("packet.body.publish", "packet.body.subscribe"):
		assert member in paths, member
		for backend, text in texts.items():
			flat = re.sub(r"\s*\n\s*[/*#]*\s*", " ", text)
			assert member in flat, (
				f"{backend} mentions `{member}` nowhere: not an accessor, and "
				f"not the note saying why")


def test_the_comparison_sees_refusals_at_all() -> None:
	"""The floor that stops this file passing over an empty list.

	Every set was empty for the members that diverged, so the comparison
	compared nothing and the assertion held -- a gate over an empty list
	reports success exactly as loudly as a real pass. This counts what the
	scoring actually finds across the corpus, so a phrase falling out of
	`REFUSALS`, or an emitter dropping its note, shows up here rather than as
	a quieter pass.
	"""
	seen = examined = 0
	for path in SCHEMAS:
		texts, paths = emitted(path)
		examined += len(paths)
		seen     += sum(len(refused(text, paths)) for text in texts.values())

	# The floor is on what the comparison *examines*, not on what it finds,
	# and the difference is the whole point.
	#
	# It used to be "at least ten refusals", and all ten were the mqtt and
	# netlink arms whose type has no computable extent. 26.209 gave those an
	# offset accessor in the three backends that had none, which is the right
	# answer and left the scoring finding *nothing* -- so the floor that was
	# guarding against a vacuous gate would itself have failed for the best
	# possible reason, and raising or lowering it would have been noise
	# either way.
	#
	# 1101 member paths is what four backends are compared over. That number
	# cannot fall without a schema leaving the corpus or `emitted` breaking,
	# and it is what stops this file passing because it read nothing.
	assert examined >= 1100, (
		f"the comparison examines {examined} member paths across the corpus, "
		f"down from 1101; a schema has left SCHEMAS or `emitted` is failing")

	# And the corpus currently holds no scored refusal at all, which is worth
	# asserting rather than leaving as an absence somebody rediscovers. It is
	# not "the backends refuse nothing": it is that what they refuse, they
	# refuse in words this scoring cannot attribute to a member -- the limit
	# 26.190 recorded and did not close. A refusal appearing here is a real
	# finding and should be looked at, not silently absorbed.
	assert seen == 0, (
		f"the scoring now finds {seen} refusals where it found none. That is "
		f"new information: either a backend has stopped emitting an accessor, "
		f"or a note has been worded into REFUSALS' reach.")


def test_every_backend_checks_a_bcd_field_nibble_by_nibble() -> None:
	"""A BCD field can hold a bit pattern that is not a number, and the getter
	cannot report it -- decoding returns a number either way. Parsing is where
	it has to be caught, and for a long time exactly one of the four caught it.

	Measured on `07 E1 09 1C`, 2017-09-28 written in binary rather than BCD --
	which is a defect openmlx4 shipped and a real ConnectX-3 corrected them
	about, `0x12` reading as 18 in binary and 12 in BCD. C refused it; C++,
	Python and Rust accepted it. `situc verify` runs the Python description,
	so it reported "2 vectors conform" over a vector written to prove that
	defect could not return.

	The runtime half was done four times -- `situ_bcd_valid`, `bcd_valid` in
	Python and in Rust all existed -- and the emitter half once. Python's
	generated module even imported `bcd_valid` and never called it.

	Asserted across the four rather than in each backend's own suite, because
	that is the shape of the fault: C's `test_a_bcd_field_is_validated_nibble
	_by_nibble` has passed throughout, and a per-backend test only catches
	this in a backend somebody remembered to write one for.
	"""
	source   = Source("unit.situ", "target buffer;\nendian big;\n"
	                  "struct s { bcd4 year; bcd2 month; }\n")
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))

	built = {
		"c":      generate_c(schema, resolved, "unit").source,
		"cpp":    generate_cpp(schema, resolved, "unit").header,
		"python": generate_py(schema, resolved, "unit").module,
		"rust":   generate_rs(schema, resolved, "unit").module,
	}
	# The paren matters, and the first draft of this test did without it and
	# was vacuous for the backend it was written for. Python's generated
	# module imports `bcd_valid` on a line that names it and does not call
	# it -- which was the whole defect -- so `"bcd_valid" in text` was
	# satisfied by the import while nothing checked a nibble. A test whose
	# passing condition is met by the bug it hunts.
	silent = [name for name, text in built.items()
	          if "bcd_valid(" not in text]
	assert not silent, f"backends not checking BCD nibbles: {silent}"


def test_no_backend_compares_a_bcd_bound_against_the_packed_nibbles() -> None:
	"""`[max = 12]` on a `bcd2 month` is a bound on the month, not on the byte.

	Rust read every non-text member through its raw load, so December -- `0x12`,
	which that backend's own getter reads as 12 -- was refused by that
	backend's own validator. Measured against C over `01 09 10 12 13 99`: the
	two agreed on four and disagreed on `0x10` and `0x12`, both valid months
	Rust alone refused.

	The identical fault in the other conversion was found and fixed earlier --
	`[min = 70701]` on cpio's magic compared `0x303730373031` against 70701 and
	refused GNU cpio's own header -- and was not carried across to BCD. The
	comment recording it sits four lines above the `else` that had the bug.

	Pinned as the decode reaching the comparison, since what makes this
	backend-specific is which expression `_attr_checks` is handed.
	"""
	source   = Source("unit.situ", "target buffer;\nendian big;\n"
	                  "struct s { bcd2 month [max = 12]; }\n")
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))

	module = generate_rs(schema, resolved, "unit").module
	bound  = [line for line in module.splitlines() if "> 12" in line]
	assert bound, module
	for line in bound:
		assert "bcd_decode" in line, line

#: What each backend's `validate` writes when the view is shorter than the
#: struct it claims to be. Four spellings of one refusal, kept here rather
#: than in four codegen files so that a backend dropping it is one failure
#: rather than a silent divergence.
#: The validate body in each backend, so the floor is asserted where it has
#: to be rather than anywhere in the file.
BODY = {
	"c":      r"situ_err_t situ_\w+_check\(situ_view_t view, uint32_t \*which\)"
	          r"\n\{(.*?)\n\}",
	# C++ follows C: where a struct has a member to name, the checks live in
	# `check` and `validate` is a one-line wrapper over it. Both spellings
	# are matched, and the wrapper is excluded by a lookahead -- counted, it
	# is a body with no floor and would report every such struct as
	# unguarded.
	"cpp":    r"err (?:check\(std::uint32_t \*which_\)|validate\(\))"
	          r" const noexcept\n\t\{(?!\n\t\treturn check\(nullptr\);)"
	          r"(.*?)\n\t\}",
	# Rust follows C and C++: where a struct has a member to name, the
	# checks live in `check_which` and `validate` is a wrapper over it. The
	# lookahead drops the wrapper, which is a body with no floor.
	"rust":   r"pub fn (?:check_which\(&self, which: &mut u32\)"
	          r"|validate\(&self\)) -> Result<\(\)> \{"
	          r"(?!\n\t\tlet mut sink)(.*?)\n\t\}",
	"python": r"def validate\(self\) -> None:(.*?)(?=\n\t*(?:def |@)|\Z)",
}

#: The floor CHECK, not the error it returns. Matching the error was vacuous
#: for C, whose other refusals return `SITU_ERR_BOUNDS` too: with the guard
#: disabled every schema still passed. The condition is what only this guard
#: writes.
FLOOR = {
	"c":      r"if \(view\.limit < SITU_\w+_SIZE_MIN\) \{",
	"cpp":    r"if \(raw_\.limit < \d+u\) \{",
	"python": r"if self\._len < \d+:",
	"rust":   r"if self\.bytes\.len\(\) < \d+ \{",
}


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_every_validate_refuses_a_view_below_the_struct(path: Path) -> None:
	"""`validate` is safe on arbitrary bytes, or it is not documented right.

	Every check it runs reaches a member at an offset the layout gives, and a
	view shorter than the struct cannot hold them. Acquisition refuses a
	short frame, so the paths that skip acquisition are the ones that bite: a
	variant arm takes its sub-view at the arm's own computed extent, and a
	five-byte modbus frame produced a view whose `validate` read a `u16` at
	offset 6. libFuzzer found it under ASan in a second (26.322).

	Asserted per backend and over the whole corpus, because the four are
	written separately and this is the kind of guard that gets added to one.
	"""
	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	name     = path.stem

	# C's `check` body is in the SOURCE, not the header -- the header
	# carries only its prototype -- so `emitted()`, which hands back
	# headers, cannot see this one. Ten schemas said so the first time this
	# ran, which is the test finding its own instrument rather than a bug.
	built = generate_c(schema, resolved, name)
	texts = {
		"c":      built.header + built.source,
		"cpp":    generate_cpp(schema, resolved, name).header,
		"python": generate_py(schema, resolved, name).module,
		"rust":   generate_rs(schema, resolved, name).module,
	}

	# A register is a bus transaction rather than bytes off a wire and gets
	# no size constants to compare against; a struct whose minimum is zero
	# has no floor to be below, and `limit < 0u` is a comparison
	# `-Wtype-limits` refuses under `-Werror`. Derived rather than exempted
	# by name, so a schema of only those two kinds skips honestly and one
	# that grows an ordinary struct starts being asserted.
	if not any(struct.layout.size_bytes and struct.layout.register is None
	           for struct in resolved.structs.values()):
		pytest.skip("every struct here is a register or has no floor")

	# Inside the validate bodies, not anywhere in the file. Asserted
	# file-wide first, and the sabotage showed that vacuous: `bounds` and
	# `BoundsError` are what acquisition raises too, so removing the guard
	# from C++ left 27 of 36 schemas still passing and from Python 32.
	expected = sum(1 for struct in resolved.structs.values()
	               if struct.layout.size_bytes
	               and struct.layout.register is None)

	# Counted the other way round, because these patterns do not match every
	# body -- a nested block closing at the same indent ends the match early,
	# and C++ loses two of json's ten that way. What each pattern DOES match
	# is a real validate body, and a body that qualifies and lacks the floor
	# is the finding; the allowance is exactly the structs that legitimately
	# have none.
	allowed = len(resolved.structs) - expected

	for backend, text in texts.items():
		bodies = re.findall(BODY[backend], text, re.S)
		if not bodies:
			continue		# nothing to guard
		missing = sum(1 for body in bodies
		              if not re.search(FLOOR[backend], body))
		assert missing <= allowed, (
			f"{path.name}: {backend} has {missing} validate bodies with no "
			f"floor and only {allowed} struct(s) entitled to none")

@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_c_and_rust_validate_the_same_arms(path: Path) -> None:
	"""A variant's arms are validated by their own type, in every backend.

	Rust nested that inside the branch for "the discriminant needs a check",
	which is a different question with a different answer:
	`classify_check` says NOTHING for `json`'s `value.body`, so Rust
	validated none of its four arms while the other three validated all of
	them. Nothing noticed, because until an arm actually refused the two
	answers were the same (26.322).

	C and Rust are compared because both name an `arm`, so the two counts
	mean the same thing. C++ and Python fold arm and nested validation into
	one spelling and are covered by the differential instead.
	"""
	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	name     = path.stem

	built = generate_c(schema, resolved, name)
	in_c  = (built.header + built.source).count("_validate(arm)")
	in_rs = generate_rs(schema, resolved, name).module.count("arm.validate()?")

	assert in_c == in_rs, (
		f"{path.name}: C validates {in_c} variant arms and Rust {in_rs}; "
		f"the schema means different things in the two languages")

SHORT_ARM = """target buffer;
endian big;

struct s {
	u8  kind;
	variant body switch (kind) {
		case 0:  u8   narrow;
		case 1:  u32  wide;
		default: error;
	}
}
"""

#: The frame CHECK an arm accessor makes, not the error it returns. Matching
#: the error is vacuous in C, whose other refusals return `SITU_ERR_BOUNDS`
#: too: with the guard deleted the assertion still passed.
ARM_BOUND = {
	"c":      "!situ_in_bounds(view, ",
	"cpp":    "!situ_in_bounds(raw_, ",
	"python": "if self._len < ",
	"rust":   "if self.bytes.len() < ",
}


def test_an_arm_accessor_checks_the_frame_in_every_backend() -> None:
	"""The arm question and the frame question are two.

	An arm accessor checked which arm the discriminant selected and then
	read at a fixed offset. A struct's minimum is its SHORTEST arm's, so a
	longer arm sits past what acquisition guarantees -- `s` above is two
	bytes at its smallest and `wide` needs five. C and C++ read past the
	view; Rust panicked and Python raised, which is safe and is still four
	answers to one question (26.325).

	Asserted per backend because the guard was added to four emitters, and
	a guard added to three is a disagreement rather than a fix.
	"""
	source   = Source("short_arm.situ", SHORT_ARM)
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))

	built = generate_c(schema, resolved, "unit")
	texts = {
		"c":      built.header + built.source,
		"cpp":    generate_cpp(schema, resolved, "unit").header,
		"python": generate_py(schema, resolved, "unit").module,
		"rust":   generate_rs(schema, resolved, "unit").module,
	}

	for backend, text in texts.items():
		at = text.find("wide")
		assert at != -1, f"{backend}: no accessor for the long arm at all"
		window = text[at:at + 700]
		assert ARM_BOUND[backend] in window, (
			f"{backend}: the `wide` arm accessor does not check that the "
			f"frame holds it")


# ---------------------------------------------------------------------------
# One message, one id, in four spellings (0051)
# ---------------------------------------------------------------------------

SAYS = """target buffer;
endian big;
bit_order msb_first;

struct S {
	u8  ver;
	u16 length;
}

when S.ver == 0
	refuse zero_version
	"version 0 was never shipped";

when S.length > 4096
	warn oversized
	"longer than any early reader was written to hold";

when S.ver == 1
	note legacy_framing
	"v1 counts the header in `length`";
"""


def test_the_four_backends_number_a_message_the_same_way() -> None:
	"""The id is the contract, so the four have to agree about which integer
	is which message (0051).

	The risk is real and is the one `traverse.messages` exists to remove:
	`traverse.obligations` carries the same rule for dirty bits, and was
	written because C and Python each numbered them from their own list and
	gave different answers for a struct carrying both a tag and an
	invariant. Four backends deriving the order from four walks would repeat
	that, and nothing else here would notice -- an id is a small integer and
	a wrong one reads exactly like a right one.

	Read off the emitted text rather than from a run, because what is being
	compared is what each backend PUBLISHED as its contract.
	"""
	schema   = parse_text(SAYS)
	resolved = resolve(schema, solve(schema))

	names = ("ZERO_VERSION", "OVERSIZED", "LEGACY_FRAMING")
	spelled = {
		"c":      (generate_c(schema, resolved, "unit").header,
		           "#define SITU_S_{}_MSG {}u"),
		"cpp":    (generate_cpp(schema, resolved, "unit").header,
		           "static constexpr std::uint32_t msg_{} = {}u;"),
		"rust":   (generate_rs(schema, resolved, "unit").module,
		           "pub const MSG_{}: u32 = {};"),
		"python": (generate_py(schema, resolved, "unit").module,
		           "\tMSG_{} = {}"),
	}

	for target, (text, shape) in spelled.items():
		for at, name in enumerate(names):
			spelling = name if target in ("c", "rust", "python") \
			           else name.lower()
			assert shape.format(spelling, at) in text, (
				f"{target} does not number {name} {at}")


# ---------------------------------------------------------------------------
# Which member refused, numbered the same way (26.231's half)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_the_backends_name_the_same_members_in_the_same_order(
		path: Path) -> None:
	"""The id is the contract, so the two that publish one must agree.

	Not only about the numbering: about the POPULATION. Which members
	`validate` can refuse over is a fact about what each backend emits, not
	about the schema, so there is no schema-level answer to share -- which
	is why the two derive it separately and this compares the results. A
	backend that stopped emitting one member's check would renumber every
	id after it, silently, and an id is a small integer that reads the same
	whatever it means.

	Python publishes no ids yet, so it is not here. When it does it joins
	this comparison rather than getting a test of its own: separate
	comparisons are chances for two backends to agree with each other and
	not with the rest.

	It has paid three times. C's pattern missed `return err;`, so a nested
	member had no id; C++ reproduced the same fault with `return e;`; and
	Rust missed `self.flags()?.validate()?;`, where the `?` IS the return
	and there is no `return` to match on. Each was a member `check` could
	refuse over while leaving `*which` at the sentinel its own doc comment
	calls "nothing refused" -- and each was invisible from inside its own
	backend, because what is missing is a constant nobody named.
	"""
	if "STATUS: needs phase" in path.read_text(encoding="ascii"):
		pytest.skip("declares itself unbuildable")

	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))

	# The struct's own name is stripped by NAMING it rather than by splitting
	# the macro on underscores. Both halves carry them -- `udp_header` has a
	# member `length`, and `SITU_UDP_HEADER_LENGTH_CHECK` splits four ways --
	# so a regex that guesses the boundary reports a disagreement that is its
	# own. It did, on 29 of 41 schemas, before the struct names were read.
	held = sorted((f"SITU_{c_name(name).upper()}_", c_name(name))
	              for name in resolved.structs)
	in_c: dict[str, list[tuple[str, str]]] = {}
	for spelled, at in re.findall(
			r"#define (SITU_\w+_CHECK) (\d+)u",
			generate_c(schema, resolved, path.stem).header):
		prefix, owner = max(
			((one, name) for one, name in held if spelled.startswith(one)),
			key=lambda pair: len(pair[0]), default=("", ""))
		assert prefix, f"{path.name}: {spelled} names no struct"
		in_c.setdefault(owner, []).append(
			(spelled[len(prefix):-len("_CHECK")].lower(), at))

	# Bucketed by class, because the two emit their structs in different
	# orders -- png puts `png_signature` first in C and last in C++ -- and a
	# flat comparison reports that as a disagreement about ids. It did, on
	# 14 schemas, which is this test's own instrument being the thing wrong
	# rather than either backend.
	in_cpp: dict[str, list[tuple[str, str]]] = {}
	renamed_back = {class_name(struct): c_name(name)
	                for name, struct in resolved.structs.items()}
	owner = ""
	for line in generate_cpp(schema, resolved, path.stem).header.splitlines():
		opened = re.match(r"class (\w+)", line)
		if opened:
			owner = opened.group(1)
			continue
		one = re.match(
			r"\tstatic constexpr std::uint32_t check_(\w+) = (\d+)u;", line)
		if one:
			in_cpp.setdefault(owner, []).append((one.group(1), one.group(2)))

	# A class C++ had to rename -- `framed` becomes `framed_` where a member
	# takes the class's own name -- is keyed back under the schema's name,
	# or the two dictionaries disagree about a struct they agree about.
	for name in list(in_cpp):
		schema_name = renamed_back.get(name, name)
		if schema_name != name:
			in_cpp[schema_name] = in_cpp.pop(name)

	# Rust's are associated constants, so they are scoped by the `impl` they
	# sit in rather than by a class body.
	in_rs: dict[str, list[tuple[str, str]]] = {}
	# Rust types are PascalCase, so the impl's name is mapped back through
	# the same function that produced it rather than lower-cased -- which
	# turns `UdpHeader` into `udpheader` and reports a struct both backends
	# agree about as one only Rust has.
	rust_back = {_pascal(name): c_name(name) for name in resolved.structs}
	owner = ""
	for line in generate_rs(schema, resolved, path.stem).module.splitlines():
		opened = re.match(r"impl<'a> (\w+?)(?:Mut)?<'a> \{", line)
		if opened:
			owner = rust_back.get(opened.group(1), opened.group(1))
			continue
		one = re.match(r"\tpub const CHECK_(\w+): u32 = (\d+);", line)
		if one:
			in_rs.setdefault(owner, []).append(
				(one.group(1).lower(), one.group(2)))

	# One divergence is held out, and the carve-out names the MECHANISM
	# rather than the symptoms -- a list of symptoms is how a gate acquires
	# an ignore list and stops being one.
	#
	# C groups over every entry it walks and C++ over the struct's own
	# members, so wherever those differ the two number different
	# populations: C names each arm of a variant that can refuse
	# (`body_read_coils`) where C++ names the variant, and C names an
	# `authenticated` region where C++ names what is inside it. Both are
	# defensible and they are different granularities, so agreeing is a
	# decision rather than a fix. 26.383 records it open.
	#
	# Everything else is compared: measured across this repository, 27
	# schemas whole and 174 of 200 structs. The two constructs are named
	# rather than the eight schemas that showed the symptom, because a list
	# of symptoms is how a gate acquires an ignore list and stops being one.
	apart = {c_name(name) for name, struct in resolved.structs.items()
	         if any(entry.placement.kind in ("variant", "authenticated")
	                for entry in struct.entries)}
	for name in apart:
		in_c.pop(name, None)
		in_cpp.pop(name, None)
		in_rs.pop(name, None)

	assert in_c == in_cpp, f"{path.name}: c and cpp"
	assert in_c == in_rs, f"{path.name}: c and rust"
