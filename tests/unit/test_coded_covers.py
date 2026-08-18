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


def test_a_contiguous_cover_reaches_the_codec_as_one_span() -> None:
	"""`flags` sits at offset 0 and the region at 1, so the two merge into a
	single run -- the span count a caller sees is the number of *separated*
	pieces, not the number of names written down."""
	out = emit_c(ADJACENT)
	assert "situ_span_t spans[1];" in out
	assert "spans[0].base = view.base + 0u;" in out
	assert "spans[0].len  = 1u + situ_adj_pn_len(view);" in out
	assert "app_hp_mask_decode_spans(spans, 1u);" in out


def test_without_the_clause_the_ordinary_abi_is_used() -> None:
	"""The control, and the compatibility guarantee. A region with no clause
	keeps 13.2a exactly: one pointer, one length, an output buffer -- so every
	codec written against it goes on working."""
	out = emit_c(ADJACENT.replace(" covers(flags)", ""))
	assert "app_hp_mask_decode(situ_adj_pn_ptr(view)," in out
	assert "situ_span_t" not in out
	assert "_decode_spans" not in out


def test_a_split_cover_emits_two_spans_and_skips_the_gap() -> None:
	"""What widening the ABI bought. `flags` is at 0, `gap` occupies 1..5 and
	is not covered, and the region follows -- so the codec is handed two
	spans and the four bytes between them are untouched."""
	out = emit_c(SPLIT)
	assert "situ_span_t spans[2];" in out
	assert "spans[0].base = view.base + 0u;" in out
	assert "spans[0].len  = 1u;" in out
	assert "spans[1].base = view.base + 5u;" in out
	assert "app_hp_mask_decode_spans(spans, 2u);" in out


def test_a_split_cover_does_not_swallow_the_uncovered_gap() -> None:
	"""The assertion that would catch a merge doing too much: a single span
	from 0 running the whole width would transform `gap` as well, which no
	schema asked for and which no length check would notice."""
	out = emit_c(SPLIT)
	assert "spans[0].len  = 1u;" in out
	assert "_decode_spans(spans, 1u);" not in out


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
	assert "app_hp_mask_decode_spans" in emit_c(ADJACENT)


def test_both_directions_are_emitted() -> None:
	"""A mask is applied as well as removed. The contiguous ABI needs only a
	decode accessor because the plaintext is somewhere else; an in-place
	transform has to be reversible from the same side, so both are here."""
	out = emit_c(SPLIT)
	assert "situ_split_pn_encode_spans(situ_view_t view)" in out
	assert "situ_split_pn_decode_spans(situ_view_t view)" in out
	assert "app_hp_mask_encode_spans(spans, 2u);" in out


# A region, not a field. These exist because every test above names `flags`,
# and a filter that excluded region placements by kind therefore passed all of
# them while silently ignoring the clause in exactly the shape QUIC needs --
# the first byte wrapped in `authenticated` so the AEAD covers it too.

# `[tag_order = after]` because a tag covers `first` here, which 14.1b makes
# an error to leave unsaid. These predate that rule and were the first schemas
# it caught -- which is the rule working, not the fixtures being awkward.
REGION_ADJACENT = """struct radj {
	authenticated first { u8 flags; }
	coded pn(hp) covers(first) [tag_order = after] { u8 number; }
	tag u8[16] covers(first);
}
"""

REGION_SPLIT = """struct rsplit {
	authenticated first { u8 flags; }
	u32 cid;
	coded pn(hp) covers(first) [tag_order = after] { u8 number; }
	tag u8[16] covers(first);
}
"""


def test_a_coded_region_may_cover_an_authenticated_region() -> None:
	"""`first` ends where `pn` begins, so the two are one run."""
	out = emit_c(REGION_ADJACENT)
	assert "situ_span_t spans[1];" in out
	assert "spans[0].base = view.base + 0u;" in out


def test_covering_a_region_across_a_gap_emits_two_spans_too() -> None:
	"""The case that regressed once: a filter keyed on "is a field" dropped
	the region, left one span, and emitted an accessor covering the wrong
	bytes. Two spans is what says the region was seen."""
	out = emit_c(REGION_SPLIT)
	assert "situ_span_t spans[2];" in out
	assert "app_hp_mask_decode_spans(spans, 2u);" in out


# Ordering against a tag (14.1b, decision 0037). A `covers` clause that
# reaches authenticated bytes has two coherent orders, and the schema says
# which -- 17.0's case rather than 0011's, because both orders terminate.

TAGGED = """struct tagged {
	authenticated first { u8 flags; }
	coded pn(hp) covers(first)%s { u8 number; }
	tag u8[16] covers(first);
}
"""


def test_an_unordered_transform_over_authenticated_bytes_is_refused() -> None:
	"""Both orders produce the same bytes in the same places, so the wrong
	one surfaces as a failed tag somewhere else entirely."""
	text = refusal(TAGGED % "")
	assert "does not say in which order" in text
	assert "tag_order = after" in text
	assert "tag_order = before" in text


def test_tag_order_after_leaves_the_tag_alone() -> None:
	"""The tag was computed over the untransformed bytes and still matches
	them, so applying the transform invalidates nothing -- and the accessor
	needs no message to say so."""
	out = emit_c(TAGGED % " [tag_order = after]")
	assert "situ_tagged_pn_encode_spans(situ_view_t view)" in out
	assert "situ_tagged_pn_decode_spans(situ_view_t view)" in out


def test_tag_order_before_marks_the_tag_dirty() -> None:
	"""The tag covers the transform's output, so applying it leaves the tag
	stale. The signature carries the obligation: there is no way to call this
	without somewhere to record the staleness."""
	out = emit_c(TAGGED % " [tag_order = before]")
	assert "situ_tagged_pn_encode_spans(situ_msg_t *msg, situ_view_t view)" in out
	assert "situ_msg_mark_dirty(msg, SITU_TAGGED_TAG_DIRTY);" in out


def test_the_dirty_bit_is_set_only_when_the_codec_succeeded() -> None:
	"""A codec that refused its input has not changed the bytes, so the tag
	is exactly as stale as it was."""
	out = emit_c(TAGGED % " [tag_order = before]")
	marked = out.index("situ_msg_mark_dirty(msg, SITU_TAGGED_TAG_DIRTY);")
	guard  = out.rindex("if (err != SITU_OK) {", 0, marked)
	assert "return err;" in out[guard:marked]


def test_tag_order_without_a_tag_is_refused() -> None:
	"""An attribute that decides no order is a construct whose meaning is
	silently nothing, which is what 14.5 refuses."""
	text = refusal(ADJACENT.replace("covers(flags)",
	                                "covers(flags) [tag_order = after]"))
	assert "no tag covers what it transforms" in text


def test_an_unknown_tag_order_is_refused() -> None:
	assert "unknown `tag_order`" in refusal(
		TAGGED % " [tag_order = sideways]")
