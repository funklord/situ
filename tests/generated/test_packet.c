/* test_packet.c -- the cryptographic model at run time (project.md 5.3).
 *
 * The phase 8 acceptance criteria that need running code:
 *
 *   - the interior of a sealed region is unreachable before verification. The
 *     half of that which cannot be tested at run time -- that reaching it does
 *     not compile -- is checked by the compile-refusal test in
 *     tests/unit/test_codegen_c.py, because a test that fails to build is not
 *     something cmocka can report.
 *   - mutating a covered field marks the tag dirty, and the message refuses to
 *     be transmittable until finalize clears it.
 *   - a field outside coverage stays freely mutable, which is the design
 *     pressure the whole chapter is about.
 *
 * The algorithm is not here and is not the compiler's business: a signature
 * says what a transform does, never how (section 13.1). What is tested is the
 * bookkeeping around it, which is what situ generates.
 */

#include <setjmp.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <cmocka.h>

#include "packet.h"

/*   00      hop         = 7
 *   01..03  reserved    = 0
 *   04      version     = 1
 *   05      type        = data
 *   06..07  length      = 4        (sealed body)
 *   08..0B  seq         = 0x11223344
 *   0C..17  nonce
 *   18..19  inner_kind  = 9
 *   1A..1D  inner_seq   = 5
 *   1E..2D  session_key
 *   2E..31  body        = 4 bytes
 *   32..41  tag
 */
#define VECTOR_LEN  66u
#define TAG_OFFSET  50u
#define KEY_OFFSET  30u

static const uint8_t VECTOR[VECTOR_LEN] = {
	0x07, 0x00, 0x00, 0x00,
	0x01, 0x02, 0x00, 0x04, 0x11, 0x22, 0x33, 0x44,
	0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xAB,
	0x00, 0x09, 0x00, 0x00, 0x00, 0x05,
	0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,
	0xC8, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD, 0xCE, 0xCF,
	0xDE, 0xAD, 0xBE, 0xEF,
	0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17,
	0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F,
};

static situ_view_t view_of(situ_msg_t *msg, uint8_t *buf)
{
	situ_view_t view;

	memcpy(buf, VECTOR, VECTOR_LEN);
	situ_msg_init(msg, buf, VECTOR_LEN);
	assert_int_equal(situ_packet_view(msg, 0, VECTOR_LEN, &view), SITU_OK);
	return view;
}

/* -- the stage gate (14.3) ------------------------------------------------ */

static void test_the_gate_refuses_before_verification(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view = view_of(&msg, buf);
	situ_packet_sealed_t gate;

	(void)state;

	memset(&gate, 0, sizeof gate);
	assert_int_equal(situ_packet_sealed_open(view, 0, &gate), SITU_ERR_TAG);
	assert_null(gate.view.base);
}

static void test_the_gate_opens_once_the_tag_verifies(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view = view_of(&msg, buf);
	situ_packet_sealed_t gate;

	(void)state;

	assert_int_equal(situ_packet_sealed_open(view, 1, &gate), SITU_OK);
	assert_int_equal(situ_packet_sealed_inner_kind_get(gate), 9u);
	assert_int_equal(situ_packet_sealed_inner_seq_get(gate), 5u);
}

static void test_the_gate_costs_nothing_at_run_time(void **state)
{
	/* It wraps the frame view rather than a sub-view, so an interior field is
	 * the same constant offset it has everywhere else. */
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view = view_of(&msg, buf);
	situ_packet_sealed_t gate;

	(void)state;

	assert_int_equal(situ_packet_sealed_open(view, 1, &gate), SITU_OK);
	assert_ptr_equal(gate.view.base, view.base);
	assert_int_equal(situ_packet_sealed_body_len(gate), 4u);
	assert_ptr_equal(situ_packet_sealed_body_ptr(gate), view.base + 46u);
}

/* -- tag coverage and the dirty bit (14.2) -------------------------------- */

static void test_a_fresh_message_is_transmittable(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;

	(void)state;
	(void)view_of(&msg, buf);

	assert_int_equal(situ_msg_transmittable(&msg), SITU_OK);
	assert_false(situ_packet_tag_is_dirty(&msg));
}

static void test_writing_a_covered_field_marks_the_tag_dirty(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view = view_of(&msg, buf);

	(void)state;

	situ_packet_hdr_seq_set(&msg, view, 0xAABBCCDDu);

	assert_true(situ_packet_tag_is_dirty(&msg));
	assert_int_equal(situ_msg_transmittable(&msg), SITU_ERR_TAG);
}

static void test_finalize_clears_the_dirty_bit(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view = view_of(&msg, buf);
	uint32_t offset  = 0;
	uint32_t len     = 0;

	(void)state;

	situ_packet_hdr_seq_set(&msg, view, 0xAABBCCDDu);
	assert_int_equal(situ_msg_transmittable(&msg), SITU_ERR_TAG);

	/* What a caller does between the two: recompute over the covered span and
	 * write the result. The algorithm is theirs; the span is the compiler's. */
	assert_int_equal(situ_packet_tag_covered(view, &offset, &len), SITU_OK);
	memset(situ_packet_tag_ptr(view), 0x5A, SITU_PACKET_TAG_COUNT);

	situ_packet_tag_finalize(&msg);

	assert_false(situ_packet_tag_is_dirty(&msg));
	assert_int_equal(situ_msg_transmittable(&msg), SITU_OK);
}

static void test_the_covered_span_is_the_authenticated_and_sealed_bytes(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view = view_of(&msg, buf);
	uint32_t offset  = 0;
	uint32_t len     = 0;

	(void)state;

	assert_int_equal(situ_packet_tag_covered(view, &offset, &len), SITU_OK);

	/* From the start of the authenticated block to the end of the sealed
	 * region: the hop counter in front is outside, and so is the tag itself. */
	assert_int_equal(offset, 4u);
	assert_int_equal(len, TAG_OFFSET - 4u);
	assert_ptr_equal(situ_packet_tag_ptr(view), view.base + TAG_OFFSET);
}

static void test_the_covered_span_follows_the_length_field(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view = view_of(&msg, buf);
	uint32_t offset  = 0;
	uint32_t len     = 0;

	(void)state;

	/* A shorter body moves the tag and shrinks what it covers. The length
	 * field is itself covered, so writing it marks the tag dirty -- which is
	 * exactly right: the bytes it authenticates just changed. */
	situ_packet_hdr_length_set(&msg, view, 2u);

	assert_true(situ_packet_tag_is_dirty(&msg));
	assert_int_equal(situ_packet_tag_covered(view, &offset, &len), SITU_OK);
	assert_int_equal(len, (TAG_OFFSET - 2u) - 4u);
}

static void test_an_uncovered_field_stays_freely_mutable(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view = view_of(&msg, buf);

	(void)state;

	/* No message argument, and nothing goes stale: a hop counter is rewritten
	 * at every forwarder, which is why it is outside coverage. */
	situ_packet_hop_set(view, 8u);

	assert_int_equal(situ_packet_hop_get(view), 8u);
	assert_false(situ_packet_tag_is_dirty(&msg));
	assert_int_equal(situ_msg_transmittable(&msg), SITU_OK);
}

static void test_writing_through_the_gate_marks_the_tag_too(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view = view_of(&msg, buf);
	situ_packet_sealed_t gate;

	(void)state;

	assert_int_equal(situ_packet_sealed_open(view, 1, &gate), SITU_OK);
	situ_packet_sealed_inner_seq_set(&msg, gate, 0x99u);

	assert_int_equal(situ_packet_sealed_inner_seq_get(gate), 0x99u);
	assert_true(situ_packet_tag_is_dirty(&msg));
}

/* -- secrets (14.6) ------------------------------------------------------- */

static void test_zeroize_erases_secret_bytes(void **state)
{
	uint8_t buf[VECTOR_LEN];
	situ_msg_t msg;
	situ_view_t view = view_of(&msg, buf);
	situ_packet_sealed_t gate;
	uint32_t i;

	(void)state;

	assert_int_equal(situ_packet_sealed_open(view, 1, &gate), SITU_OK);
	assert_int_equal(situ_packet_sealed_session_key_ptr(gate)[0], 0xC0u);

	situ_packet_sealed_session_key_zeroize(gate);

	for (i = 0; i < SITU_PACKET_SEALED_SESSION_KEY_COUNT; i++) {
		assert_int_equal(situ_packet_sealed_session_key_ptr(gate)[i], 0u);
	}

	/* Only the secret bytes, and nothing either side of them. */
	assert_int_equal(situ_packet_sealed_inner_seq_get(gate), 5u);
	assert_int_equal(buf[KEY_OFFSET - 1u], 0x05u);
	assert_int_equal(buf[KEY_OFFSET + 16u], 0xDEu);
}

int main(void)
{
	const struct CMUnitTest tests[] = {
		cmocka_unit_test(test_the_gate_refuses_before_verification),
		cmocka_unit_test(test_the_gate_opens_once_the_tag_verifies),
		cmocka_unit_test(test_the_gate_costs_nothing_at_run_time),
		cmocka_unit_test(test_a_fresh_message_is_transmittable),
		cmocka_unit_test(test_writing_a_covered_field_marks_the_tag_dirty),
		cmocka_unit_test(test_finalize_clears_the_dirty_bit),
		cmocka_unit_test(test_the_covered_span_is_the_authenticated_and_sealed_bytes),
		cmocka_unit_test(test_the_covered_span_follows_the_length_field),
		cmocka_unit_test(test_an_uncovered_field_stays_freely_mutable),
		cmocka_unit_test(test_writing_through_the_gate_marks_the_tag_too),
		cmocka_unit_test(test_zeroize_erases_secret_bytes),
	};

	return cmocka_run_group_tests(tests, NULL, NULL);
}
