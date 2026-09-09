/* test_spans.c -- the scattered tier-1 ABI at run time (project.md 13.2b).
 *
 * `covers(...)` on a `coded` region sends its transform to a codec through a
 * list of spans rather than one pointer and one length, which is what makes
 * QUIC's header protection expressible: a mask over the first byte and the
 * packet number, with the connection id between them.
 *
 * Everything here is about the span *list*, never the algorithm -- a
 * signature says what a transform does and never how (13.1). What the
 * generated code decides, and what these tests hold it to, is which bytes go
 * to the codec and which do not. The mask in `codec_impl.c` is XOR with a
 * constant precisely so that "was this byte transformed" is a question with a
 * checkable answer.
 *
 * The test that matters most is the negative one. A span list that collapsed
 * the gap into a single run would still round-trip, still preserve length,
 * and still pass every property the codec declares -- and would quietly
 * transform four bytes no schema asked it to touch.
 */

#include <setjmp.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <cmocka.h>

#include "edges.h"

#define MASK 0xA5u

/* hp_split: first(1) cid(4) pn(1) rest(4) -- the covered spans are byte 0 and
 * byte 5, and `cid` at 1..4 sits in the gap uncovered. */
#define SPLIT_LEN 10u

static const uint8_t SPLIT[SPLIT_LEN] = {
	0x11,                       /* first  -- covered */
	0x22, 0x33, 0x44, 0x55,     /* cid    -- in the gap, NOT covered */
	0x66,                       /* pn     -- the region itself */
	0x77, 0x88, 0x99, 0xAA,     /* rest   -- after, not covered */
};

/* hp_packet: first(1) pn(1) rest(4) -- adjacent, so one merged span. */
#define PACKET_LEN 6u

static const uint8_t PACKET[PACKET_LEN] = {
	0x11, 0x66, 0x77, 0x88, 0x99, 0xAA,
};

static situ_view_t view_of(situ_msg_t *msg, uint8_t *buf, uint32_t len)
{
	situ_view_t view;

	situ_msg_init(msg, buf, len);
	view.base       = buf;
	view.limit      = len;
	view.generation = msg->generation;
	return view;
}

static void test_a_split_cover_transforms_both_spans(void **state)
{
	uint8_t     buf[SPLIT_LEN];
	situ_msg_t  msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, SPLIT, SPLIT_LEN);
	view = view_of(&msg, buf, SPLIT_LEN);

	assert_int_equal(situ_hp_split_pn_encode_spans(view), SITU_OK);
	assert_int_equal(buf[0], SPLIT[0] ^ MASK);
	assert_int_equal(buf[5], SPLIT[5] ^ MASK);
}

static void test_a_split_cover_leaves_the_gap_alone(void **state)
{
	uint8_t     buf[SPLIT_LEN];
	situ_msg_t  msg;
	situ_view_t view;
	uint32_t    i;

	/* The one that would catch a span list collapsing into a single run.
	 * `cid` is between the covered spans and no schema asked for it. */
	(void)state;
	memcpy(buf, SPLIT, SPLIT_LEN);
	view = view_of(&msg, buf, SPLIT_LEN);

	assert_int_equal(situ_hp_split_pn_encode_spans(view), SITU_OK);
	for (i = 1u; i < 5u; i++) {
		assert_int_equal(buf[i], SPLIT[i]);
	}
}

static void test_bytes_after_the_region_are_untouched(void **state)
{
	uint8_t     buf[SPLIT_LEN];
	situ_msg_t  msg;
	situ_view_t view;
	uint32_t    i;

	/* The other direction of the same worry: a length that ran to the end of
	 * the frame rather than to the end of the region. */
	(void)state;
	memcpy(buf, SPLIT, SPLIT_LEN);
	view = view_of(&msg, buf, SPLIT_LEN);

	assert_int_equal(situ_hp_split_pn_encode_spans(view), SITU_OK);
	for (i = 6u; i < SPLIT_LEN; i++) {
		assert_int_equal(buf[i], SPLIT[i]);
	}
}

static void test_the_transform_round_trips(void **state)
{
	uint8_t     buf[SPLIT_LEN];
	situ_msg_t  msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, SPLIT, SPLIT_LEN);
	view = view_of(&msg, buf, SPLIT_LEN);

	assert_int_equal(situ_hp_split_pn_encode_spans(view), SITU_OK);
	assert_int_equal(situ_hp_split_pn_decode_spans(view), SITU_OK);
	assert_memory_equal(buf, SPLIT, SPLIT_LEN);
}

static void test_an_adjacent_cover_transforms_one_merged_run(void **state)
{
	uint8_t     buf[PACKET_LEN];
	situ_msg_t  msg;
	situ_view_t view;
	uint32_t    i;

	/* Merged into a single span, and the merge must cover exactly the two
	 * members rather than rounding out to something tidier. */
	(void)state;
	memcpy(buf, PACKET, PACKET_LEN);
	view = view_of(&msg, buf, PACKET_LEN);

	assert_int_equal(situ_hp_packet_pn_encode_spans(view), SITU_OK);
	assert_int_equal(buf[0], PACKET[0] ^ MASK);
	assert_int_equal(buf[1], PACKET[1] ^ MASK);
	for (i = 2u; i < PACKET_LEN; i++) {
		assert_int_equal(buf[i], PACKET[i]);
	}
}

/* secret_run: n(1) key[n] tail(2), and `key` is `[secret]`. The eraser is
 * the one accessor that was never clamped: `situ_secret_run_key_len` wraps
 * the declared length in `situ_min_u32` and, two lines below it,
 * `_key_zeroize` passed the raw wire byte to `situ_zeroize`. With `n` at
 * 255 in a six-byte frame that wrote 251 bytes past the end -- ASan called
 * it a heap-buffer-overflow, and nothing in the suite could, because both
 * `[secret]` fixtures anybody had written were constant spans.
 *
 * A canary rather than a sanitizer, so the ordinary build catches it. */
#define RUN_FRAME  6u
#define RUN_ARENA 10u

static void test_zeroize_clamps_a_wire_declared_length(void **state)
{
	uint8_t     arena[RUN_ARENA];
	situ_msg_t  msg;
	situ_view_t view;
	uint32_t    i;

	(void)state;
	memset(arena, 0x5Au, sizeof arena);	/* the four past the frame */
	arena[0] = 255u;			/* n, straight off the wire */
	arena[1] = 0xAAu;
	arena[2] = 0xBBu;
	arena[3] = 0xCCu;
	arena[4] = 0xDDu;
	arena[5] = 0xEEu;
	view = view_of(&msg, arena, RUN_FRAME);

	/* What the frame really holds, which is what the eraser must use. */
	assert_int_equal(situ_secret_run_key_len(view), RUN_FRAME - 1u);

	situ_secret_run_key_zeroize(view);

	for (i = 1u; i < RUN_FRAME; i++) {
		assert_int_equal(arena[i], 0u);
	}
	for (i = RUN_FRAME; i < RUN_ARENA; i++) {
		assert_int_equal(arena[i], 0x5Au);
	}
}

static void test_zeroize_erases_the_key_and_stops(void **state)
{
	uint8_t     arena[RUN_FRAME];
	situ_msg_t  msg;
	situ_view_t view;

	/* The honest length, and the half a zero-length eraser passes: the
	 * three backends that read `size_bits` -- zero for a data-sized member
	 * rather than absent -- erased nothing here and said they had. */
	(void)state;
	arena[0] = 3u;
	arena[1] = 0xAAu;
	arena[2] = 0xBBu;
	arena[3] = 0xCCu;
	arena[4] = 0xDDu;
	arena[5] = 0xEEu;
	view = view_of(&msg, arena, RUN_FRAME);

	situ_secret_run_key_zeroize(view);

	assert_int_equal(arena[0], 3u);		/* the length itself stays */
	assert_int_equal(arena[1], 0u);
	assert_int_equal(arena[2], 0u);
	assert_int_equal(arena[3], 0u);
	assert_int_equal(arena[4], 0xDDu);	/* tail, not the eraser's */
	assert_int_equal(arena[5], 0xEEu);
}

int main(void)
{
	const struct CMUnitTest tests[] = {
		cmocka_unit_test(test_zeroize_clamps_a_wire_declared_length),
		cmocka_unit_test(test_zeroize_erases_the_key_and_stops),
		cmocka_unit_test(test_a_split_cover_transforms_both_spans),
		cmocka_unit_test(test_a_split_cover_leaves_the_gap_alone),
		cmocka_unit_test(test_bytes_after_the_region_are_untouched),
		cmocka_unit_test(test_the_transform_round_trips),
		cmocka_unit_test(test_an_adjacent_cover_transforms_one_merged_run),
	};

	return cmocka_run_group_tests(tests, NULL, NULL);
}
