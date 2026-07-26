/* test_tiff.c -- the byte-order-marker case (project.md section 8.3).
 *
 * The phase 4 acceptance criterion: a byte-order-marked struct parses both
 * orders from hex vectors, and the generated host constant matches the build's
 * endianness.
 */

#include <setjmp.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <cmocka.h>

#include "tiff.h"

/* The same header, written twice. Both encode magic=42 and ifd_offset=8; only
 * the marker and the byte order differ. */
static const uint8_t LITTLE[SITU_TIFFHEADER_SIZE_FIXED] = {
	0x49, 0x49,			/* "II" */
	0x2A, 0x00,			/* 42, little endian */
	0x08, 0x00, 0x00, 0x00,		/* 8, little endian */
};

static const uint8_t BIG[SITU_TIFFHEADER_SIZE_FIXED] = {
	0x4D, 0x4D,			/* "MM" */
	0x00, 0x2A,			/* 42, big endian */
	0x00, 0x00, 0x00, 0x08,		/* 8, big endian */
};

static situ_view_t view_of(situ_msg_t *msg, uint8_t *buf)
{
	situ_view_t view;

	situ_msg_init(msg, buf, SITU_TIFFHEADER_SIZE_FIXED);
	assert_int_equal(situ_TiffHeader_view(msg, 0, &view), SITU_OK);
	return view;
}

static void test_little_endian_vector(void **state)
{
	uint8_t buf[SITU_TIFFHEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, LITTLE, sizeof(buf));
	view = view_of(&msg, buf);

	assert_true(situ_TiffHeader_byte_order_is_little(view));
	assert_int_equal(situ_TiffHeader_magic_get(view), 42);
	assert_int_equal(situ_TiffHeader_ifd_offset_get(view), 8);
}

static void test_big_endian_vector(void **state)
{
	uint8_t buf[SITU_TIFFHEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, BIG, sizeof(buf));
	view = view_of(&msg, buf);

	assert_false(situ_TiffHeader_byte_order_is_little(view));
	assert_int_equal(situ_TiffHeader_magic_get(view), 42);
	assert_int_equal(situ_TiffHeader_ifd_offset_get(view), 8);
}

static void test_both_orders_yield_the_same_values(void **state)
{
	uint8_t little_buf[SITU_TIFFHEADER_SIZE_FIXED];
	uint8_t big_buf[SITU_TIFFHEADER_SIZE_FIXED];
	situ_msg_t little_msg;
	situ_msg_t big_msg;
	situ_view_t little_view;
	situ_view_t big_view;

	(void)state;
	memcpy(little_buf, LITTLE, sizeof(little_buf));
	memcpy(big_buf, BIG, sizeof(big_buf));
	little_view = view_of(&little_msg, little_buf);
	big_view    = view_of(&big_msg, big_buf);

	/* Two different byte sequences, one value. That is exactly why the format
	 * is CanonicalGiven(byte_order) rather than Canonical, and why signing has
	 * to verify over received bytes rather than re-encoded ones. */
	assert_memory_not_equal(little_buf, big_buf, sizeof(little_buf));
	assert_int_equal(situ_TiffHeader_magic_get(little_view),
	                 situ_TiffHeader_magic_get(big_view));
	assert_int_equal(situ_TiffHeader_ifd_offset_get(little_view),
	                 situ_TiffHeader_ifd_offset_get(big_view));
}

static void test_host_constant_matches_the_build(void **state)
{
	uint16_t probe = 1;
	int host_is_little;

	(void)state;
	host_is_little = (*(const uint8_t *)&probe) == 1;

	if (host_is_little) {
		assert_int_equal(situ_TiffHeader_byte_order_host(),
		                 SITU_TIFFHEADER_BYTE_ORDER_LITTLE);
	} else {
		assert_int_equal(situ_TiffHeader_byte_order_host(),
		                 SITU_TIFFHEADER_BYTE_ORDER_BIG);
	}
}

static void test_writer_emits_host_order_and_the_matching_marker(void **state)
{
	uint8_t buf[SITU_TIFFHEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memset(buf, 0, sizeof(buf));
	view = view_of(&msg, buf);

	/* A writer stores the host marker first, then values; every subsequent
	 * accessor branches on what it wrote. This is what makes the writer
	 * deterministic even though the format is not canonical. */
	situ_TiffHeader_byte_order_set_host(view);
	situ_TiffHeader_magic_set(view, 42);
	situ_TiffHeader_ifd_offset_set(view, 8);

	assert_int_equal(situ_TiffHeader_magic_get(view), 42);
	assert_int_equal(situ_TiffHeader_ifd_offset_get(view), 8);
	assert_int_equal(situ_TiffHeader_validate(view), SITU_OK);

	/* And the bytes are one of the two legal encodings, not a mixture. */
	if (situ_TiffHeader_byte_order_is_little(view)) {
		assert_memory_equal(buf, LITTLE, sizeof(buf));
	} else {
		assert_memory_equal(buf, BIG, sizeof(buf));
	}
}

static void test_round_trip_preserves_each_order(void **state)
{
	uint8_t buf[SITU_TIFFHEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;
	unsigned i;
	const uint8_t *vectors[2];

	(void)state;
	vectors[0] = LITTLE;
	vectors[1] = BIG;

	for (i = 0; i < 2u; i++) {
		uint16_t magic;
		uint32_t ifd;

		memcpy(buf, vectors[i], sizeof(buf));
		view  = view_of(&msg, buf);
		magic = situ_TiffHeader_magic_get(view);
		ifd   = situ_TiffHeader_ifd_offset_get(view);

		/* Writing the values back must reproduce the *received* order, not
		 * the host's: the marker in the buffer is what the setters branch on. */
		situ_TiffHeader_magic_set(view, magic);
		situ_TiffHeader_ifd_offset_set(view, ifd);

		assert_memory_equal(buf, vectors[i], sizeof(buf));
	}
}

static void test_validate_rejects_a_wrong_magic(void **state)
{
	uint8_t buf[SITU_TIFFHEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, LITTLE, sizeof(buf));
	view = view_of(&msg, buf);

	assert_int_equal(situ_TiffHeader_validate(view), SITU_OK);

	/* Section 8.3: a must_eq constraint in marker scope is validated after the
	 * marker resolves, not before. */
	situ_TiffHeader_magic_set(view, 43);
	assert_int_equal(situ_TiffHeader_validate(view), SITU_ERR_CONSTRAINT);
}

int main(void)
{
	const struct CMUnitTest tests[] = {
		cmocka_unit_test(test_little_endian_vector),
		cmocka_unit_test(test_big_endian_vector),
		cmocka_unit_test(test_both_orders_yield_the_same_values),
		cmocka_unit_test(test_host_constant_matches_the_build),
		cmocka_unit_test(test_writer_emits_host_order_and_the_matching_marker),
		cmocka_unit_test(test_round_trip_preserves_each_order),
		cmocka_unit_test(test_validate_rejects_a_wrong_magic),
	};

	return cmocka_run_group_tests(tests, NULL, NULL);
}
