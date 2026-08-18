"""`covers(...)` on a `coded` region: section 14.1a.

The clause widens what a transform runs over, beyond the region's own bytes.
Header protection is what it is for -- QUIC masks the first byte and the
packet number under one operation -- and the reason it needs its own tests is
that the codec ABI of 13.2a takes one pointer and one length. A coverage that
is not contiguous has no single range to be handed, and the interesting half
of this feature is what the backends do about that.

Every test here is written against a *difference*: a clause that parses and is
then ignored produces output identical to one without it, and that is exactly
what the first version of this did.
"""

from __future__ import annotations

import pytest

from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from situc.unparse import unparse
from situc.codegen.c.emit import generate as generate_c

PREAMBLE = """endian big;
bit_order msb_first;

codec hp {
	granularity = byte;
	length_preserving;
	seekable;
	invertible;
	deterministic;
}

impl hp extern "app_hp_mask";
"""

ADJACENT = """struct adj {
	u8 flags;
	coded pn(hp) covers(flags) { u8 number; }
	u32 rest;
}
"""

SPLIT = """struct split {
	u8 flags;
	u32 gap;
	coded pn(hp) covers(flags) { u8 number; }
}
"""


def emit_c(body: str) -> str:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	files    = generate_c(schema, resolved, "t")
	return "".join(files.files().values())


def refusal(body: str) -> str:
	with pytest.raises(SituError) as caught:
		emit_c(body)
	return caught.value.diagnostic.render()


def test_the_clause_round_trips_through_unparse() -> None:
	"""Dropped here, the clause would vanish from every tool that reprints a
	schema while still compiling -- the quietest way to lose a feature."""
	source = unparse(parse_text(PREAMBLE + ADJACENT))
	assert "covers(flags)" in source
	assert "covers(flags)" in unparse(parse_text(source))


def test_a_contiguous_cover_widens_the_span_handed_to_the_codec() -> None:
	"""The whole point. `flags` sits at offset 0 and the region at 1, so the
	transform runs over both bytes from the start of the struct."""
	out = emit_c(ADJACENT)
	assert "app_hp_mask_decode(view.base + 0u," in out
	assert "1u + situ_adj_pn_len(view)" in out


def test_without_the_clause_the_region_covers_only_itself() -> None:
	"""The control. Without this the test above passes for a backend that
	widened every region, covers clause or not."""
	out = emit_c(ADJACENT.replace(" covers(flags)", ""))
	assert "app_hp_mask_decode(situ_adj_pn_ptr(view)," in out
	# Not a bare `view.base + 0u`, which the `flags` accessor legitimately
	# emits: what must be absent is the *decode* reaching outside the region.
	assert "app_hp_mask_decode(view.base" not in out


def test_a_split_cover_is_refused_rather_than_gathered() -> None:
	"""13.2a hands a codec one pointer and one length. Two spans with an
	uncovered `gap` between them have no single range, and gathering them
	would copy what a zero-copy accessor exists to avoid."""
	out = emit_c(SPLIT)
	assert "No decode accessor for `pn`" in out
	assert "not one contiguous run" in out
	# The accessor is genuinely absent, not merely commented near.
	assert "app_hp_mask_decode(" not in out


def test_covering_an_unknown_span_is_an_error() -> None:
	bad = ADJACENT.replace("covers(flags)", "covers(flgas)")
	text = refusal(bad)
	assert "covers unknown span `flgas`" in text
	# The names that were available, so the typo is fixable from the message.
	assert "flags" in text


def test_covering_itself_is_an_error() -> None:
	"""Either a no-op or a second pass over the same bytes; the schema does
	not say which, so neither is guessed at."""
	assert "covers itself" in refusal(
		ADJACENT.replace("covers(flags)", "covers(pn)"))


def test_the_interior_of_another_region_cannot_be_covered() -> None:
	"""Those bytes are transform output and do not exist until it has run
	(13.3), so naming one is an ordering error rather than a coverage one."""
	body = """struct nested {
	coded outer(hp) { u8 inner_field; }
	coded pn(hp) covers(inner_field) { u8 number; }
}
"""
	assert "covers unknown span `inner_field`" in refusal(body)


def test_a_length_changing_codec_may_not_cover_anything() -> None:
	"""What makes the clause coherent at all.

	A covered span sits at an offset the layout has already fixed. A codec
	that returns a different number of bytes than it was given would move it,
	so the decoded form would not correspond to the struct. Header protection,
	the case this exists for, is a mask and preserves length.
	"""
	body = """codec stuffed {
	kernel = stuffing(worst_case = 4, per = 3, unit = stream, code = smtp_dot);
}
impl stuffed derived;

struct s {
	u8 flags;
	coded body(stuffed) covers(flags) { u8 content[remaining]; }
}
"""
	text = refusal(body)
	assert "does not preserve length" in text
	assert "ratio_bounded" in text


def test_a_length_preserving_codec_may() -> None:
	"""The control for the test above: same shape, a codec that masks."""
	assert "app_hp_mask_decode" in emit_c(ADJACENT)


# A region, not a field. These exist because every test above names `flags`,
# and a filter that excluded region placements by kind therefore passed all of
# them while silently ignoring the clause in exactly the shape QUIC needs --
# the first byte wrapped in `authenticated` so the AEAD covers it too.

REGION_ADJACENT = """struct radj {
	authenticated first { u8 flags; }
	coded pn(hp) covers(first) { u8 number; }
	tag u8[16] covers(first);
}
"""

REGION_SPLIT = """struct rsplit {
	authenticated first { u8 flags; }
	u32 cid;
	coded pn(hp) covers(first) { u8 number; }
	tag u8[16] covers(first);
}
"""


def test_a_coded_region_may_cover_an_authenticated_region() -> None:
	"""`first` ends where `pn` begins, so the two are one run."""
	out = emit_c(REGION_ADJACENT)
	assert "app_hp_mask_decode(view.base + 0u," in out


def test_covering_a_region_across_a_gap_is_refused_too() -> None:
	"""The same refusal as for a field, and the case that regressed: a filter
	keyed on "is a field" dropped the region, left one span, and emitted the
	ordinary accessor for a coverage that should never have compiled."""
	out = emit_c(REGION_SPLIT)
	assert "No decode accessor for `pn`" in out
	assert "app_hp_mask_decode(" not in out
