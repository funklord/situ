"""Which region owns a member, and which placements ARE a region (26.523).

Both questions were answered from the way a path is SPELLED. The owner was
the first row anywhere in the image whose path ended with the region's bare
name, so two structs each declaring `sealed body` collapsed into one gate
and `one`'s gate was reported as holding `two`'s secret bytes -- the worst
direction this table can be wrong in, since 14.3 hands a region's interior
out on its tag. And a placement counted as a region if its path merely
ENDED with the region's name, so a `u8 somebody` inside `body` was written
as a region and disappeared from the gate.

The relation is containment over paths, and the kind is the kind. Neither
is a question about spelling, which is why every fixture here differs from
its control only in a name.

The nesting fixture is the one that separates `longest` from `first`: an
inner region's members are contained by the outer region too, and the gate
that holds them is the inner one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from situc import pack as packer
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

from every_schema import SCHEMAS, ids, load_schema

from walker import image as image_reader
from walker.report import _gated

PREAMBLE = (
	"target buffer;\n"
	"endian big;\n\n"
	"codec aead { length_preserving; seekable = linear;\n"
	"             granularity = byte; authenticated; invertible;\n"
	"             deterministic; }\n"
	"impl aead extern \"my_aead\";\n"
	"codec scramble { length_preserving; seekable = linear;\n"
	"                 granularity = byte; invertible; deterministic; }\n"
	"impl scramble extern \"my_scramble\";\n\n"
)

SEALED = (
	"struct {name} {{\n"
	"\tsealed body(aead) {{ u8 {field}; }}\n"
	"\ttag u8 sig[16] covers(body);\n"
	"}}\n"
)


def _image(text: str) -> image_reader.Image:
	"""The packed image, with the names the assertions read it by.

	`metadata=True` because every question here is about which placement
	owns which, and an image without the name table answers in indices --
	which would make a wrong owner and a right one equally readable.
	"""
	schema = parse_text(PREAMBLE + text)
	blob, _ = packer.pack(schema, resolve(schema, solve(schema)),
	                      metadata=True)
	return image_reader.load(blob)


def _owners(image: image_reader.Image) -> dict[str, str]:
	"""Every placement's region owner, both sides named by path."""
	return {image.name_of(i): image.name_of(owner)
	        for i, owner in sorted(image.region_owner.items())}


def _interior(image: image_reader.Image, gate: str) -> list[str]:
	"""What the walker renders behind one gate, named by path."""
	index = next(i for i in range(len(image.placements))
	             if image.name_of(i) == gate)
	return [image.name_of(i) for i in _gated(image, index)]


def test_two_structs_named_body_keep_their_gates_apart() -> None:
	"""The reported case, and the whole of it: four regions, four gates."""
	image = _image(
		SEALED.format(name="one", field="x")
		+ SEALED.format(name="two", field="y")
		+ "struct outer { u8 plain; one first; two second; }\n")

	owners = _owners(image)
	assert owners["two.body"] == "two.body", \
		"`two`'s region was recorded as sitting inside `one`'s"
	assert owners["two.body.y"] == "two.body"
	assert owners["outer.second.body.y"] == "outer.second.body"

	# The consequence, which is the reason this matters: a gate names the
	# bytes it protects, so the wrong owner is a wrong claim about secrecy.
	assert _interior(image, "one.body") == ["one.body.x"]
	assert _interior(image, "two.body") == ["two.body.y"]
	assert _interior(image, "outer.second.body") == ["outer.second.body.y"]


def test_a_member_whose_name_ends_with_the_regions_is_not_a_region() -> None:
	"""`somebody` inside `body`. The two members differ only in spelling."""
	image = _image(
		"struct s {\n"
		"\tsealed body(aead) { u8 somebody; u8 other; }\n"
		"\ttag u8 sig[16] covers(body);\n"
		"}\n")

	assert _interior(image, "s.body") == ["s.body.somebody", "s.body.other"], \
		"a member behind the gate vanished because of how it is spelled"


def test_an_inner_region_gates_its_own_members() -> None:
	"""Nesting, which is what separates the longest prefix from the first.

	`nest.body.inner.deep` is contained by `nest.body` as well, and that
	is the answer a first-match search gives.
	"""
	image = _image(
		"struct nest {\n"
		"\tsealed body(aead) {\n"
		"\t\tu8 outer_field;\n"
		"\t\tcoded inner(scramble) { u8 deep; }\n"
		"\t}\n"
		"\ttag u8 sig[16] covers(body);\n"
		"}\n")

	owners = _owners(image)
	assert owners["nest.body.inner"] == "nest.body.inner", \
		"the inner region was not recorded as a region at all"
	assert owners["nest.body.inner.deep"] == "nest.body.inner", \
		"an inner region's member was attributed to the outer gate"
	assert owners["nest.body.outer_field"] == "nest.body"


def test_a_top_level_coded_region_is_written_as_a_region() -> None:
	"""A region that is not nested inside another carries no `regions`, so
	the old guard never ran and `KIND.get("coded")` fell through to FIELD.

	Seven placements in the committed corpus were fields for this reason,
	SLIP's datagram and SMTP's dot-stuffed body among them.
	"""
	image = _image(
		"struct plain_coded {\n"
		"\tcoded body(scramble) { u8 payload[4]; }\n"
		"}\n")

	index = next(i for i in range(len(image.placements))
	             if image.name_of(i) == "plain_coded.body")
	assert image.placements[index].kind == packer.KIND["region"]


def test_every_recorded_owner_contains_the_member_it_owns() -> None:
	"""The invariant, over every fixture here at once.

	An owner that does not contain its member is the fault this file is
	about, whatever produced it -- so assert the relation rather than the
	four answers, which is what would have caught a repair that got the
	direction right and the scope wrong.
	"""
	image = _image(
		SEALED.format(name="one", field="x")
		+ SEALED.format(name="two", field="y")
		+ "struct outer { u8 plain; one first; two second; }\n"
		+ "struct nest {\n"
		"\tsealed body(aead) {\n"
		"\t\tu8 outer_field;\n"
		"\t\tcoded inner(scramble) { u8 deep; }\n"
		"\t}\n"
		"\ttag u8 sig[16] covers(body);\n"
		"}\n")

	for member, owner in _owners(image).items():
		assert member == owner or member.startswith(owner + "."), \
			f"{owner} does not contain {member}"
		assert image.placements[
			next(i for i in range(len(image.placements))
			     if image.name_of(i) == owner)].kind \
			== packer.KIND["region"], f"{owner} is not a region"


AUTHENTICATED = (
	"struct {name} {{\n"
	"\tauthenticated body {{ u8 {field}; }}\n"
	"\tchecksum u8 sig[4] covers(body);\n"
	"}}\n"
)


def test_an_authenticated_regions_members_name_their_own_region() -> None:
	"""The sibling case again, for the region that opens no namespace.

	`a.body` holds `a.x`, not `a.body.x`, so nothing about the paths says
	they belong together -- which is why containment alone could not reach
	this and the whole corpus had no correct owner for one.
	"""
	image = _image(
		AUTHENTICATED.format(name="a", field="x")
		+ AUTHENTICATED.format(name="b", field="y")
		+ "struct outer { u8 plain; a first; b second; }\n")

	owners = _owners(image)
	assert owners["a.x"] == "a.body"
	assert owners["b.y"] == "b.body"
	assert owners["a.body"] == "a.body", "a region names itself"


def test_the_innermost_of_two_authenticated_regions_wins() -> None:
	"""Both share one scope, so the longest-scope key cannot separate them
	and the position in `regions` -- stamped outermost-first -- must."""
	image = _image(
		"struct nested {\n"
		"\tauthenticated body {\n"
		"\t\tu8 top;\n"
		"\t\tauthenticated inner { u8 deep; }\n"
		"\t}\n"
		"\tchecksum u8 sig[4] covers(body);\n"
		"}\n")

	owners = _owners(image)
	assert owners["nested.top"] == "nested.body"
	assert owners["nested.deep"] == "nested.inner"


def test_an_authenticated_region_inside_a_sealed_one() -> None:
	"""The case where the two keys must be applied in order.

	`mixed.body` is sealed and scopes `mixed.body`; `mixed.body.inner` is
	authenticated and scopes its OWNER, which is also `mixed.body`. The
	scopes tie, and only the `regions` position says which is inner.

	**This one passed before the fix as well, and saying so is the
	point.** An authenticated region nested inside a sealed one inherits
	`sealed_by` from `stamp_region`, so it was already a row for that
	reason -- which is exactly why the corpus's TOP-LEVEL authenticated
	regions had none and this one did. So this is a regression guard on
	the key order and not a control for the change, and the two are not
	interchangeable: every other fixture here fails against the previous
	commit and this one cannot.
	"""
	image = _image(
		"struct mixed {\n"
		"\tsealed body(aead) {\n"
		"\t\tu8 plain_in_seal;\n"
		"\t\tauthenticated inner { u8 z; }\n"
		"\t}\n"
		"\ttag u8 sig[16] covers(body);\n"
		"}\n")

	owners = _owners(image)
	assert owners["mixed.body.plain_in_seal"] == "mixed.body"
	assert owners["mixed.body.z"] == "mixed.body.inner", \
		"the inner authenticated region lost to the sealed one it is in"


def test_an_authenticated_region_is_still_not_a_member() -> None:
	"""The row it gains must not join any struct's member walk.

	`traverse.NOT_A_MEMBER` holds `authenticated` because such a region
	names bytes its members already own and consumes none itself, so a
	walk that counted it would place everything after it one region too
	far along. The row goes in after every span for exactly that reason,
	and this is the assertion that keeps it there.
	"""
	image = _image(AUTHENTICATED.format(name="a", field="x"))

	struct = image.structs[0]
	walked = [image.name_of(i) for i in image.members(struct)]
	assert "a.body" not in walked, \
		"the region joined the member walk; every offset after it moves"
	assert walked == ["a.x", "a.sig"]
	# ...and it is still in the table, which is the point of adding it.
	assert "a.body" in {image.name_of(i) for i in range(len(image.placements))}


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_every_member_inside_a_region_names_the_region(path: Path) -> None:
	"""The invariant, over every schema this repository builds.

	A placement carrying a region record and no owner is a member that
	knows it is inside something and cannot say what -- which is what
	every `authenticated` region in the corpus was, and what four members
	of `edges.situ` were instead pointing at another struct's region.

	Asserted over the corpus rather than over fixtures because the fault
	was invisible in both directions from inside one schema: a wrong
	owner is a real index, and a missing one was the sentinel.
	"""
	schema = load_schema(path)
	blob, _ = packer.pack(schema, resolve(schema, solve(schema)),
	                      metadata=True)
	image = image_reader.load(blob)

	for index in sorted(image.regions):
		assert index in image.region_owner, \
			f"{image.name_of(index)} is inside a region it cannot name"
		owner = image.region_owner[index]
		# Range-checked before it is used, so that a reintroduced
		# sentinel reports itself instead of raising IndexError two lines
		# down. Against the previous commit this is what that fault
		# looked like -- a crash where the message should be.
		assert owner < len(image.placements), \
			f"{image.name_of(index)} names placement {owner}, which is " \
			f"outside the table of {len(image.placements)}"
		assert image.placements[owner].kind == packer.KIND["region"], \
			f"{image.name_of(index)} names {image.name_of(owner)}, not a region"
