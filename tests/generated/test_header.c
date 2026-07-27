/* test_header.c -- cmocka tests over the code generated from example 5.1.
 *
 * The header this exercises is generated at build time by `situc build`, so
 * these tests fail if the codegen changes shape as well as if it changes
 * behaviour.
 *
 * The round-trip test is the one that matters: parse a hex vector through the
 * generated getters, write every value back through the setters, and require
 * the buffer to come out byte-identical. That is the only reliable way to know
 * a layout change broke the wire format (project.md section 20.3).
 */

#include <setjmp.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <cmocka.h>

#include "header.h"

/* A hand-assembled header, byte by byte:
 *
 *   00        version  = 1
 *   01        type     = data (2)
 *   02        flags    = urgent|ack|priority=5, reserved 0
 *                        msb_first: 1 1 101 000 = 0xE8
 *   03..04    length   = 0x05DC (1500), big endian
 *   05..08    seq      = 0xDEADBEEF, big endian
 */
static const uint8_t VECTOR[SITU_HEADER_SIZE_FIXED] = {
	0x01, 0x02, 0xE8, 0x05, 0xDC, 0xDE, 0xAD, 0xBE, 0xEF,
};

static situ_view_t view_of(situ_msg_t *msg, uint8_t *buf, uint32_t size)
{
	situ_view_t view;

	situ_msg_init(msg, buf, size);
	assert_int_equal(situ_header_view(msg, 0, &view), SITU_OK);
	return view;
}

static void test_size_constants(void **state)
{
	(void)state;
	/* 1 + 1 + 1 + 2 + 4. project.md example 5.1 prints 10, which is off by
	 * one: reaching it would need a padding byte and section 8.4 inserts
	 * none. */
	assert_int_equal(SITU_HEADER_SIZE_FIXED, 9);
	assert_int_equal(SITU_HEADER_SIZE_MIN, 9);
	assert_int_equal(SITU_HEADER_SIZE_MAX, 9);
	assert_int_equal(SITU_FLAGS_SIZE_FIXED, 1);
}

static void test_scalar_getters(void **state)
{
	uint8_t buf[SITU_HEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf, (uint32_t)sizeof(buf));

	assert_int_equal(situ_header_version_get(view), 1);
	assert_int_equal(situ_header_type_get(view), SITU_MSG_TYPE_DATA);
	assert_int_equal(situ_header_length_get(view), 1500);
	assert_int_equal(situ_header_seq_get(view), 0xDEADBEEFu);
}

static void test_big_endian_is_decoded_not_aliased(void **state)
{
	uint8_t buf[SITU_HEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf, (uint32_t)sizeof(buf));

	/* The bytes are 05 DC; the value is 1500 on every host. A cast through a
	 * uint16_t pointer would give 0xDC05 on a little-endian machine, which is
	 * exactly why no pointer accessor is generated for this field. */
	assert_int_equal(buf[3], 0x05);
	assert_int_equal(buf[4], 0xDC);
	assert_int_equal(situ_header_length_get(view), 1500);
}

static void test_bit_fields_through_a_sub_view(void **state)
{
	uint8_t buf[SITU_HEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t flags;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf, (uint32_t)sizeof(buf));

	assert_int_equal(situ_header_flags_view(view, &flags), SITU_OK);
	assert_ptr_equal(flags.base, buf + 2);

	/* 0xE8 = 1110 1000, msb_first: urgent=1, ack=1, priority=101b, rsvd=0 */
	assert_int_equal(situ_flags_urgent_get(flags), 1);
	assert_int_equal(situ_flags_ack_get(flags), 1);
	assert_int_equal(situ_flags_priority_get(flags), 5);
}

static void test_bit_field_write_touches_only_its_own_bits(void **state)
{
	uint8_t buf[SITU_HEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t flags;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf, (uint32_t)sizeof(buf));
	assert_int_equal(situ_header_flags_view(view, &flags), SITU_OK);

	situ_flags_priority_set(flags, 2);

	/* 1 1 010 000 = 0xD0. The neighbouring fields and the reserved bits are
	 * unchanged, which is what a read-modify-write has to guarantee. */
	assert_int_equal(buf[2], 0xD0);
	assert_int_equal(situ_flags_urgent_get(flags), 1);
	assert_int_equal(situ_flags_ack_get(flags), 1);
	assert_int_equal(situ_flags_priority_get(flags), 2);
}

static void test_round_trip_is_byte_identical(void **state)
{
	uint8_t buf[SITU_HEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t flags;

	uint8_t version;
	situ_msg_type_t type;
	uint16_t length;
	uint32_t seq;
	uint8_t urgent;
	uint8_t ack;
	uint8_t priority;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf, (uint32_t)sizeof(buf));
	assert_int_equal(situ_header_flags_view(view, &flags), SITU_OK);

	version  = situ_header_version_get(view);
	type     = situ_header_type_get(view);
	length   = situ_header_length_get(view);
	seq      = situ_header_seq_get(view);
	urgent   = situ_flags_urgent_get(flags);
	ack      = situ_flags_ack_get(flags);
	priority = situ_flags_priority_get(flags);

	/* Scribble, then write every value back. */
	memset(buf, 0xA5, sizeof(buf));

	situ_header_version_set(view, version);
	situ_header_type_set(view, type);
	situ_header_length_set(view, length);
	situ_header_seq_set(view, seq);
	situ_flags_urgent_set(flags, urgent);
	situ_flags_ack_set(flags, ack);
	situ_flags_priority_set(flags, priority);

	/* The reserved bits were scribbled over and no accessor can restore them,
	 * which is the point of them being reserved: they carry no information.
	 * Zero them the way a writer would and the buffer must match exactly. */
	buf[2] &= 0xF8u;

	assert_memory_equal(buf, VECTOR, sizeof(buf));
}

static void test_validate_accepts_the_vector(void **state)
{
	uint8_t buf[SITU_HEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t flags;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf, (uint32_t)sizeof(buf));

	assert_int_equal(situ_header_validate(view), SITU_OK);
	assert_int_equal(situ_header_flags_view(view, &flags), SITU_OK);
	assert_int_equal(situ_flags_validate(flags), SITU_OK);
}

static void test_validate_rejects_a_wrong_must_eq(void **state)
{
	uint8_t buf[SITU_HEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf, (uint32_t)sizeof(buf));

	situ_header_version_set(view, 2);
	assert_int_equal(situ_header_validate(view), SITU_ERR_CONSTRAINT);
}

static void test_validate_rejects_a_length_over_max(void **state)
{
	uint8_t buf[SITU_HEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf, (uint32_t)sizeof(buf));

	situ_header_length_set(view, 1501);
	assert_int_equal(situ_header_validate(view), SITU_ERR_CONSTRAINT);
}

static void test_validate_rejects_dirty_reserved_bits(void **state)
{
	uint8_t buf[SITU_HEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;
	situ_view_t flags;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf, (uint32_t)sizeof(buf));
	assert_int_equal(situ_header_flags_view(view, &flags), SITU_OK);

	/* Section 14.5: every ignored bit is a malleability surface, so reserved
	 * bits are rejected on parse rather than preserved. */
	buf[2] |= 0x01u;
	assert_int_equal(situ_flags_validate(flags), SITU_ERR_CONSTRAINT);
}

static void test_view_refuses_a_short_buffer(void **state)
{
	uint8_t buf[SITU_HEADER_SIZE_FIXED - 1];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	situ_msg_init(&msg, buf, (uint32_t)sizeof(buf));
	assert_int_equal(situ_header_view(&msg, 0, &view), SITU_ERR_BOUNDS);
}

static void test_pointer_accessor_for_byte_fields(void **state)
{
	uint8_t buf[SITU_HEADER_SIZE_FIXED];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, VECTOR, sizeof(buf));
	view = view_of(&msg, buf, (uint32_t)sizeof(buf));

	/* version is MemoryIdentical, so a pointer is safe and is offered. */
	assert_ptr_equal(situ_header_version_ptr(view), buf);
	*situ_header_version_ptr(view) = 7;
	assert_int_equal(situ_header_version_get(view), 7);
}

int main(void)
{
	const struct CMUnitTest tests[] = {
		cmocka_unit_test(test_size_constants),
		cmocka_unit_test(test_scalar_getters),
		cmocka_unit_test(test_big_endian_is_decoded_not_aliased),
		cmocka_unit_test(test_bit_fields_through_a_sub_view),
		cmocka_unit_test(test_bit_field_write_touches_only_its_own_bits),
		cmocka_unit_test(test_round_trip_is_byte_identical),
		cmocka_unit_test(test_validate_accepts_the_vector),
		cmocka_unit_test(test_validate_rejects_a_wrong_must_eq),
		cmocka_unit_test(test_validate_rejects_a_length_over_max),
		cmocka_unit_test(test_validate_rejects_dirty_reserved_bits),
		cmocka_unit_test(test_view_refuses_a_short_buffer),
		cmocka_unit_test(test_pointer_accessor_for_byte_fields),
	};

	return cmocka_run_group_tests(tests, NULL, NULL);
}
