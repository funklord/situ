"""A nested struct is validated at its own extent (26.577).

Every backend acquires a nested member's view at the extent the member
measures and propagates what that refuses:

	e = situ_view_sub(view, base, situ_remaining_u32(view.limit, base),
	                  &whole);
	return situ_view_sub(view, base, situ_name_extent(whole), out);

The walker handed the inner walk the rest of the PARENT's frame instead,
so a nested struct measuring shorter than its own minimum was never
caught. `dnsname.question` is the case and it is the dangerous direction:

	2b 34 0d 39 0a 20 2b 0a 45 20 34 32 45 20 31 65 36 3a 2d

`0x2b` is form 0, rest 43 -- a length-43 label in a 19-byte message. The
run holds no element, `name` measures 0, and `name`'s minimum is 1. C
answers BOUNDS. The walker answered OK: **a validator accepting a
truncated DNS name.**

The check needed a number the image did not carry. `Struct.size_bits` is
`NONE` for a variable struct -- which is why neither reader tested it --
while every generated header has `SIZE_MIN` for one. The packer computed
it and discarded it; it is appended to the struct row now, after the
fields a reader already takes, so an older reader stepping by the
section's own stride is unaffected.

Found by a magic-planting draw written for 26.576, which reached this
because `0x2b` is an ordinary byte and the shape needs no magic at all --
what it needed was a buffer short enough to truncate the first label,
which the planted draws happened to produce.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

from every_schema import SCHEMAS, ids, load_schema	# noqa: E402
import pytest						# noqa: E402
from situc import pack as packer			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker.image import NONE, load			# noqa: E402
from walker import report				# noqa: E402
from walker.walk import View				# noqa: E402

#: The buffer a planted draw produced, kept rather than regenerated: the
#: seed that found it is one of twelve and the shape is the subject.
TRUNCATED = bytes.fromhex("2b340d390a202b0a4520343245203165363a2d")

ERR_BOUNDS = 1


def _image(path: Path):  # type: ignore[no-untyped-def]
	parsed   = load_schema(path)
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	return load(blob)


def test_a_truncated_nested_name_is_refused() -> None:
	"""BOUNDS, and the member it names is the nested one."""
	image = _image(ROOT / "example" / "dnsname" / "dnsname.situ")
	index = next(i for i, _ in enumerate(image.structs)
	             if image.struct_names[i] == "question")

	found: list[tuple[int, int]] = []
	view = View(image, bytearray(TRUNCATED), index, 0, len(TRUNCATED))
	verdict = report._validate(image, view, index, found)

	assert verdict == ERR_BOUNDS, (
		f"a 19-byte message whose first label claims 43 bytes read "
		f"{verdict}, where every backend answers {ERR_BOUNDS}")
	assert found, "the refusal names no member"
	assert image.placement_names[found[0][0]] == "question.qname", found


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_every_struct_carries_a_minimum(path: Path) -> None:
	"""The number the check needs, for every struct in the corpus.

	A floor rather than a value per struct: what matters is that the row
	carries one at all, since `NONE` is what the check declines on and a
	silent decline is how this went unnoticed. A fixed struct's minimum is
	its size, so the two agree where both are known -- asserted, because
	appending a field is exactly where an off-by-one in the row layout
	would put a neighbouring field's bytes here.
	"""
	image = _image(path)
	for struct in image.structs:
		assert struct.size_min_bits != NONE, "a struct with no minimum"
		if struct.size_bits != NONE:
			assert struct.size_min_bits == struct.size_bits, (
				"a fixed struct's minimum is its size")
