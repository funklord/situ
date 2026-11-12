/* test_message.c -- islands of staticness (project.md example 5.2).
 *
 * The phase 5 acceptance criteria that need running code: a stale view is
 * caught under SITU_CHECKED, and mutating a preceding length field increments
 * the generation.
 *
 * The point of the schema is that `record` is fully static once its base is
 * resolved, even though the base itself is not known until parse time. These
 * tests exercise exactly that: one runtime resolution, then constant offsets.
 */

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "situ_check.h"

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
	situ_check_eq(situ_message_view(msg, 0, VECTOR_LEN, &view), SITU_OK);
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
	situ_check_eq(situ_message_hdr_view(view, &hdr), SITU_OK);
	situ_check_ptr_eq(hdr.base, buf);
	situ_check_eq(situ_header_seq_get(hdr), 0x11223344u);
	situ_check_eq(situ_header_length_get(hdr), 4);
	situ_check_eq(situ_header_rec_count_get(hdr), 2);
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
	situ_check_eq(situ_message_opts_len(view), 4);
	situ_check_ptr_eq(situ_message_opts_ptr(view), buf + 11);
	situ_check_eq(situ_message_recs_offset(view), 15);
	situ_check_eq(situ_message_recs_count(view), 2);
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
	 * The accessors are record's own: a dynamically positioned static struct
	 * gets its static capabilities back (section 12.2). */
	situ_check_eq(situ_message_recs_at(view, 0, &record), SITU_OK);
	situ_check_ptr_eq(record.base, buf + 15);
	situ_check_eq(situ_record_id_get(record), 1);
	situ_check_eq(situ_record_kind_get(record), 2);
	situ_check_eq(situ_record_value_get(record), 3);

	situ_check_eq(situ_message_recs_at(view, 1, &record), SITU_OK);
	situ_check_ptr_eq(record.base, buf + 23);
	situ_check_eq(situ_record_id_get(record), 4);
	situ_check_eq(situ_record_value_get(record), 6);
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

	/* `require in_place(message.recs[].value)` says this moves nothing. */
	situ_check_eq(situ_message_recs_at(view, 1, &record), SITU_OK);
	situ_record_value_set(record, 0x0BAD);

	situ_check_eq(situ_record_value_get(record), 0x0BAD);
	situ_check_eq(buf[29], 0x0B);
	situ_check_eq(buf[30], 0xAD);
	/* Nothing else moved. */
	situ_check_bytes(buf, VECTOR, 29);
	situ_check_bytes(buf + 31, VECTOR + 31, 3);
}

static void test_trailer_runs_to_the_end_of_the_view(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf);

	situ_check_eq(situ_message_trailer_offset(view), 31);
	situ_check_eq(situ_message_trailer_len(view), 3);
	situ_check_ptr_eq(situ_message_trailer_ptr(view), buf + 31);
}

static void test_length_write_shifts_everything_after_it(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf);

	situ_check_eq(situ_message_recs_offset(view), 15);

	/* Growing the options moves the records. The schema said this would
	 * happen -- `mutate=Shifting` on opts -- and here it is. */
	situ_message_hdr_length_set(&msg, view, 8);

	/* Re-acquired, because that write moved what follows and every view
	 * taken before it is now stale (12.3). This read through the same
	 * view, which worked only while nothing checked the generation: the
	 * accessors reach their bytes through `situ_base` now, so a checked
	 * build traps here rather than answering. `test_reacquiring_after_a_
	 * shift_is_valid_again` below is the same rule stated on purpose. */
	view = view_of(&msg, buf);
	situ_check_eq(situ_message_recs_offset(view), 19);
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

	situ_message_hdr_length_set(&msg, view, 8);

	situ_check_ne(msg.generation, before);
	situ_check_eq(view.generation, before);
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

	situ_check_eq(situ_message_recs_at(view, 0, &record), SITU_OK);
	situ_check_eq(situ_view_check(record), SITU_OK);

	/* The element view was taken before the shift, so it now points at the
	 * wrong bytes. This is the bug class section 12.3 exists to catch. */
	situ_message_hdr_length_set(&msg, view, 8);

#ifdef SITU_CHECKED
	situ_check_eq(situ_view_check(record), SITU_ERR_STALE);
	situ_check_eq(situ_view_check(view), SITU_ERR_STALE);
#else
	/* Compiled out in a release build, which is why the checked build is the
	 * one to develop against. */
	situ_check_eq(situ_view_check(record), SITU_OK);
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
	situ_message_hdr_length_set(&msg, view, 8);

	/* Take the view again and it is live: the generation is what makes the
	 * difference, not the bytes. */
	situ_check_eq(situ_message_view(&msg, 0, VECTOR_LEN, &view), SITU_OK);
	situ_check_eq(situ_view_check(view), SITU_OK);

	situ_check_eq(situ_message_recs_at(view, 0, &record), SITU_OK);
	situ_check_eq(situ_view_check(record), SITU_OK);
	situ_check_ptr_eq(record.base, buf + 19);
}

static void test_short_buffer_is_refused(void **state)
{
	uint8_t buf[4];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	situ_msg_init(&msg, buf, sizeof(buf));
	situ_check_eq(situ_message_view(&msg, 0, (uint32_t)sizeof(buf), &view),
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
	situ_check_eq(situ_message_recs_at(view, 100, &record), SITU_ERR_BOUNDS);
}

int main(void)
{
	const struct situ_check_case tests[] = {
		situ_check_case(test_static_prefix_is_still_static),
		situ_check_case(test_dynamic_offset_is_resolved_from_the_data),
		situ_check_case(test_frame_elements_are_static_once_the_base_is_found),
		situ_check_case(test_element_mutation_is_in_place),
		situ_check_case(test_trailer_runs_to_the_end_of_the_view),
		situ_check_case(test_length_write_shifts_everything_after_it),
		situ_check_case(test_length_write_increments_the_generation),
		situ_check_case(test_stale_view_is_caught_when_checked),
		situ_check_case(test_reacquiring_after_a_shift_is_valid_again),
		situ_check_case(test_short_buffer_is_refused),
		situ_check_case(test_element_past_the_end_is_refused),
	};

	return situ_check_run(tests, sizeof tests / sizeof tests[0], "test_message");
}
