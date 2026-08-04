"""Schemas nobody wrote, composed from the constructs that compose badly.

Every defect in 26.47 through 26.49 was found the same way: take two
constructs this repository covers several times over, put them next to each
other, and see what four backends do with the pair. Twenty of them were found
by hand, one schema at a time, which is a person doing a machine's job.

This is the machine. Four axes, each a question a backend's emitters branch
on:

  * **the driver** -- what says how many. A plain field, a nibble, a varint, a
    number written as digits, a field of a nested struct. Six emitters read
    these and each has read one of them wrongly.
  * **the form** -- how the count is written: a bare reference, arithmetic over
    it, or `remaining`. `traverse.data_sized` exists because three places
    answered "is this sized by the data" differently for the second one.
  * **the element** -- what is counted: bytes, values wider than a byte, a
    fixed record, a variable one. The width decides whether the member is a
    span or an index, which 26.47 found four answers to.
  * **what precedes it** -- nothing, a variable-length run, or a delimiter.
    This is the axis that decides whether the member's offset is a constant,
    and 26.49 is eleven places that assumed it was.
  * **where it sits** -- the frame itself, a nested struct, an `authenticated`
    region, a `sealed` one, a variant arm. Each is a different accessor
    family, and a gate is a parameter three of them forgot.

The cross product is larger than anything that can run in CI, and most of it
is not interesting: the point is not to run every cell but to have the cells
*enumerated*, so a sample is a sample of something known rather than of
whatever somebody thought of. `tools/sweep.py` runs a slice as long as you
like; `test_composed_schemas` runs a fixed sample of it on every commit.

**A schema the compiler refuses is a pass.** Most of this space is illegal --
a bit-packed field at a dynamic offset, `[remaining]` with something after it,
a varint inside a run -- and a diagnostic is the right answer to all of those.
What is not a pass is a traceback, generated code that does not compile, or
four backends that disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

#: What says how many. The name is the axis value; the text is what it takes
#: to declare one, and `read` is how a member refers to it.
DRIVERS: dict[str, tuple[str, str]] = {
	# A plain field, which is every length field in `examples/`.
	"u8":     ("\tu8   n [max = 4];\n", "n"),
	"u16":    ("\tu16  n [max = 4];\n", "n"),
	# A nibble, which contributes half a byte to every offset after it: the
	# arithmetic 26.46 found seven copies of, each dividing by eight too soon.
	"packed": ("\tu4   hi;\n\tu4   n;\n", "n"),
	# A varint, whose width is in its own bytes. `readable_names` asked for a
	# scalar, which a varint has not.
	"varint": ("\tedge_varint  n;\n", "n"),
	# Digits. The read that cannot fail is `<field>_value`, and only two of
	# the three forms of a text number emitted one.
	"text":   ("\tdecimal u32  n[4] [max = 4];\n", "n"),
	# A field of a nested struct, reached through the dot that four backends'
	# name lists stopped at.
	"nested": ("\thdr  head;\n", "head.n"),
}

#: How the count is written. `sized_by` holds a path and holds nothing for
#: arithmetic, which is the second spelling of one question.
FORMS: dict[str, str] = {
	"count":     "[{read}]",
	"arith":     "[{read} + 1]",
	"remaining": "[remaining]",
}

#: What is counted. A byte run is a pointer and a length; anything wider is
#: reached by index, because the bytes are not the values.
ELEMENTS: dict[str, tuple[str, str]] = {
	"u8":   ("u8",   ""),
	"u16":  ("u16",  ""),
	"i32":  ("i32",  ""),
	"rec":  ("rec",  "struct rec { u16 a; u8 b; }\n"),
	"vrec": ("vrec", "struct vrec { u8 len; u8 body[len]; }\n"),
}

#: What comes between the driver and the member under test. This is the axis
#: that decides whether the member has a constant offset at all.
BEFORE: dict[str, str] = {
	"nothing": "",
	"bytes":   "\tu8   gap[{read}];\n",
	"delim":   "\tu8   label[] until \"\\0\" max 8;\n",
	# A varint: the offsets after it are dynamic for a reason nothing else
	# here has -- its width is in its own bytes rather than in a field.
	"varint":  "\tedge_varint  skip;\n",
	# A run of records ending at a terminator. Everything after it is placed
	# by a *walk*, which is a third way for an offset to stop being a
	# constant: not a sum, not a scan, a loop.
	"records": "\tmark  seen[] until \"\\xff\";\n",
}

#: Where the member under test sits. Each is its own accessor family: a gate
#: takes a parameter, an arm takes a discriminant test, a nested struct is
#: addressed from its own frame.
PLACES = ("frame", "nested", "authenticated", "sealed", "coded", "arm")

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"

VARINT_DECL = (
	"varint_type edge_varint {\n"
	"\tencoding  = be128;\n"
	"\tmax_bits  = 64;\n"
	"\tmax_bytes = 9;\n"
	"}\n"
)

SEAL_DECL = (
	"codec seal {\n"
	"\tlength_preserving;\n"
	"\tseekable;\n"
	"\tauthenticated;\n"
	"\tinvertible;\n"
	"\tdeterministic;\n"
	"}\n"
	"\nimpl seal extern \"my_seal\";\n"
)

ARM_DECL = (
	"enum arm_kind : u8 {\n"
	"\tfirst   = 0x11,\n"
	"\tsecond  = 0x22,\n"
	"\tdefault = error,\n"
	"}\n"
)

HDR_DECL = "struct hdr { u16 lead; u8 n [max = 4]; }\n"

#: A record for a terminated run to walk. Fixed size, so what the run costs
#: an offset is the walk itself rather than the element's own measurement.
MARK_DECL = "struct mark { u8 kind; u8 weight; }\n"

#: A codec that doubles, so the arithmetic between a region's interior and its
#: bytes on the wire is visible: a two-byte interior is four bytes of region,
#: and a length that forgot the ratio would still look plausible.
CODED_DECL = (
	"codec doubling {\n"
	"\texpansion = ratio_exact(2, 1);\n"
	"\tgranularity = byte;\n"
	"\tseekable = linear;\n"
	"\tinvertible;\n"
	"\tdeterministic;\n"
	"}\n"
	"\nimpl doubling extern \"my_doubling\";\n"
)


@dataclass(frozen=True)
class Case:
	"""One composition, and the schema it stands for."""

	driver:  str
	form:    str
	element: str
	before:  str
	place:   str

	@property
	def name(self) -> str:
		return (f"{self.driver}-{self.form}-{self.element}"
		        f"-after-{self.before}-in-{self.place}")

	def schema(self) -> str:
		"""The whole schema, ready to hand to `situc`."""
		declared, read = DRIVERS[self.driver]
		element, decl  = ELEMENTS[self.element]

		head: list[str] = [PREAMBLE, ""]
		if self.driver == "varint":
			head.append(VARINT_DECL)
		if self.driver == "nested":
			head.append(HDR_DECL)
		if decl:
			head.append(decl)
		if self.place == "sealed":
			head.append(SEAL_DECL)
		if self.place == "coded":
			head.append(CODED_DECL)
		if self.place == "arm":
			head.append(ARM_DECL)
		if self.before == "varint" and self.driver != "varint":
			head.append(VARINT_DECL)
		if self.before == "records":
			head.append(MARK_DECL)

		# The member under test, and a scalar after it: what follows a member
		# the data sizes is where a wrong extent shows up, and half of what
		# this sweep has found was found in the member *after* the one being
		# composed.
		member = (f"\t{element}  run{FORMS[self.form].format(read=read)};\n")
		tail   = "\tu16  tail;\n"

		body = declared + BEFORE[self.before].format(read=read)
		if self.place == "frame":
			inner = ""
			body += member + tail
		elif self.place == "nested":
			# The driver goes *inside* the nested struct: a member cannot
			# name a field of the struct that contains its own. What the
			# outer struct contributes is the thing that matters here, which
			# is that `part` sits at an offset the message decides.
			inner = f"struct held {{\n{body}{member}\tu16  seq;\n}}\n"
			body  = ("\tu8   lead [max = 4];\n"
			         "\tu8   ahead[lead];\n"
			         "\theld  part;\n") + tail
		elif self.place == "authenticated":
			inner = ""
			body += (f"\tauthenticated body {{\n\t{member}\t}}\n"
			         "\ttag u8 mac[4] covers(body);\n")
		elif self.place == "sealed":
			inner = ""
			body += (f"\tsealed body(seal) {{\n\t{member}\t}}\n"
			         "\ttag u8 mac[16] covers(body);\n")
		elif self.place == "coded":
			# The fourth container, and the one whose bytes on the wire are
			# not its interior: what the region occupies is its members put
			# through the codec's expansion.
			inner = ""
			body += (f"\tcoded body(doubling) {{\n\t{member}\t}}\n"
			         + tail)
		else:
			inner = ""
			body += ("\tarm_kind  which;\n"
			         "\tvariant body switch (which) {\n"
			         f"\t\tcase arm_kind.first:  {element}"
			         f"  run{FORMS[self.form].format(read=read)};\n"
			         "\t\tcase arm_kind.second: u8  other[2];\n"
			         "\t}\n")

		return "".join(head) + inner + f"\nstruct s {{\n{body}}}\n"


def cases() -> list[Case]:
	"""Every cell of the space, in a fixed order.

	Fixed because a sample of it has to be reproducible from a seed, and
	because a case that fails should have the same name tomorrow.
	"""
	return [Case(driver, form, element, before, place)
	        for driver, form, element, before, place
	        in product(sorted(DRIVERS), sorted(FORMS), sorted(ELEMENTS),
	                   sorted(BEFORE), PLACES)]
