"""`remaining` is measured lazily, or a region's size recurses (26.585).

`situ-edit` and `report.listing` both died with a RecursionError on any
keystore whose `version` selects an arm. The cycle:

	chain_bits(sealed.plaintext)   sums the PARENT's members
	  -> size_bits(keystore.sealed)      reaches the region
	    -> 32 + SIZE(sealed.plaintext)   the region's size program
	      -> content_bits(plaintext)
	        -> offset_bits(plaintext)    for `remaining`, eagerly
	          -> chain_bits(plaintext)   and round again

`content_bits` computed the member's own offset before running its size
program, because `remaining` is measured from there -- and passed it as a
VALUE while every other input to the program was a thunk. For a member
interior to a region that offset is not answerable against the parent
view: the sum walks the parent's members, reaches the region, needs the
region's size, needs this member.

**And `plaintext`'s size program never uses `remaining` at all.** Its
program is a field load and an arithmetic op; the offset was computed for
nothing and the recursion was the whole of what it bought.

So `remaining` became a thunk like its neighbours, called by the one op
that wants it. The two cases the eager version existed for -- sqlite's 44
against C's 37 and ipv6ext's 38 against 46 -- are what
`test_the_walker_agrees_with_the_compiled_backends` holds, and they still
pass: this changes WHEN the offset is taken, not from where.

WHY NOTHING CAUGHT IT. The four-backend differential walks keystore on
every run and reaches this on none of them, because getting past `params`
needs `magic` to equal "KSTR" *and* `version` to be a declared revision.
Random bytes give the first about never, and 26.576's planted draw writes
every magic a schema states -- but a variant's discriminant is not a
magic, so the pair is unreachable. That gap is its own finding.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest					# noqa: E402
from every_schema import load_schema		# noqa: E402
from situc import pack as packer			# noqa: E402
from situc.layout import solve			# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker import report, vm			# noqa: E402
from walker.image import load			# noqa: E402
from walker.walk import acquire, offset_bits		# noqa: E402

#: A keystore whose version selects the pbkdf2 arm, which is what it takes
#: to walk past `params` -- `KSTR`, revision 1, a salt, a nonce, four bytes
#: of iterations, a zero length, thirty-two sealed bytes and a tag. 87
#: bytes, which the map gives as this struct's minimum.
KEYSTORE = (b"KSTR" + bytes([1]) + bytes(range(16)) + bytes(range(12))
            + (100000).to_bytes(4, "big") + (0).to_bytes(2, "big")
            + bytes([0xDE]) * 32 + bytes([0xAA]) * 16)

#: Where the tag lands: 4 + 1 + 16 + 12 + 4 + 2 of header, then the
#: thirty-two sealed bytes. Derived from the fields rather than pinned
#: blind, below.
TAG_AT = 71


def _image(name: str) -> bytes:
	parsed   = load_schema(ROOT / "example" / name / f"{name}.situ")
	resolved = resolve(parsed, solve(parsed))
	return packer.pack(parsed, resolved, metadata=True)[0]


def test_a_keystore_can_be_walked_at_all() -> None:
	"""It raised RecursionError, which is not a refusal anybody handles.

	`Refused` is the walker's way of saying a buffer does not support a
	question, and every caller is written against it. A RecursionError is a
	traceback out of the tool, so `situ-edit` did not report a bad keystore
	-- it died on a good one.
	"""
	listing = report.listing(load(_image("keystore")), KEYSTORE).splitlines()

	assert "-- keystore" in listing, listing[:8]
	block = listing[listing.index("-- keystore"):]
	block = block[:next((i for i, line in enumerate(block[1:], 1)
	                     if line.startswith("-- ")), len(block))]

	# What the differ probes, which is the walk having got all the way
	# through: the members it reads, the sealed region refused unopened,
	# and a verdict.
	assert any(line.startswith("validate ") for line in block), block
	assert any(line.startswith("sealed refused=") for line in block), block
	assert "version 1" in block, block


def test_the_tag_after_a_sealed_region_is_placed_where_c_puts_it() -> None:
	"""And placed correctly, not merely without crashing.

	A fix that made the question refusable rather than answerable would
	leave the walker refusing where the four backends answer, which is the
	direction the differential exists to catch.
	"""
	image = load(_image("keystore"))
	which = next(i for i in range(len(image.structs))
	             if image.struct_name(i) == "keystore")
	view  = acquire(image, KEYSTORE, which)

	placed = {image.name_of(i): offset_bits(view, i) // 8
	          for i in image.members(image.structs[which])}

	assert placed["keystore.tag"] == TAG_AT, placed
	assert placed["keystore.tag"] + 16 == len(KEYSTORE), (
		"the tag does not end where the message does")


def test_a_program_without_remaining_never_asks_for_an_offset() -> None:
	"""The property the fix rests on, asserted rather than inferred.

	The recursion was not in the arithmetic: it was in computing an input
	the program does not read. So a program with no `remaining` op must not
	call the thunk at all -- which is what makes an interior member's size
	answerable against a view its offset is not.
	"""
	asked = 0

	def remaining() -> int:
		nonlocal asked
		asked += 1
		return 99

	# PUSH 7, END: an arithmetic program that reads nothing.
	code = bytes([vm.PUSH]) + (7).to_bytes(8, "little", signed=True) \
		+ bytes([vm.END])
	held = vm.run(code, 0,
	              load_field = lambda i: 0, size_of = lambda i: 0,
	              offset_of  = lambda i: 0, count_of = lambda i: 0,
	              remaining  = remaining)

	assert held == 7
	assert asked == 0, "the thunk was called by a program that never asks"


def test_a_program_with_remaining_still_gets_it() -> None:
	"""The other half, so the thunk is not merely never called.

	A control: if `remaining` stopped being read at all, every
	`[remaining]` run in the corpus would measure zero and the test above
	would still pass.
	"""
	calls = 0

	def remaining() -> int:
		nonlocal calls
		calls += 1
		return 37

	code = bytes([vm.REMAINING, vm.END])
	held = vm.run(code, 0,
	              load_field = lambda i: 0, size_of = lambda i: 0,
	              offset_of  = lambda i: 0, count_of = lambda i: 0,
	              remaining  = remaining)

	assert held == 37
	assert calls == 1, f"the thunk ran {calls} times for one op"


def test_a_plain_value_still_works() -> None:
	"""`remaining` may be an int, which is what every other caller passes."""
	code = bytes([vm.REMAINING, vm.END])
	assert vm.run(code, 0,
	              load_field = lambda i: 0, size_of = lambda i: 0,
	              offset_of  = lambda i: 0, count_of = lambda i: 0,
	              remaining  = 12) == 12
