/* test_protobuf.c -- parse real protobuf-encoded bytes (section 9.7).
 *
 * The other half of the conformance gate. The capability half is checked in
 * tests/unit/test_protobuf.py; this one checks that situ's description of the
 * wire format actually matches the wire format, using vectors produced by
 * protoc rather than by situ.
 *
 * A description that agrees only with its own compiler has demonstrated
 * nothing, which is why every byte array below came out of
 *
 *   printf '...' | protoc --encode=User examples/protobuf/user.proto
 *
 * and is reproduced verbatim. examples/protobuf/vectors.txt records the input
 * each one was generated from.
 */

#include <setjmp.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <cmocka.h>

#include "situ.h"

#include "protobuf.h"

/* The tlv half of this file walks items through the accessors situc generated
 * from examples/protobuf/protobuf.situ. It used to walk them through a cursor
 * hand-written in the runtime, with `tag >> 3` and protobuf's four wire types
 * baked into it, and dispatch on field numbers this file defined itself --
 * three descriptions of one wire format, of which the schema's was the one
 * nobody read. There is one now, and these tests are what hold it to protoc.
 */
typedef situ_proto_message_fields_item_t item_t;

/* protoc --encode=User <<< 'user_id: 150; username: "situ"; score: 1.5' */
static const uint8_t ALL_THREE[] = {
	0x08, 0x96, 0x01,				/* 1, varint, 150	*/
	0x12, 0x04, 0x73, 0x69, 0x74, 0x75,		/* 2, bytes, "situ"	*/
	0x1D, 0x00, 0x00, 0xC0, 0x3F,			/* 3, fixed32, 1.5f	*/
};

/* protoc --encode=User <<< 'user_id: 0' */
static const uint8_t ZERO[] = { 0x08, 0x00 };

/* protoc --encode=User <<< 'user_id: 18446744073709551615' */
static const uint8_t MAX_U64[] = {
	0x08, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01,
};

static situ_view_t view_over(situ_msg_t *msg, const uint8_t *data, uint32_t len,
		uint8_t *scratch)
{
	situ_view_t view;

	memcpy(scratch, data, len);
	situ_msg_init(msg, scratch, len);
	assert_int_equal(situ_view_at(msg, 0, len, &view), SITU_OK);
	return view;
}

/* -- varints --------------------------------------------------------------- */

static void test_varint_decodes_protoc_output(void **state)
{
	uint64_t value = 0;

	(void)state;
	/* 0x96 0x01 is 150: 0x16 low group, 0x01 high group, shifted seven. */
	assert_int_equal(situ_varint_get(ALL_THREE + 1, 2, 10, &value), 2);
	assert_int_equal(value, 150);
}

static void test_varint_decodes_zero_and_max(void **state)
{
	uint64_t value = 1;

	(void)state;
	assert_int_equal(situ_varint_get(ZERO + 1, 1, 10, &value), 1);
	assert_int_equal(value, 0);

	assert_int_equal(situ_varint_get(MAX_U64 + 1, 10, 10, &value), 10);
	assert_true(value == UINT64_MAX);
}

static void test_varint_round_trips(void **state)
{
	static const uint64_t cases[] = {
		0, 1, 127, 128, 150, 300, 16383, 16384, UINT64_MAX,
	};
	uint8_t buf[10];
	size_t i;

	(void)state;
	for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		uint64_t back = 0;
		uint32_t n    = situ_varint_put(buf, sizeof(buf), cases[i]);

		assert_int_not_equal(n, 0);
		assert_int_equal(n, situ_varint_len(cases[i]));
		assert_int_equal(situ_varint_get(buf, n, 10, &back), n);
		assert_true(back == cases[i]);
	}
}

static void test_varint_encoding_matches_protoc(void **state)
{
	uint8_t buf[10];

	(void)state;
	/* Our minimal encoding of 150 must be the bytes protoc produced. */
	assert_int_equal(situ_varint_put(buf, sizeof(buf), 150), 2);
	assert_memory_equal(buf, ALL_THREE + 1, 2);
}

static void test_varint_refuses_a_truncated_value(void **state)
{
	uint64_t value = 0;
	static const uint8_t truncated[] = { 0x96 };	/* continuation bit set */

	(void)state;
	assert_int_equal(situ_varint_get(truncated, 1, 10, &value), 0);
}

static void test_varint_refuses_an_overlong_value(void **state)
{
	uint64_t value = 0;
	static const uint8_t overlong[] = {
		0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x00,
	};

	(void)state;
	assert_int_equal(situ_varint_get(overlong, sizeof(overlong), 10, &value), 0);
}

static void test_non_minimal_encodings_are_accepted(void **state)
{
	/* 150 written in three bytes rather than two. Protobuf accepts this, which
	 * is precisely why the format is not canonical -- and why the decoder must
	 * accept it while the capability map records the cost. */
	static const uint8_t padded[] = { 0x96, 0x81, 0x00 };
	uint64_t value = 0;

	(void)state;
	assert_int_equal(situ_varint_get(padded, sizeof(padded), 10, &value), 3);
	assert_int_equal(value, 150);
}

static void test_zigzag_round_trips(void **state)
{
	static const int64_t cases[] = { 0, -1, 1, -2, 2, INT64_MIN, INT64_MAX };
	size_t i;

	(void)state;
	for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		assert_true(situ_zigzag_decode(situ_zigzag_encode(cases[i])) == cases[i]);
	}

	/* protobuf's own mapping: 0 -> 0, -1 -> 1, 1 -> 2, -2 -> 3. */
	assert_int_equal(situ_zigzag_encode(0), 0);
	assert_int_equal(situ_zigzag_encode(-1), 1);
	assert_int_equal(situ_zigzag_encode(1), 2);
	assert_int_equal(situ_zigzag_encode(-2), 3);
}

/* -- tlv iteration --------------------------------------------------------- */

static void test_iterates_every_item(void **state)
{
	uint8_t scratch[sizeof(ALL_THREE)];
	situ_msg_t msg;
	situ_view_t view;
	(void)state;
	view = view_over(&msg, ALL_THREE, sizeof(ALL_THREE), scratch);

	assert_int_equal(situ_proto_message_fields_count(view), 3);
}

static void test_finds_the_fields_protoc_wrote(void **state)
{
	uint8_t scratch[sizeof(ALL_THREE)];
	situ_msg_t msg;
	situ_view_t view;
	item_t item;

	uint64_t user_id = 0;
	char username[8];

	(void)state;
	view = view_over(&msg, ALL_THREE, sizeof(ALL_THREE), scratch);

	/* One accessor per tag the schema names, each keyed on the part
	 * `tag_identity` picks out (decision 0023). */
	assert_int_equal(situ_proto_message_fields_user_id(view, &item), SITU_OK);
	assert_int_equal(item.wire, 0);
	assert_int_equal(situ_varint_get(view.base + item.value_at, item.value_len,
	                                 10, &user_id), item.value_len);
	assert_int_equal(user_id, 150);

	assert_int_equal(situ_proto_message_fields_username(view, &item), SITU_OK);
	assert_int_equal(item.wire, 2);
	assert_int_equal(item.value_len, 4);
	memcpy(username, view.base + item.value_at, item.value_len);
	username[item.value_len] = '\0';
	assert_string_equal(username, "situ");

	assert_int_equal(situ_proto_message_fields_score(view, &item), SITU_OK);
	assert_int_equal(item.wire, 5);
	/* protobuf fixed32 is little endian on the wire; 1.5f is 0x3FC00000. */
	assert_int_equal(situ_get_le32(view.base + item.value_at), 0x3FC00000u);
}

static void test_item_extents_match_the_wire(void **state)
{
	uint8_t scratch[sizeof(ALL_THREE)];
	situ_msg_t msg;
	situ_view_t view;
	item_t item;

	(void)state;
	view = view_over(&msg, ALL_THREE, sizeof(ALL_THREE), scratch);

	/* Each item's value sits exactly where protoc put it. */
	assert_int_equal(situ_proto_message_fields_first(view, &item), SITU_OK);
	assert_int_equal(item.value_at, 1);
	assert_int_equal(item.value_len, 2);

	assert_int_equal(situ_proto_message_fields_next(&item), SITU_OK);
	assert_int_equal(item.value_at, 5);
	assert_int_equal(item.value_len, 4);

	assert_int_equal(situ_proto_message_fields_next(&item), SITU_OK);
	assert_int_equal(item.value_at, 10);
	assert_int_equal(item.value_len, 4);

	assert_int_equal(situ_proto_message_fields_next(&item), SITU_ERR_BOUNDS);
}

static void test_same_size_item_mutation_is_in_place(void **state)
{
	uint8_t scratch[sizeof(ALL_THREE)];
	uint8_t expected[sizeof(ALL_THREE)];
	situ_msg_t msg;
	situ_view_t view;
	item_t item;

	(void)state;
	view = view_over(&msg, ALL_THREE, sizeof(ALL_THREE), scratch);
	memcpy(expected, ALL_THREE, sizeof(expected));

	assert_int_equal(situ_proto_message_fields_username(view, &item), SITU_OK);

	/* Four bytes for four bytes: `mutate = InPlaceSlack` says this stays
	 * put, and nothing around it moves. */
	memcpy(scratch + item.value_at, "SITU", 4);

	memcpy(expected + 5, "SITU", 4);
	assert_memory_equal(scratch, expected, sizeof(expected));
}

static void test_a_longer_value_would_not_fit(void **state)
{
	uint8_t scratch[sizeof(ALL_THREE)];
	situ_msg_t msg;
	situ_view_t view;
	item_t item;
	uint32_t trailing;
	uint32_t grown;

	(void)state;
	view = view_over(&msg, ALL_THREE, sizeof(ALL_THREE), scratch);

	assert_int_equal(situ_proto_message_fields_username(view, &item), SITU_OK);

	/* The other half of InPlaceSlack: a longer value does not fit where the
	 * old one was, so everything after it would have to move. Growing "situ"
	 * to five bytes needs one byte of slack at the end of the region, and this
	 * region is exactly full. */
	trailing = view.limit - (item.value_at + item.value_len);
	grown    = item.value_at + item.value_len + 1u + trailing;

	assert_int_equal(trailing, 5);		/* the fixed32 item */
	assert_true(grown > view.limit);
	assert_false(situ_in_bounds(view, 0, grown));
}

static void test_truncated_input_is_refused(void **state)
{
	uint8_t scratch[sizeof(ALL_THREE)];
	situ_msg_t msg;
	situ_view_t view;
	item_t item;
	situ_err_t err;

	(void)state;
	/* Everything but the last two bytes of the fixed32. */
	view = view_over(&msg, ALL_THREE, sizeof(ALL_THREE) - 2u, scratch);

	err = situ_proto_message_fields_first(view, &item);
	while (err == SITU_OK) {
		err = situ_proto_message_fields_next(&item);
	}

	assert_int_equal(err, SITU_ERR_BOUNDS);
}

static void test_a_group_wire_type_is_refused(void **state)
{
	/* Wire types 3 and 4 are groups, which situ does not describe: the schema
	 * says `default: error` and the decoder agrees. */
	static const uint8_t group[] = { 0x0B };	/* field 1, wire 3 */
	uint8_t scratch[sizeof(group)];
	situ_msg_t msg;
	situ_view_t view;
	item_t item;

	(void)state;
	view = view_over(&msg, group, sizeof(group), scratch);

	assert_int_equal(situ_proto_message_fields_first(view, &item),
	                 SITU_ERR_CONSTRAINT);
}

static void test_an_empty_region_yields_nothing(void **state)
{
	uint8_t scratch[1];
	situ_msg_t msg;
	situ_view_t view;
	item_t item;

	(void)state;
	situ_msg_init(&msg, scratch, 0);
	assert_int_equal(situ_view_at(&msg, 0, 0, &view), SITU_OK);

	assert_int_equal(situ_proto_message_fields_first(view, &item),
	                 SITU_ERR_BOUNDS);
}

int main(void)
{
	const struct CMUnitTest tests[] = {
		cmocka_unit_test(test_varint_decodes_protoc_output),
		cmocka_unit_test(test_varint_decodes_zero_and_max),
		cmocka_unit_test(test_varint_round_trips),
		cmocka_unit_test(test_varint_encoding_matches_protoc),
		cmocka_unit_test(test_varint_refuses_a_truncated_value),
		cmocka_unit_test(test_varint_refuses_an_overlong_value),
		cmocka_unit_test(test_non_minimal_encodings_are_accepted),
		cmocka_unit_test(test_zigzag_round_trips),
		cmocka_unit_test(test_iterates_every_item),
		cmocka_unit_test(test_finds_the_fields_protoc_wrote),
		cmocka_unit_test(test_item_extents_match_the_wire),
		cmocka_unit_test(test_same_size_item_mutation_is_in_place),
		cmocka_unit_test(test_a_longer_value_would_not_fit),
		cmocka_unit_test(test_truncated_input_is_refused),
		cmocka_unit_test(test_a_group_wire_type_is_refused),
		cmocka_unit_test(test_an_empty_region_yields_nothing),
	};

	return cmocka_run_group_tests(tests, NULL, NULL);
}
