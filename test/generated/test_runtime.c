/* test_runtime.c -- tests over the hand-written situ runtime.
 *
 * Compiled twice, with and without SITU_CHECKED. The generation tests assert
 * different behaviour in the two builds, which is the point: the checked
 * build catches a stale view, the release build compiles the check out.
 */

#include <stddef.h>
#include <stdint.h>

#include "situ_check.h"

#include "situ.h"

static uint8_t g_buf[32];

static void test_msg_init(void **state)
{
	situ_msg_t msg;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));

	situ_check_ptr_eq(msg.base, g_buf);
	situ_check_eq(msg.size, sizeof(g_buf));
	/* Generation starts at 1 so a zeroed view never looks live. */
	situ_check_eq(msg.generation, 1);
}

static void test_view_at_bounds(void **state)
{
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));

	situ_check_eq(situ_view_at(&msg, 0, 32, &view), SITU_OK);
	situ_check_ptr_eq(view.base, g_buf);
	situ_check_eq(view.limit, 32);

	situ_check_eq(situ_view_at(&msg, 8, 24, &view), SITU_OK);
	situ_check_ptr_eq(view.base, g_buf + 8);
	situ_check_eq(view.limit, 24);

	/* One byte past the end. */
	situ_check_eq(situ_view_at(&msg, 8, 25, &view), SITU_ERR_BOUNDS);
	situ_check_eq(situ_view_at(&msg, 33, 0, &view), SITU_ERR_BOUNDS);
}

static void test_view_at_no_overflow(void **state)
{
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));

	/* offset+extent overflows uint32; the check must not wrap into OK. */
	situ_check_eq(situ_view_at(&msg, 0xFFFFFFF0u, 0x20u, &view),
	                 SITU_ERR_BOUNDS);
	situ_check_eq(situ_view_at(&msg, 0x20u, 0xFFFFFFF0u, &view),
	                 SITU_ERR_BOUNDS);
}

static void test_view_sub(void **state)
{
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t sub;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));
	situ_check_eq(situ_view_at(&msg, 4, 16, &view), SITU_OK);

	situ_check_eq(situ_view_sub(view, 2, 8, &sub), SITU_OK);
	situ_check_ptr_eq(sub.base, g_buf + 6);
	situ_check_eq(sub.limit, 8);
	/* A sub-view inherits the parent's generation, not a fresh one. */
	situ_check_eq(sub.generation, view.generation);

	situ_check_eq(situ_view_sub(view, 10, 8, &sub), SITU_ERR_BOUNDS);
}

static void test_in_bounds(void **state)
{
	situ_view_t view = { g_buf, 16, 1, NULL };

	(void)state;
	situ_check_true(situ_in_bounds(view, 0, 16));
	situ_check_true(situ_in_bounds(view, 16, 0));
	situ_check_false(situ_in_bounds(view, 0, 17));
	situ_check_false(situ_in_bounds(view, 9, 8));
	situ_check_false(situ_in_bounds(view, 0xFFFFFFFFu, 1));
}

static void test_touch_increments_generation(void **state)
{
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));
	situ_check_eq(situ_view_at(&msg, 0, 16, &view), SITU_OK);
	situ_check_eq(view.generation, msg.generation);

	situ_msg_touch(&msg);
	situ_check_eq(msg.generation, 2);
	situ_check_ne(view.generation, msg.generation);
}

static void test_touch_skips_generation_zero(void **state)
{
	situ_msg_t msg;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));
	msg.generation = 0xFFFFFFFFu;

	situ_msg_touch(&msg);
	situ_check_eq(msg.generation, 1);
}

static void test_stale_view(void **state)
{
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));
	situ_check_eq(situ_view_at(&msg, 0, 16, &view), SITU_OK);
	situ_check_eq(situ_view_check(view), SITU_OK);

	situ_msg_touch(&msg);

#ifdef SITU_CHECKED
	situ_check_eq(situ_view_check(view), SITU_ERR_STALE);
#else
	/* The check is compiled out; a release build cannot see this. */
	situ_check_eq(situ_view_check(view), SITU_OK);
#endif
}

static void test_zeroed_view_is_never_live(void **state)
{
	situ_msg_t msg;
	situ_view_t view = { NULL, 0, 0, NULL };

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));

#ifdef SITU_CHECKED
	situ_check_eq(situ_view_check(view), SITU_ERR_STALE);
#else
	situ_check_eq(situ_view_check(view), SITU_OK);
#endif
}

static void test_err_str(void **state)
{
	(void)state;
	situ_check_str_eq(situ_err_str(SITU_OK), "ok");
	situ_check_str_eq(situ_err_str(SITU_ERR_BOUNDS), "out of bounds");
	situ_check_str_eq(situ_err_str(SITU_ERR_STALE), "stale view");
	situ_check_non_null(situ_err_str((situ_err_t)99));
}

/* -- text validation (section 8.6) ----------------------------------------- */

static void test_utf8_accepts_every_sequence_length(void **state)
{
	(void)state;

	situ_check_true(situ_utf8_valid((const uint8_t *)"hello", 5));
	situ_check_true(situ_utf8_valid((const uint8_t *)"\xc3\xa9", 2));		/* U+00E9 */
	situ_check_true(situ_utf8_valid((const uint8_t *)"\xe2\x82\xac", 3));	/* U+20AC */
	situ_check_true(situ_utf8_valid((const uint8_t *)"\xf0\x9d\x84\x9e", 4));	/* U+1D11E */
	situ_check_true(situ_utf8_valid((const uint8_t *)"\xf4\x8f\xbf\xbf", 4));	/* U+10FFFF */
	situ_check_true(situ_utf8_valid((const uint8_t *)"", 0));
}

static void test_utf8_rejects_a_second_spelling(void **state)
{
	/* RFC 3629 forbids overlong forms and surrogate halves, and the reason is
	 * situ's reason for caring about reserved bits: two byte sequences that
	 * mean one value let a sender vary bytes a receiver thinks are fixed.
	 *
	 * Every expectation here agrees with Python's strict decoder, which is an
	 * independent implementation rather than a value this project chose. */
	(void)state;

	situ_check_false(situ_utf8_valid((const uint8_t *)"\xc0\xaf", 2));
	situ_check_false(situ_utf8_valid((const uint8_t *)"\xe0\x80\xaf", 3));
	situ_check_false(situ_utf8_valid((const uint8_t *)"\xf0\x80\x80\xaf", 4));
	situ_check_false(situ_utf8_valid((const uint8_t *)"\xed\xa0\x80", 3));
	situ_check_false(situ_utf8_valid((const uint8_t *)"\xc1\xaf", 2));
}

static void test_utf8_rejects_what_is_not_a_character(void **state)
{
	(void)state;

	situ_check_false(situ_utf8_valid((const uint8_t *)"\xf4\x90\x80\x80", 4));	/* > U+10FFFF */
	situ_check_false(situ_utf8_valid((const uint8_t *)"\x80", 1));		/* lone continuation */
	situ_check_false(situ_utf8_valid((const uint8_t *)"\xfe", 1));
}

static void test_utf8_rejects_a_sequence_running_off_the_end(void **state)
{
	/* A field is a fixed number of bytes, so a multi-byte sequence starting
	 * near the end has nowhere to finish. Reading past it would be the bug
	 * this check exists to prevent. */
	(void)state;

	situ_check_false(situ_utf8_valid((const uint8_t *)"\xc3", 1));
	situ_check_false(situ_utf8_valid((const uint8_t *)"\xe2\x82", 2));
	situ_check_false(situ_utf8_valid((const uint8_t *)"ab\xf0\x9d\x84", 5));
}

static void test_ascii_is_the_seven_bit_half(void **state)
{
	(void)state;

	situ_check_true(situ_ascii_valid((const uint8_t *)"hello\x7f", 6));
	situ_check_false(situ_ascii_valid((const uint8_t *)"caf\xc3\xa9", 5));
}

static void test_nul_len_stops_at_the_terminator(void **state)
{
	(void)state;

	situ_check_eq(situ_nul_len((const uint8_t *)"hi\0\0\0", 5), 2u);
	situ_check_eq(situ_nul_len((const uint8_t *)"\0abcd", 5), 0u);
}

static void test_nul_len_is_bounded_by_the_capacity(void **state)
{
	/* An unterminated field reports the whole thing rather than running off
	 * the end. The field is a fixed number of bytes and this must not be the
	 * function that leaves it; `validate` is what refuses the field. */
	(void)state;

	situ_check_eq(situ_nul_len((const uint8_t *)"abcdefgh", 8), 8u);
	situ_check_false(situ_nul_terminated((const uint8_t *)"abcdefgh", 8));
	situ_check_true(situ_nul_terminated((const uint8_t *)"abcdefg\0", 8));
}

static void test_bytes_past_the_terminator_are_not_the_value(void **state)
{
	/* Which is why the field is NonCanonical: these two buffers differ, and
	 * mean the same thing. */
	static const uint8_t zeroed[8] = { 'h', 'i', 0, 0, 0, 0, 0, 0 };
	static const uint8_t junked[8] = { 'h', 'i', 0, 'X', 'Y', 'Z', '!', '?' };

	(void)state;

	situ_check_eq(situ_nul_len(zeroed, 8), situ_nul_len(junked, 8));
	situ_check_bytes(zeroed, junked, situ_nul_len(zeroed, 8));
}

/* -- checksums (section 26.15) --------------------------------------------- */

static void test_the_internet_checksum_matches_rfc_1071(void **state)
{
	/* The RFC's own worked example, which is the only value here that is a
	 * published constant rather than a property. */
	static const uint8_t example[8] = {
		0x00, 0x01, 0xf2, 0x03, 0xf4, 0xf5, 0xf6, 0xf7
	};

	(void)state;

	situ_check_eq(situ_checksum_internet(example, sizeof example), 0x220du);
}

static void test_a_block_carrying_its_checksum_sums_to_zero(void **state)
{
	/* RFC 1071's defining property, and how a receiver verifies: it does not
	 * recompute and compare, it sums the lot and expects zero. Worth testing
	 * as a property rather than against a header somebody wrote down, because
	 * it holds for every input rather than one. */
	uint8_t  header[20];
	uint16_t sum;
	unsigned i;

	(void)state;

	for (i = 0; i < sizeof header; i++) {
		header[i] = (uint8_t)(i * 7u + 3u);
	}
	header[10] = 0;
	header[11] = 0;

	sum        = situ_checksum_internet(header, sizeof header);
	header[10] = (uint8_t)(sum >> 8);
	header[11] = (uint8_t)(sum & 0xFFu);

	situ_check_eq(situ_checksum_internet(header, sizeof header), 0);
}

static void test_the_internet_checksum_carries_around(void **state)
{
	/* The fold is what makes it one's complement rather than two's, and a
	 * single 0xffff word is the smallest input that exercises it. */
	static const uint8_t ones[2] = { 0xff, 0xff };

	(void)state;

	situ_check_eq(situ_checksum_internet(ones, sizeof ones), 0);
}

static void test_an_odd_length_pads_the_last_byte_high(void **state)
{
	/* RFC 1071: the odd byte is the high half of a word, not the low one.
	 * Padding it the other way is a plausible-looking wrong answer. */
	static const uint8_t odd[3]  = { 0x12, 0x34, 0x56 };
	static const uint8_t even[4] = { 0x12, 0x34, 0x56, 0x00 };

	(void)state;

	situ_check_eq(situ_checksum_internet(odd, 3),
	                 situ_checksum_internet(even, 4));
}

static void test_fletcher16_matches_its_published_vectors(void **state)
{
	(void)state;

	situ_check_eq(situ_fletcher16((const uint8_t *)"abcde", 5),    0xc8f0u);
	situ_check_eq(situ_fletcher16((const uint8_t *)"abcdef", 6),   0x2057u);
	situ_check_eq(situ_fletcher16((const uint8_t *)"abcdefgh", 8), 0x0627u);
}

static void test_fletcher32_reads_its_words_little_endian(void **state)
{
	/* Not a free choice: the published vectors only come out right this way,
	 * and the big-endian reading gives a byte-swapped near-miss that looks
	 * entirely plausible until it is compared with somebody else's answer. */
	(void)state;

	situ_check_eq(situ_fletcher32((const uint8_t *)"abcde", 5),    0xf04fc729u);
	situ_check_eq(situ_fletcher32((const uint8_t *)"abcdef", 6),   0x56502d2au);
	situ_check_eq(situ_fletcher32((const uint8_t *)"abcdefgh", 8), 0xebe19591u);
}

static void test_adler32_matches_zlib(void **state)
{
	/* These come from Python's zlib, which is an independent implementation of
	 * RFC 1950 rather than a number this project wrote down for itself. */
	(void)state;

	situ_check_eq(situ_adler32((const uint8_t *)"", 0),           0x00000001u);
	situ_check_eq(situ_adler32((const uint8_t *)"a", 1),          0x00620062u);
	situ_check_eq(situ_adler32((const uint8_t *)"abc", 3),        0x024d0127u);
	situ_check_eq(situ_adler32((const uint8_t *)"Wikipedia", 9),  0x11e60398u);
	situ_check_eq(situ_adler32((const uint8_t *)"123456789", 9),  0x091e01deu);
}

static void test_the_sum_checksums_reject_a_reordering(void **state)
{
	/* What a positional checksum buys over a plain sum: swapping two bytes
	 * changes it. The internet checksum does not have this property and is not
	 * asked for it here. */
	static const uint8_t forward[4] = { 1, 2, 3, 4 };
	static const uint8_t swapped[4] = { 1, 3, 2, 4 };

	(void)state;

	situ_check_ne(situ_fletcher16(forward, 4), situ_fletcher16(swapped, 4));
	situ_check_ne(situ_adler32(forward, 4),    situ_adler32(swapped, 4));
}

/* -- packed BCD (section 8.1) ---------------------------------------------- */

static void test_bcd_round_trips_every_two_digit_value(void **state)
{
	/* Exhaustive at this width, which is what an RTC actually stores. */
	unsigned value;

	(void)state;

	for (value = 0; value < 100u; value++) {
		uint64_t packed = situ_bcd_encode(value, 2u);

		situ_check_true(situ_bcd_valid(packed, 2u));
		situ_check_eq(situ_bcd_decode(packed, 2u), value);
	}
}

static void test_bcd_packs_a_digit_to_a_nibble(void **state)
{
	/* The property that makes BCD worth having: the hex reading of the bytes
	 * is the decimal number, which is why a seven-segment decoder can take
	 * them directly and why Wireshark shows the field in hex. */
	(void)state;

	situ_check_eq(situ_bcd_encode(59u, 2u), 0x59u);
	situ_check_eq(situ_bcd_encode(1234u, 4u), 0x1234u);
	situ_check_eq(situ_bcd_encode(12345678u, 8u), 0x12345678u);

	situ_check_eq(situ_bcd_decode(0x59u, 2u), 59u);
	situ_check_eq(situ_bcd_decode(0x12345678u, 8u), 12345678u);
}

static void test_a_nibble_above_nine_is_not_a_digit(void **state)
{
	/* A BCD field can hold a bit pattern that is not a number. The getter
	 * cannot report that -- it returns a number either way -- so validation is
	 * where it has to be caught, and this is what validation asks. */
	(void)state;

	situ_check_true(situ_bcd_valid(0x99u, 2u));
	situ_check_false(situ_bcd_valid(0x9Au, 2u));
	situ_check_false(situ_bcd_valid(0xA0u, 2u));
	situ_check_false(situ_bcd_valid(0xFFFFu, 4u));

	/* Only the digits in range are examined: rubbish above them is not this
	 * field's business. */
	situ_check_true(situ_bcd_valid(0xFF12u, 2u));
}

static void test_bcd_encode_truncates_rather_than_overflowing(void **state)
{
	/* Two digits cannot hold 100. Wrapping is the defined behaviour, and it is
	 * the setter's caller who is out of range -- which `[max = 59]` on the
	 * schema is there to catch first. */
	(void)state;

	situ_check_eq(situ_bcd_encode(100u, 2u), 0x00u);
	situ_check_eq(situ_bcd_encode(123u, 2u), 0x23u);
}

int main(void)
{
	const struct situ_check_case tests[] = {
		situ_check_case(test_msg_init),
		situ_check_case(test_view_at_bounds),
		situ_check_case(test_view_at_no_overflow),
		situ_check_case(test_view_sub),
		situ_check_case(test_in_bounds),
		situ_check_case(test_touch_increments_generation),
		situ_check_case(test_touch_skips_generation_zero),
		situ_check_case(test_stale_view),
		situ_check_case(test_zeroed_view_is_never_live),
		situ_check_case(test_err_str),
		situ_check_case(test_nul_len_stops_at_the_terminator),
		situ_check_case(test_nul_len_is_bounded_by_the_capacity),
		situ_check_case(test_bytes_past_the_terminator_are_not_the_value),
		situ_check_case(test_utf8_accepts_every_sequence_length),
		situ_check_case(test_utf8_rejects_a_second_spelling),
		situ_check_case(test_utf8_rejects_what_is_not_a_character),
		situ_check_case(test_utf8_rejects_a_sequence_running_off_the_end),
		situ_check_case(test_ascii_is_the_seven_bit_half),
		situ_check_case(test_the_internet_checksum_matches_rfc_1071),
		situ_check_case(test_a_block_carrying_its_checksum_sums_to_zero),
		situ_check_case(test_the_internet_checksum_carries_around),
		situ_check_case(test_an_odd_length_pads_the_last_byte_high),
		situ_check_case(test_fletcher16_matches_its_published_vectors),
		situ_check_case(test_fletcher32_reads_its_words_little_endian),
		situ_check_case(test_adler32_matches_zlib),
		situ_check_case(test_the_sum_checksums_reject_a_reordering),
		situ_check_case(test_bcd_round_trips_every_two_digit_value),
		situ_check_case(test_bcd_packs_a_digit_to_a_nibble),
		situ_check_case(test_a_nibble_above_nine_is_not_a_digit),
		situ_check_case(test_bcd_encode_truncates_rather_than_overflowing),
	};

	return situ_check_run(tests, sizeof tests / sizeof tests[0], "test_runtime");
}
