"""The walker validates a variant's selected arm, as the backends do.

`report._validate` had no arm handling at all, so a variant whose arm is a
nested struct went unchecked: `sexpr.sexpr` is `peek u8 kind` and a switch
over `list`, `text` and `symbol`, and the walker read a 61-byte buffer C
refuses (26.578).

**Two things this got wrong first, both from reasoning by analogy with the
plain nested-field case rather than reading what C emits.**

1. It asked `offset_bits` for the ARM, which answered `Refused: placement
   52 is not a member of this struct` -- a bookkeeping answer, not a
   bounds one -- and turning that into BOUNDS refused three schemas C
   accepts. `_arm_bits` says it: "the arm starts where the variant does
   ... asking `offset_bits` for the arm would walk the variant whose
   extent is this". A variant's extent IS its selected arm's.
2. It then validated the selected arm unconditionally. C emits **no**
   `_view` accessor for an arm whose struct it cannot measure -- there is
   no `situ_packet_body_publish_view` in mqtt's header at all -- so
   `packet_check` has no block for one and says nothing about it. The
   condition is `measurable`, which `_arm_bits` already used.

Both were caught by the planted draw, and the second only by compiling a
probe against mqtt's header and reading which accessors exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

from every_schema import load_schema			# noqa: E402
from situc import pack as packer			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker.image import NONE, load			# noqa: E402
from walker import report				# noqa: E402
from walker.walk import View, chosen_arm		# noqa: E402

ERR_BOUNDS = 1

#: The buffer a planted draw produced for `sexpr`. It opens with `)`, so
#: the discriminant selects the `symbol` arm.
SEXPR = bytes.fromhex(
	"293e2b47512e416a762b5b27224e2524794c426d4e7d0d785d2d743d49647a38"
	"4a09392552733d5578246e312e420d4f52740956275e5b793c26336d2b")

#: And one for `mqtt`, whose selected arm is `publish` -- a struct no
#: backend can measure, so no backend validates it and neither may this.
MQTT = bytes.fromhex(
	"3132203265302d652d352b34342e0a39372b36302e360d2032363a350d323535"
	"3a30453634453832392e0a312e45320a200d0d3939203220343938453965")


def _image(name: str):  # type: ignore[no-untyped-def]
	path     = ROOT / "example" / name / f"{name}.situ"
	parsed   = load_schema(path)
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	return load(blob)


def _verdict(image, struct: str, buffer: bytes):  # type: ignore[no-untyped-def]
	index = next(i for i, _ in enumerate(image.structs)
	             if image.struct_names[i] == struct)
	found: list[tuple[int, int]] = []
	view = View(image, bytearray(buffer), index, 0, len(buffer))
	return report._validate(image, view, index, found), found


def test_a_recursive_arm_is_validated() -> None:
	"""`sexpr` over a buffer C refuses, which the walker read as OK."""
	verdict, _ = _verdict(_image("sexpr"), "sexpr", SEXPR)
	assert verdict == ERR_BOUNDS, (
		f"a buffer every backend refuses read {verdict}")


def test_an_arm_no_backend_can_measure_is_left_alone() -> None:
	"""mqtt's `publish`, and this is the over-refusal half.

	The discriminant selects it, so a check that validated the selected arm
	unconditionally refuses here -- and C has no accessor for it, so C says
	nothing. Accepting is agreement, not laxity.
	"""
	image = _image("mqtt")
	verdict, _ = _verdict(image, "packet", MQTT)
	assert verdict == 0, (
		f"`packet` read {verdict} where every backend answers 0: the arm "
		f"its discriminant selects is one no backend can acquire")


def test_the_unmeasurable_arm_is_still_the_one_selected() -> None:
	"""The control for the test above, and it is load-bearing.

	Without it that assertion passes if the discriminant stops selecting
	`publish` at all -- a green test over a case that no longer exists.
	This pins that the arm IS selected and IS the one no backend measures,
	so the acceptance above is about the `measurable` condition rather than
	about the arm having gone away.
	"""
	image = _image("mqtt")
	index = next(i for i, _ in enumerate(image.structs)
	             if image.struct_names[i] == "packet")
	variant = next(m for m in image.members(image.structs[index])
	               if m in image.arms)

	view = View(image, bytearray(MQTT), index, 0, len(MQTT))
	arm = chosen_arm(view, variant)
	assert arm is not None and arm != NONE, "no arm is selected"
	assert image.placement_names[arm] == "packet.body.publish", \
		image.placement_names[arm]

	held = image.placements[arm]
	assert held.type_struct != NONE
	assert not image.structs[held.type_struct].measurable, (
		"`publish_body` is measurable now, so this case no longer tests "
		"what it was written for")
