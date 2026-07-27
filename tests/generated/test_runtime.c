/* test_runtime.c -- cmocka tests over the hand-written situ runtime.
 *
 * Compiled twice, with and without SITU_CHECKED. The generation tests assert
 * different behaviour in the two builds, which is the point: the checked
 * build catches a stale view, the release build compiles the check out.
 */

#include <setjmp.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>

#include <cmocka.h>

#include "situ.h"

static uint8_t g_buf[32];

static void test_msg_init(void **state)
{
	situ_msg_t msg;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));

	assert_ptr_equal(msg.base, g_buf);
	assert_int_equal(msg.size, sizeof(g_buf));
	/* Generation starts at 1 so a zeroed view never looks live. */
	assert_int_equal(msg.generation, 1);
}

static void test_view_at_bounds(void **state)
{
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));

	assert_int_equal(situ_view_at(&msg, 0, 32, &view), SITU_OK);
	assert_ptr_equal(view.base, g_buf);
	assert_int_equal(view.limit, 32);

	assert_int_equal(situ_view_at(&msg, 8, 24, &view), SITU_OK);
	assert_ptr_equal(view.base, g_buf + 8);
	assert_int_equal(view.limit, 24);

	/* One byte past the end. */
	assert_int_equal(situ_view_at(&msg, 8, 25, &view), SITU_ERR_BOUNDS);
	assert_int_equal(situ_view_at(&msg, 33, 0, &view), SITU_ERR_BOUNDS);
}

static void test_view_at_no_overflow(void **state)
{
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));

	/* offset+extent overflows uint32; the check must not wrap into OK. */
	assert_int_equal(situ_view_at(&msg, 0xFFFFFFF0u, 0x20u, &view),
	                 SITU_ERR_BOUNDS);
	assert_int_equal(situ_view_at(&msg, 0x20u, 0xFFFFFFF0u, &view),
	                 SITU_ERR_BOUNDS);
}

static void test_view_sub(void **state)
{
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t sub;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));
	assert_int_equal(situ_view_at(&msg, 4, 16, &view), SITU_OK);

	assert_int_equal(situ_view_sub(view, 2, 8, &sub), SITU_OK);
	assert_ptr_equal(sub.base, g_buf + 6);
	assert_int_equal(sub.limit, 8);
	/* A sub-view inherits the parent's generation, not a fresh one. */
	assert_int_equal(sub.generation, view.generation);

	assert_int_equal(situ_view_sub(view, 10, 8, &sub), SITU_ERR_BOUNDS);
}

static void test_in_bounds(void **state)
{
	situ_view_t view = { g_buf, 16, 1 };

	(void)state;
	assert_true(situ_in_bounds(view, 0, 16));
	assert_true(situ_in_bounds(view, 16, 0));
	assert_false(situ_in_bounds(view, 0, 17));
	assert_false(situ_in_bounds(view, 9, 8));
	assert_false(situ_in_bounds(view, 0xFFFFFFFFu, 1));
}

static void test_touch_increments_generation(void **state)
{
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));
	assert_int_equal(situ_view_at(&msg, 0, 16, &view), SITU_OK);
	assert_int_equal(view.generation, msg.generation);

	situ_msg_touch(&msg);
	assert_int_equal(msg.generation, 2);
	assert_int_not_equal(view.generation, msg.generation);
}

static void test_touch_skips_generation_zero(void **state)
{
	situ_msg_t msg;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));
	msg.generation = 0xFFFFFFFFu;

	situ_msg_touch(&msg);
	assert_int_equal(msg.generation, 1);
}

static void test_stale_view(void **state)
{
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));
	assert_int_equal(situ_view_at(&msg, 0, 16, &view), SITU_OK);
	assert_int_equal(situ_view_check(&msg, view), SITU_OK);

	situ_msg_touch(&msg);

#ifdef SITU_CHECKED
	assert_int_equal(situ_view_check(&msg, view), SITU_ERR_STALE);
#else
	/* The check is compiled out; a release build cannot see this. */
	assert_int_equal(situ_view_check(&msg, view), SITU_OK);
#endif
}

static void test_zeroed_view_is_never_live(void **state)
{
	situ_msg_t msg;
	situ_view_t view = { NULL, 0, 0 };

	(void)state;
	situ_msg_init(&msg, g_buf, (uint32_t)sizeof(g_buf));

#ifdef SITU_CHECKED
	assert_int_equal(situ_view_check(&msg, view), SITU_ERR_STALE);
#else
	assert_int_equal(situ_view_check(&msg, view), SITU_OK);
#endif
}

static void test_err_str(void **state)
{
	(void)state;
	assert_string_equal(situ_err_str(SITU_OK), "ok");
	assert_string_equal(situ_err_str(SITU_ERR_BOUNDS), "out of bounds");
	assert_string_equal(situ_err_str(SITU_ERR_STALE), "stale view");
	assert_non_null(situ_err_str((situ_err_t)99));
}

/* -- packed BCD (section 8.1) ---------------------------------------------- */

static void test_bcd_round_trips_every_two_digit_value(void **state)
{
	/* Exhaustive at this width, which is what an RTC actually stores. */
	unsigned value;

	(void)state;

	for (value = 0; value < 100u; value++) {
		uint64_t packed = situ_bcd_encode(value, 2u);

		assert_true(situ_bcd_valid(packed, 2u));
		assert_int_equal(situ_bcd_decode(packed, 2u), value);
	}
}

static void test_bcd_packs_a_digit_to_a_nibble(void **state)
{
	/* The property that makes BCD worth having: the hex reading of the bytes
	 * is the decimal number, which is why a seven-segment decoder can take
	 * them directly and why Wireshark shows the field in hex. */
	(void)state;

	assert_int_equal(situ_bcd_encode(59u, 2u), 0x59u);
	assert_int_equal(situ_bcd_encode(1234u, 4u), 0x1234u);
	assert_int_equal(situ_bcd_encode(12345678u, 8u), 0x12345678u);

	assert_int_equal(situ_bcd_decode(0x59u, 2u), 59u);
	assert_int_equal(situ_bcd_decode(0x12345678u, 8u), 12345678u);
}

static void test_a_nibble_above_nine_is_not_a_digit(void **state)
{
	/* A BCD field can hold a bit pattern that is not a number. The getter
	 * cannot report that -- it returns a number either way -- so validation is
	 * where it has to be caught, and this is what validation asks. */
	(void)state;

	assert_true(situ_bcd_valid(0x99u, 2u));
	assert_false(situ_bcd_valid(0x9Au, 2u));
	assert_false(situ_bcd_valid(0xA0u, 2u));
	assert_false(situ_bcd_valid(0xFFFFu, 4u));

	/* Only the digits in range are examined: rubbish above them is not this
	 * field's business. */
	assert_true(situ_bcd_valid(0xFF12u, 2u));
}

static void test_bcd_encode_truncates_rather_than_overflowing(void **state)
{
	/* Two digits cannot hold 100. Wrapping is the defined behaviour, and it is
	 * the setter's caller who is out of range -- which `[max = 59]` on the
	 * schema is there to catch first. */
	(void)state;

	assert_int_equal(situ_bcd_encode(100u, 2u), 0x00u);
	assert_int_equal(situ_bcd_encode(123u, 2u), 0x23u);
}

int main(void)
{
	const struct CMUnitTest tests[] = {
		cmocka_unit_test(test_msg_init),
		cmocka_unit_test(test_view_at_bounds),
		cmocka_unit_test(test_view_at_no_overflow),
		cmocka_unit_test(test_view_sub),
		cmocka_unit_test(test_in_bounds),
		cmocka_unit_test(test_touch_increments_generation),
		cmocka_unit_test(test_touch_skips_generation_zero),
		cmocka_unit_test(test_stale_view),
		cmocka_unit_test(test_zeroed_view_is_never_live),
		cmocka_unit_test(test_err_str),
		cmocka_unit_test(test_bcd_round_trips_every_two_digit_value),
		cmocka_unit_test(test_bcd_packs_a_digit_to_a_nibble),
		cmocka_unit_test(test_a_nibble_above_nine_is_not_a_digit),
		cmocka_unit_test(test_bcd_encode_truncates_rather_than_overflowing),
	};

	return cmocka_run_group_tests(tests, NULL, NULL);
}
