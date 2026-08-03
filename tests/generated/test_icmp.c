/* test_icmp.c -- compute a checksum the kernel already computed (26.39).
 *
 * `examples/icmp` declares its checksum with `[self_as = 0]`: it covers the
 * whole message *including its own two bytes*, which the algorithm takes as
 * zero while it runs (RFC 1071). situ does not compute it -- a signature says
 * what a transform does, never how (13.1) -- so what the generated code
 * contributes is the two things only the compiler knows:
 *
 *   - `..._covered()`, the span the sum runs over, and
 *   - `..._self_span()` with `..._SELF_AS`, where inside that span the
 *     checksum's own bytes are and what they read as instead.
 *
 * This is the check that those two are right, and it is the only kind that can
 * be: a caller writes RFC 1071 over what the compiler hands out, and the answer
 * has to be the one already on the wire. A span off by two bytes, or a hole in
 * the wrong place, produces a plausible number that is not that one.
 *
 * The bytes are not laid out here. They are an echo *reply* read off a
 * `SOCK_DGRAM`/`IPPROTO_ICMP` socket on this machine -- so the checksum in
 * them was computed by the kernel's ICMP stack, which is the independent
 * implementation this file is checked against:
 *
 *   fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_ICMP);
 *   sendto(fd, {8, 0, 0, 0, 0, 0, 0, 1}, 8, 0, &loopback, sizeof loopback);
 *   recv(fd, buf, sizeof buf, 0);
 *
 * `id` is the port the kernel assigned that socket rather than anything this
 * test chose, which is why it is 5 and not 1.
 */

#include <setjmp.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <cmocka.h>

#include "icmp.h"

/* type 0 (echo reply), code 0, checksum FFF9, id 0005, sequence 0001. */
static const uint8_t REPLY[] = {
	0x00, 0x00, 0xFF, 0xF9, 0x00, 0x05, 0x00, 0x01,
};

/* RFC 1071, over the span situ names, with the hole situ names taken as the
 * value situ names. Nothing here knows where the checksum field is. */
static uint16_t ones_complement(situ_view_t view)
{
	uint32_t at = 0, len = 0, hole = 0, held = 0, sum = 0, i;

	assert_int_equal(situ_icmp_message_checksum_covered(view, &at, &len),
	                 SITU_OK);
	assert_int_equal(situ_icmp_message_checksum_self_span(view, &hole, &held),
	                 SITU_OK);

	for (i = 0; i + 1u < len; i += 2u) {
		const uint32_t a  = at + i;
		const uint8_t  hi = (a >= hole && a < hole + held)
			? SITU_ICMP_MESSAGE_CHECKSUM_SELF_AS : view.base[a];
		const uint8_t  lo = (a + 1u >= hole && a + 1u < hole + held)
			? SITU_ICMP_MESSAGE_CHECKSUM_SELF_AS : view.base[a + 1u];

		sum += ((uint32_t)hi << 8) | (uint32_t)lo;
	}
	while (sum >> 16) {
		sum = (sum & 0xFFFFu) + (sum >> 16);
	}
	return (uint16_t)~sum;
}

static void open_reply(situ_msg_t *msg, situ_view_t *view, uint8_t *buf,
		uint32_t length)
{
	situ_msg_init(msg, buf, length);
	assert_int_equal(situ_icmp_message_view(msg, 0, view), SITU_OK);
}

static void test_a_real_reply_validates(void **state)
{
	uint8_t     buf[sizeof(REPLY)];
	situ_msg_t  msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, REPLY, sizeof(buf));
	open_reply(&msg, &view, buf, (uint32_t)sizeof(buf));

	assert_int_equal(situ_icmp_message_validate(view), SITU_OK);
	assert_int_equal(situ_icmp_message_type_get(view), 0);
}

/* The claim `[self_as = 0]` makes, against a number this test did not
 * compute. */
static void test_the_checksum_is_the_one_on_the_wire(void **state)
{
	uint8_t     buf[sizeof(REPLY)];
	situ_msg_t  msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, REPLY, sizeof(buf));
	open_reply(&msg, &view, buf, (uint32_t)sizeof(buf));

	assert_int_equal(ones_complement(view),
	                 ((uint16_t)REPLY[2] << 8) | REPLY[3]);
}

/* The hole has to be the checksum's own bytes and nothing else. Filling them
 * with anything at all must not change the answer -- which is what "taken as
 * zero" means, and what a span that skipped the wrong two bytes would fail. */
static void test_the_hole_is_where_the_checksum_is(void **state)
{
	uint8_t     buf[sizeof(REPLY)];
	situ_msg_t  msg;
	situ_view_t view;
	uint16_t    before, after;

	(void)state;
	memcpy(buf, REPLY, sizeof(buf));
	open_reply(&msg, &view, buf, (uint32_t)sizeof(buf));
	before = ones_complement(view);

	buf[2] = 0xAB;
	buf[3] = 0xCD;
	after = ones_complement(view);

	assert_int_equal(before, after);
}

/* And the other half of 14.2: writing a covered byte says so. `sequence` is
 * inside the coverage, so its setter takes the message and marks the bit. */
static void test_a_covered_write_marks_the_checksum_stale(void **state)
{
	uint8_t     buf[sizeof(REPLY)];
	situ_msg_t  msg;
	situ_view_t view;

	(void)state;
	memcpy(buf, REPLY, sizeof(buf));
	open_reply(&msg, &view, buf, (uint32_t)sizeof(buf));

	/* The arm's own setter on the enclosing view: the field is inside the
	 * coverage, so it takes the message and marks the bit. There is no plain
	 * setter for it, which is the compile-time half of the same statement. */
	assert_false(situ_icmp_message_checksum_is_dirty(&msg));
	situ_icmp_message_body_reply_sequence_set(&msg, view, 9);
	assert_true(situ_icmp_message_checksum_is_dirty(&msg));

	situ_icmp_message_checksum_finalize(&msg);
	assert_false(situ_icmp_message_checksum_is_dirty(&msg));
}

int main(void)
{
	const struct CMUnitTest tests[] = {
		cmocka_unit_test(test_a_real_reply_validates),
		cmocka_unit_test(test_the_checksum_is_the_one_on_the_wire),
		cmocka_unit_test(test_the_hole_is_where_the_checksum_is),
		cmocka_unit_test(test_a_covered_write_marks_the_checksum_stale),
	};

	return cmocka_run_group_tests(tests, NULL, NULL);
}
