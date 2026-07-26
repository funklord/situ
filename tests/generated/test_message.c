/* test_message.c -- islands of staticness (project.md example 5.2).
 *
 * The phase 5 acceptance criteria that need running code: a stale view is
 * caught under SITU_CHECKED, and mutating a preceding length field increments
 * the generation.
 *
 * The point of the schema is that `Record` is fully static once its base is
 * resolved, even though the base itself is not known until parse time. These
 * tests exercise exactly that: one runtime resolution, then constant offsets.
 */

#include <setjmp.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <cmocka.h>

#include "message.h"

/* An 11-byte header, 4 bytes of options, two records, then a 3-byte trailer.
 *
 *   00      version   = 1
 *   01      type      = data
 *   02      flags     = 0
 *   03..04  length    = 4      (options)
 *   05..06  rec_count = 2
 *   07..0A  seq       = 0x11223344
 *   0B..0E  opts
 *   0F..16  recs[0]   id=1 kind=2 value=3
 *   17..1E  recs[1]   id=4 kind=5 value=6
 *   1F..21  trailer
 */
#define VECTOR_LEN 34u

static const uint8_t VECTOR[VECTOR_LEN] = {
	0x01, 0x02, 0x00, 0x00, 0x04, 0x00, 0x02, 0x11, 0x22, 0x33, 0x44,
	0xAA, 0xBB, 0xCC, 0xDD,
	0x00, 0x00, 0x00, 0x01, 0x00, 0x02, 0x00, 0x03,
	0x00, 0x00, 0x00, 0x04, 0x00, 0x05, 0x00, 0x06,
	0xEE, 0xEF, 0xF0,
};

static situ_view_t view_of(situ_msg_t *msg, uint8_t *buf)
{
	situ_view_t view;

	situ_msg_init(msg, buf, VECTOR_LEN);
	assert_int_equal(situ_Message_view(msg, 0, VECTOR_LEN, &view), SITU_OK);
	return view;
}

static void test_static_prefix_is_still_static(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t hdr;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf);

	/* Everything before `opts` keeps an absolute offset, which is the locality
	 * rule: a dynamic member weakens what follows it and nothing else. */
	assert_int_equal(situ_Message_hdr_view(view, &hdr), SITU_OK);
	assert_ptr_equal(hdr.base, buf);
	assert_int_equal(situ_Header_seq_get(hdr), 0x11223344u);
	assert_int_equal(situ_Header_length_get(hdr), 4);
	assert_int_equal(situ_Header_rec_count_get(hdr), 2);
}

static void test_dynamic_offset_is_resolved_from_the_data(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf);

	/* opts is at a static 11; recs starts after it, which needs the length. */
	assert_int_equal(situ_Message_opts_len(view), 4);
	assert_ptr_equal(situ_Message_opts_ptr(view), buf + 11);
	assert_int_equal(situ_Message_recs_offset(view), 15);
	assert_int_equal(situ_Message_recs_count(view), 2);
}

static void test_frame_elements_are_static_once_the_base_is_found(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t record;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf);

	/* One runtime resolution per element, then constant offsets inside it.
	 * The accessors are Record's own: a dynamically positioned static struct
	 * gets its static capabilities back (section 12.2). */
	assert_int_equal(situ_Message_recs_at(view, 0, &record), SITU_OK);
	assert_ptr_equal(record.base, buf + 15);
	assert_int_equal(situ_Record_id_get(record), 1);
	assert_int_equal(situ_Record_kind_get(record), 2);
	assert_int_equal(situ_Record_value_get(record), 3);

	assert_int_equal(situ_Message_recs_at(view, 1, &record), SITU_OK);
	assert_ptr_equal(record.base, buf + 23);
	assert_int_equal(situ_Record_id_get(record), 4);
	assert_int_equal(situ_Record_value_get(record), 6);
}

static void test_element_mutation_is_in_place(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t record;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf);

	/* `require in_place(Message.recs[].value)` says this moves nothing. */
	assert_int_equal(situ_Message_recs_at(view, 1, &record), SITU_OK);
	situ_Record_value_set(record, 0x0BAD);

	assert_int_equal(situ_Record_value_get(record), 0x0BAD);
	assert_int_equal(buf[29], 0x0B);
	assert_int_equal(buf[30], 0xAD);
	/* Nothing else moved. */
	assert_memory_equal(buf, VECTOR, 29);
	assert_memory_equal(buf + 31, VECTOR + 31, 3);
}

static void test_trailer_runs_to_the_end_of_the_view(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf);

	assert_int_equal(situ_Message_trailer_offset(view), 31);
	assert_int_equal(situ_Message_trailer_len(view), 3);
	assert_ptr_equal(situ_Message_trailer_ptr(view), buf + 31);
}

static void test_length_write_shifts_everything_after_it(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf);

	assert_int_equal(situ_Message_recs_offset(view), 15);

	/* Growing the options moves the records. The schema said this would
	 * happen -- `mutate=Shifting` on opts -- and here it is. */
	situ_Message_hdr_length_set(&msg, view, 8);
	assert_int_equal(situ_Message_recs_offset(view), 19);
}

static void test_length_write_increments_the_generation(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view;
	uint32_t before;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf);
	before = msg.generation;

	situ_Message_hdr_length_set(&msg, view, 8);

	assert_int_not_equal(msg.generation, before);
	assert_int_equal(view.generation, before);
}

static void test_stale_view_is_caught_when_checked(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t record;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf);

	assert_int_equal(situ_Message_recs_at(view, 0, &record), SITU_OK);
	assert_int_equal(situ_view_check(&msg, record), SITU_OK);

	/* The element view was taken before the shift, so it now points at the
	 * wrong bytes. This is the bug class section 12.3 exists to catch. */
	situ_Message_hdr_length_set(&msg, view, 8);

#ifdef SITU_CHECKED
	assert_int_equal(situ_view_check(&msg, record), SITU_ERR_STALE);
	assert_int_equal(situ_view_check(&msg, view), SITU_ERR_STALE);
#else
	/* Compiled out in a release build, which is why the checked build is the
	 * one to develop against. */
	assert_int_equal(situ_view_check(&msg, record), SITU_OK);
#endif
}

static void test_reacquiring_after_a_shift_is_valid_again(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t record;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf);
	situ_Message_hdr_length_set(&msg, view, 8);

	/* Take the view again and it is live: the generation is what makes the
	 * difference, not the bytes. */
	assert_int_equal(situ_Message_view(&msg, 0, VECTOR_LEN, &view), SITU_OK);
	assert_int_equal(situ_view_check(&msg, view), SITU_OK);

	assert_int_equal(situ_Message_recs_at(view, 0, &record), SITU_OK);
	assert_int_equal(situ_view_check(&msg, record), SITU_OK);
	assert_ptr_equal(record.base, buf + 19);
}

static void test_short_buffer_is_refused(void **state)
{
	uint8_t buf[4];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	situ_msg_init(&msg, buf, sizeof(buf));
	assert_int_equal(situ_Message_view(&msg, 0, (uint32_t)sizeof(buf), &view),
	                 SITU_ERR_BOUNDS);
}

static void test_element_past_the_end_is_refused(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t record;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf);

	/* Acquiring the element view is the bounds check, and it holds even for an
	 * index the record count would allow. */
	assert_int_equal(situ_Message_recs_at(view, 100, &record), SITU_ERR_BOUNDS);
}

int main(void)
{
	const struct CMUnitTest tests[] = {
		cmocka_unit_test(test_static_prefix_is_still_static),
		cmocka_unit_test(test_dynamic_offset_is_resolved_from_the_data),
		cmocka_unit_test(test_frame_elements_are_static_once_the_base_is_found),
		cmocka_unit_test(test_element_mutation_is_in_place),
		cmocka_unit_test(test_trailer_runs_to_the_end_of_the_view),
		cmocka_unit_test(test_length_write_shifts_everything_after_it),
		cmocka_unit_test(test_length_write_increments_the_generation),
		cmocka_unit_test(test_stale_view_is_caught_when_checked),
		cmocka_unit_test(test_reacquiring_after_a_shift_is_valid_again),
		cmocka_unit_test(test_short_buffer_is_refused),
		cmocka_unit_test(test_element_past_the_end_is_refused),
	};

	return cmocka_run_group_tests(tests, NULL, NULL);
}
