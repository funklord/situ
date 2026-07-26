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
	};

	return cmocka_run_group_tests(tests, NULL, NULL);
}
