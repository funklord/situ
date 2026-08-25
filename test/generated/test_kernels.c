/* test_kernels.c -- the derived implementations, against published vectors.
 *
 * Section 26.12 asks for "vectors from an independent reference implementation"
 * per family, and that is what these are: the COBS examples from Cheshire and
 * Baker, Hamming(7, 4)'s defining property that it corrects any single-bit
 * error, HDLC's rule that a zero follows five ones. None of them is a value
 * situ computed and then wrote down as its own expectation, which is the only
 * way to tell an implementation of COBS from an implementation of whatever this
 * file happens to do.
 *
 * The codecs under test come from std/kernels.situ, so they are the ones a user
 * gets rather than a fixture written to be easy on the generator.
 */

#include <setjmp.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <cmocka.h>

#include "kernels.h"

/* -- COBS (Cheshire and Baker) --------------------------------------------- */

static void test_cobs_matches_the_published_examples(void **state)
{
	static const struct {
		uint32_t len;
		uint8_t  in[8];
		uint32_t encoded;
		uint8_t  want[10];
	} cases[] = {
		{ 1, {0x00},                3, {0x01, 0x01, 0x00} },
		{ 2, {0x00, 0x00},          4, {0x01, 0x01, 0x01, 0x00} },
		{ 4, {0x11, 0x22, 0x00, 0x33},
			                        6, {0x03, 0x11, 0x22, 0x02, 0x33, 0x00} },
		{ 4, {0x11, 0x22, 0x33, 0x44},
			                        6, {0x05, 0x11, 0x22, 0x33, 0x44, 0x00} },
		{ 4, {0x11, 0x00, 0x00, 0x00},
			                        6, {0x02, 0x11, 0x01, 0x01, 0x01, 0x00} },
	};
	uint8_t  out[16];
	uint8_t  back[16];
	unsigned i;

	(void)state;

	for (i = 0; i < sizeof cases / sizeof cases[0]; i++) {
		uint32_t written = situ_cobs_encode(cases[i].in, cases[i].len, out);

		assert_int_equal(written, cases[i].encoded);
		assert_memory_equal(out, cases[i].want, written);

		assert_int_equal(situ_cobs_decode(out, written, back), cases[i].len);
		assert_memory_equal(back, cases[i].in, cases[i].len);
	}
}

static void test_cobs_spends_one_byte_per_254(void **state)
{
	/* The boundary the code exists to get right, and the promise its name
	 * makes: consistent overhead. A full group takes the code 0xFF with no
	 * implicit zero after it, so 254 bytes cost exactly two -- one code and one
	 * delimiter. Opening a second group at the end would spend a third. */
	uint8_t  in[254];
	uint8_t  out[300];
	uint8_t  back[300];
	unsigned i;

	(void)state;

	for (i = 0; i < 254; i++) {
		in[i] = (uint8_t)(i + 1);
	}

	assert_int_equal(situ_cobs_encode(in, 254, out), 256);
	assert_int_equal(out[0], 0xFF);
	assert_int_equal(out[255], 0x00);

	assert_int_equal(situ_cobs_decode(out, 256, back), 254);
	assert_memory_equal(back, in, 254);
}

/* -- Hamming(7, 4) --------------------------------------------------------- */

static void test_hamming_is_systematic(void **state)
{
	/* What `systematic` promises: the data bits sit verbatim at computable
	 * positions, so a reader that trusts the codeword takes them with no decode
	 * at all. Deriving that from the matrix rather than declaring it is the
	 * reason the linear-block kernel exists. */
	uint8_t nibble;

	(void)state;

	for (nibble = 0; nibble < 16; nibble++) {
		assert_int_equal(situ_hamming_7_4_encode(nibble) & 0x0Fu, nibble);
	}
}

static void test_hamming_corrects_any_single_bit_error(void **state)
{
	/* The defining property of the code, and an independent one: true of
	 * Hamming(7, 4) whoever implements it. */
	uint8_t nibble;
	uint8_t bit;

	(void)state;

	for (nibble = 0; nibble < 16; nibble++) {
		uint8_t word      = situ_hamming_7_4_encode(nibble);
		int     corrected = 1;

		assert_int_equal(situ_hamming_7_4_decode(word, &corrected), nibble);
		assert_int_equal(corrected, 0);

		for (bit = 0; bit < 7; bit++) {
			uint8_t damaged = (uint8_t)(word ^ (uint8_t)(1u << bit));

			corrected = 0;
			assert_int_equal(situ_hamming_7_4_decode(damaged, &corrected), nibble);
			assert_int_equal(corrected, 1);
		}
	}
}

/* -- scramblers ------------------------------------------------------------ */

static void test_the_additive_scrambler_is_its_own_inverse(void **state)
{
	/* Which is what feedback from the input buys, and why the derivation calls
	 * it seekable: the keystream does not depend on the data, so the register
	 * can be wound to any position. */
	static const uint8_t plain[5] = { 'h', 'e', 'l', 'l', 'o' };
	uint8_t coded[5];
	uint8_t back[5];

	(void)state;

	assert_int_equal(situ_scrambler_additive_encode(plain, 5, coded), 5);
	assert_memory_not_equal(coded, plain, 5);

	assert_int_equal(situ_scrambler_additive_decode(coded, 5, back), 5);
	assert_memory_equal(back, plain, 5);
}

static void test_the_multiplicative_scrambler_is_not_an_involution(void **state)
{
	/* Feedback from the output: a receiver synchronises itself without being
	 * told the state, and pays for it with error propagation. Running the
	 * encoder twice does not undo it, which is the observable difference from
	 * the additive one and why they derive opposite properties. */
	static const uint8_t plain[6] = { 0x01, 0x02, 0x03, 0x04, 0x05, 0x06 };
	uint8_t coded[6];
	uint8_t back[6];
	uint8_t twice[6];

	(void)state;

	assert_int_equal(situ_scrambler_multiplicative_encode(plain, 6, coded), 6);
	assert_int_equal(situ_scrambler_multiplicative_decode(coded, 6, back), 6);
	assert_memory_equal(back, plain, 6);

	situ_scrambler_multiplicative_encode(coded, 6, twice);
	assert_memory_not_equal(twice, plain, 6);
}

/* -- block interleaver ----------------------------------------------------- */

static void test_the_interleaver_spreads_adjacent_bytes(void **state)
{
	/* Written in rows and read in columns, which is what puts a burst error
	 * across codewords instead of inside one. A 4 x 4 block emits byte 0, then
	 * byte 4, then byte 8. */
	uint8_t in[16];
	uint8_t out[16];
	uint8_t back[16];
	uint8_t i;

	(void)state;

	for (i = 0; i < 16; i++) {
		in[i] = i;
	}

	assert_int_equal(situ_interleave_16_encode(in, 16, out), 16);
	assert_int_equal(out[0], 0);
	assert_int_equal(out[1], 4);
	assert_int_equal(out[2], 8);
	assert_int_equal(out[3], 12);
	assert_int_equal(out[4], 1);

	assert_int_equal(situ_interleave_16_decode(out, 16, back), 16);
	assert_memory_equal(back, in, 16);
}

static void test_the_interleaver_refuses_a_partial_block(void **state)
{
	/* A partial block has no defined permutation, and inventing one would make
	 * the decoder disagree with the encoder about where a byte went. */
	uint8_t in[16] = { 0 };
	uint8_t out[16];

	(void)state;

	assert_int_equal(situ_interleave_16_encode(in, 15, out), 0);
}

/* -- HDLC bit stuffing ----------------------------------------------------- */

static void test_hdlc_inserts_a_zero_after_five_ones(void **state)
{
	/* Which is what stops the flag sequence appearing inside a frame. Sixteen
	 * ones take three inserted zeros, so nineteen bits come out. */
	static const uint8_t ones[2] = { 0xFF, 0xFF };
	uint8_t out[4]  = { 0, 0, 0, 0 };
	uint8_t back[4] = { 0, 0, 0, 0 };

	(void)state;

	assert_int_equal(situ_hdlc_bit_stuffing_encode(ones, 16, out), 19);
	assert_int_equal(situ_hdlc_bit_stuffing_decode(out, 19, back), 16);
	assert_int_equal(back[0], 0xFF);
	assert_int_equal(back[1], 0xFF);
}

static void test_hdlc_refuses_six_ones(void **state)
{
	/* Six consecutive ones is a flag, not data: an encoder cannot have produced
	 * them, so a decoder that accepted them would be taking a corrupted frame
	 * for a valid one. */
	static const uint8_t flag[1] = { 0xFC };	/* 111111.. */
	uint8_t out[4];

	(void)state;

	assert_int_equal(situ_hdlc_bit_stuffing_decode(flag, 8, out), 0);
}

/* -- base16 (RFC 4648) ----------------------------------------------------- */

static void test_base16_encodes_to_ascii_hex(void **state)
{
	/* Not a value situ computed and wrote down: this is what `xxd` prints. */
	static const uint8_t in[3] = { 0xDE, 0xAD, 0xBE };
	uint8_t out[8]  = { 0 };
	uint8_t back[4] = { 0 };

	(void)state;

	assert_int_equal(situ_base16_encode(in, 24u, out), 48u);
	assert_memory_equal(out, "DEADBE", 6);

	assert_int_equal(situ_base16_decode(out, 48u, back), 24u);
	assert_memory_equal(back, in, 3);
}

static void test_base16_needs_no_padding_at_any_length(void **state)
{
	/* The property that separates it from base32 and base64, and the reason it
	 * is a table code and nothing more: every byte is exactly two nibbles, so
	 * there is never a partial group to decide about. */
	uint8_t in[7];
	uint8_t out[16];
	uint8_t back[8];
	unsigned len;

	(void)state;

	for (len = 0; len < 7u; len++) {
		unsigned i;

		for (i = 0; i < len; i++) {
			in[i] = (uint8_t)(i * 37u + 1u);
		}

		assert_int_equal(situ_base16_encode(in, len * 8u, out), len * 16u);
		assert_int_equal(situ_base16_decode(out, len * 16u, back), len * 8u);
		assert_memory_equal(back, in, len);
	}
}

static void test_base16_lower_is_a_different_code_not_an_alias(void **state)
{
	/* A protocol that specifies one and receives the other has received
	 * something it did not specify. */
	static const uint8_t in[1] = { 0xAB };
	uint8_t upper[2] = { 0 };
	uint8_t lower[2] = { 0 };

	(void)state;

	situ_base16_encode(in, 8u, upper);
	situ_base16_lower_encode(in, 8u, lower);

	assert_memory_equal(upper, "AB", 2);
	assert_memory_equal(lower, "ab", 2);
}

/* -- base32 and base64 (RFC 4648) ------------------------------------------ */

static void test_base64_matches_rfc_4648_vectors(void **state)
{
	/* The RFC's own table, which is what makes these somebody else's answer
	 * rather than situ's. Every length mod 3 appears, because that is what
	 * decides how much padding there is. */
	static const struct { const char *in; const char *want; } cases[] = {
		{ "",       ""         },
		{ "f",      "Zg=="     },
		{ "fo",     "Zm8="     },
		{ "foo",    "Zm9v"     },
		{ "foob",   "Zm9vYg==" },
		{ "fooba",  "Zm9vYmE=" },
		{ "foobar", "Zm9vYmFy" },
	};
	uint8_t  out[16];
	uint8_t  back[16];
	unsigned i;

	(void)state;

	for (i = 0; i < sizeof cases / sizeof cases[0]; i++) {
		uint32_t len     = (uint32_t)strlen(cases[i].in);
		uint32_t written = situ_base64_encode((const uint8_t *)cases[i].in, len, out);

		assert_int_equal(written, strlen(cases[i].want));
		assert_memory_equal(out, cases[i].want, written);

		assert_int_equal(situ_base64_decode(out, written, back), len);
		assert_memory_equal(back, cases[i].in, len);
	}
}

static void test_base32_matches_rfc_4648_vectors(void **state)
{
	static const struct { const char *in; const char *want; } cases[] = {
		{ "",       ""                 },
		{ "f",      "MY======"         },
		{ "fo",     "MZXQ===="         },
		{ "foo",    "MZXW6==="         },
		{ "foob",   "MZXW6YQ="         },
		{ "fooba",  "MZXW6YTB"         },
		{ "foobar", "MZXW6YTBOI======" },
	};
	uint8_t  out[24];
	uint8_t  back[16];
	unsigned i;

	(void)state;

	for (i = 0; i < sizeof cases / sizeof cases[0]; i++) {
		uint32_t len     = (uint32_t)strlen(cases[i].in);
		uint32_t written = situ_base32_encode((const uint8_t *)cases[i].in, len, out);

		assert_int_equal(written, strlen(cases[i].want));
		assert_memory_equal(out, cases[i].want, written);

		assert_int_equal(situ_base32_decode(out, written, back), len);
		assert_memory_equal(back, cases[i].in, len);
	}
}

static void test_the_output_is_always_whole_groups(void **state)
{
	/* Which is what `ratio_padded` claims, and the reason the form had to be
	 * added: an exact ratio would predict 2 characters for one byte of base64,
	 * and the answer is 4. */
	uint8_t  in[16];
	uint8_t  out[64];
	unsigned n;

	(void)state;

	for (n = 0; n <= 16u; n++) {
		unsigned i;

		for (i = 0; i < n; i++) {
			in[i] = (uint8_t)(i * 37u + 5u);
		}

		assert_int_equal(situ_base64_encode(in, n, out), ((n + 2u) / 3u) * 4u);
		assert_int_equal(situ_base32_encode(in, n, out), ((n + 4u) / 5u) * 8u);
	}
}

static void test_base64url_is_a_different_code(void **state)
{
	/* The two alphabets differ only in their last two characters, so a value
	 * exercising 62 and 63 is the only one that tells them apart -- and text
	 * encoded with one and decoded with the other is wrong in exactly the
	 * bytes that made somebody reach for it. */
	static const uint8_t in[3] = { 0xFB, 0xFF, 0xBF };
	uint8_t plain[8] = { 0 };
	uint8_t safe[8]  = { 0 };

	(void)state;

	situ_base64_encode(in, 3, plain);
	situ_base64url_encode(in, 3, safe);

	assert_memory_equal(plain, "+/+/", 4);
	assert_memory_equal(safe,  "-_-_", 4);
}

static void test_base64_refuses_what_it_did_not_encode(void **state)
{
	/* A length that is not whole groups, and a byte outside the alphabet.
	 * Both are input somebody else produced, which is the only kind a decoder
	 * ever sees. */
	uint8_t back[16];

	(void)state;

	assert_int_equal(situ_base64_decode((const uint8_t *)"Zm9", 3, back), 0);
	assert_int_equal(situ_base64_decode((const uint8_t *)"Zm9 ", 4, back), 0);
	assert_int_equal(situ_base64_decode((const uint8_t *)"Zm9*", 4, back), 0);
}

/* -- Reed-Solomon ---------------------------------------------------------- */

/* A small deterministic generator, so a failure is reproducible. The point is
 * to cover many error placements rather than to be unpredictable. */
static uint32_t rs_seed = 12345u;

static uint32_t rs_random(void)
{
	rs_seed = rs_seed * 1103515245u + 12345u;
	return (rs_seed >> 16) & 0x7FFFu;
}

static void test_reed_solomon_is_systematic(void **state)
{
	/* Section 13.2 calls `systematic` the highest-value property: the message
	 * sits verbatim at computable positions, so a field under the code can be
	 * read with no decode at all. */
	uint8_t  data[223];
	uint8_t  block[255];
	unsigned i;

	(void)state;

	for (i = 0; i < 223; i++) {
		data[i] = (uint8_t)(i * 7u + 1u);
	}
	memcpy(block, data, 223);

	assert_int_equal(situ_reed_solomon_255_223_encode(data, 223, block + 223), 32);
	assert_memory_equal(block, data, 223);
}

static void test_an_undamaged_block_needs_no_correction(void **state)
{
	uint8_t  data[223];
	uint8_t  block[255];
	unsigned i;

	(void)state;

	for (i = 0; i < 223; i++) {
		data[i] = (uint8_t)(i * 7u + 1u);
	}
	memcpy(block, data, 223);
	situ_reed_solomon_255_223_encode(data, 223, block + 223);

	assert_int_equal(situ_reed_solomon_255_223_decode(block, 255), 0);
}

static void test_reed_solomon_corrects_up_to_sixteen_errors(void **state)
{
	/* The defining property of RS(255, 223), and an independent one: it is
	 * true of the code whoever implements it. Errors are placed anywhere in
	 * the block, including in the parity, because the code does not privilege
	 * the message half. */
	uint8_t  data[223];
	uint8_t  clean[255];
	uint8_t  block[255];
	unsigned i;
	unsigned trial;

	(void)state;

	for (i = 0; i < 223; i++) {
		data[i] = (uint8_t)(i * 7u + 1u);
	}
	memcpy(clean, data, 223);
	situ_reed_solomon_255_223_encode(data, 223, clean + 223);

	for (trial = 0; trial < 100u; trial++) {
		unsigned errors = 1u + rs_random() % 16u;
		unsigned placed = 0;

		memcpy(block, clean, 255);

		while (placed < errors) {
			unsigned at = rs_random() % 255u;

			if (block[at] == clean[at]) {
				block[at] = (uint8_t)(block[at] ^ (uint8_t)(1u + rs_random() % 255u));
				placed++;
			}
		}

		assert_int_equal(situ_reed_solomon_255_223_decode(block, 255),
		                 (int)errors);
		assert_memory_equal(block, clean, 255);
	}
}

static void test_a_shortened_code_works_the_same_way(void **state)
{
	/* The tables and the generator polynomial come from the parameters, so a
	 * code nobody has standardised encodes exactly as well as CCSDS's. */
	uint8_t  data[56];
	uint8_t  clean[64];
	uint8_t  block[64];
	unsigned i;

	(void)state;

	for (i = 0; i < 56; i++) {
		data[i] = (uint8_t)(i * 13u + 5u);
	}
	memcpy(clean, data, 56);

	assert_int_equal(situ_reed_solomon_64_56_encode(data, 56, clean + 56), 8);
	memcpy(block, clean, 64);
	assert_int_equal(situ_reed_solomon_64_56_decode(block, 64), 0);

	/* Four errors is the capacity of eight parity symbols. */
	block[3]  = (uint8_t)(block[3] ^ 0xFFu);
	block[20] = (uint8_t)(block[20] ^ 0x01u);
	block[58] = (uint8_t)(block[58] ^ 0x7Fu);
	block[63] = (uint8_t)(block[63] ^ 0xA5u);

	assert_int_equal(situ_reed_solomon_64_56_decode(block, 64), 4);
	assert_memory_equal(block, clean, 64);
}

static void test_a_block_of_the_wrong_length_is_refused(void **state)
{
	uint8_t block[255] = { 0 };

	(void)state;

	assert_int_equal(situ_reed_solomon_255_223_decode(block, 254), -1);
	assert_int_equal(situ_reed_solomon_255_223_encode(block, 222, block), 0);
}


/* -- SMTP dot-stuffing ----------------------------------------------------- */

/* RFC 5321 section 4.5.2, which is two sentences long and turns on exactly
 * these cases: a line beginning with a period is sent with two, and the
 * receiver removes one. */
static void test_dot_stuffing_doubles_a_leading_period(void **state)
{
	static const struct { const char *plain, *wire; } CASES[] = {
		{ "hello\r\n",            "hello\r\n" },
		{ ".hello\r\n",           "..hello\r\n" },
		{ ".\r\n",                "..\r\n" },
		{ "a.b\r\n",              "a.b\r\n" },      /* not at a line start */
		{ ".a\r\n.b\r\n",         "..a\r\n..b\r\n" },
		{ "x\r\n.y\r\nz\r\n",     "x\r\n..y\r\nz\r\n" },
		{ "\r\n.q\r\n",           "\r\n..q\r\n" },
	};
	uint8_t out[64];
	uint8_t back[64];
	size_t i;

	(void)state;
	for (i = 0; i < sizeof(CASES) / sizeof(CASES[0]); i++) {
		const uint32_t plain = (uint32_t)strlen(CASES[i].plain);
		const uint32_t wire  = (uint32_t)strlen(CASES[i].wire);
		uint32_t written;

		written = situ_smtp_dot_stuffing_encode(
		    (const uint8_t *)CASES[i].plain, plain, out);
		assert_int_equal(written, wire);
		assert_memory_equal(out, CASES[i].wire, wire);

		assert_int_equal(situ_smtp_dot_stuffing_decode(
		    (const uint8_t *)CASES[i].wire, wire, back), plain);
		assert_memory_equal(back, CASES[i].plain, plain);
	}
}

static void test_dot_stuffing_meets_its_declared_worst_case(void **state)
{
	/* `ratio_bounded(4, 3)`: a body of nothing but `.CRLF` lines, three bytes
	 * in and four out. The properties and the code come from one description,
	 * so a mismatch here would mean the map is wrong about the wire. */
	uint8_t out[16];

	(void)state;
	assert_int_equal(situ_smtp_dot_stuffing_encode((const uint8_t *)".\r\n", 3,
	                                               out), 4);
}

static void test_dot_stuffing_carries_line_state_across_the_buffer(void **state)
{
	/* `unit = stream`: the trigger is the start of a line, and the body starts
	 * at one. A decoder that only looked at bytes could not tell the first
	 * period of the body from one in the middle of a line. */
	uint8_t out[32];

	(void)state;
	assert_int_equal(situ_smtp_dot_stuffing_decode((const uint8_t *)"..x\r\n", 5,
	                                               out), 4);
	assert_memory_equal(out, ".x\r\n", 4);
}

int main(void)
{
	const struct CMUnitTest tests[] = {
		cmocka_unit_test(test_cobs_matches_the_published_examples),
		cmocka_unit_test(test_cobs_spends_one_byte_per_254),
		cmocka_unit_test(test_hamming_is_systematic),
		cmocka_unit_test(test_hamming_corrects_any_single_bit_error),
		cmocka_unit_test(test_the_additive_scrambler_is_its_own_inverse),
		cmocka_unit_test(test_the_multiplicative_scrambler_is_not_an_involution),
		cmocka_unit_test(test_the_interleaver_spreads_adjacent_bytes),
		cmocka_unit_test(test_the_interleaver_refuses_a_partial_block),
		cmocka_unit_test(test_dot_stuffing_doubles_a_leading_period),
		cmocka_unit_test(test_dot_stuffing_meets_its_declared_worst_case),
		cmocka_unit_test(test_dot_stuffing_carries_line_state_across_the_buffer),
		cmocka_unit_test(test_hdlc_inserts_a_zero_after_five_ones),
		cmocka_unit_test(test_hdlc_refuses_six_ones),
		cmocka_unit_test(test_base64_matches_rfc_4648_vectors),
		cmocka_unit_test(test_base32_matches_rfc_4648_vectors),
		cmocka_unit_test(test_the_output_is_always_whole_groups),
		cmocka_unit_test(test_base64url_is_a_different_code),
		cmocka_unit_test(test_base64_refuses_what_it_did_not_encode),
		cmocka_unit_test(test_base16_encodes_to_ascii_hex),
		cmocka_unit_test(test_base16_needs_no_padding_at_any_length),
		cmocka_unit_test(test_base16_lower_is_a_different_code_not_an_alias),
		cmocka_unit_test(test_reed_solomon_is_systematic),
		cmocka_unit_test(test_an_undamaged_block_needs_no_correction),
		cmocka_unit_test(test_reed_solomon_corrects_up_to_sixteen_errors),
		cmocka_unit_test(test_a_shortened_code_works_the_same_way),
		cmocka_unit_test(test_a_block_of_the_wrong_length_is_refused),
	};

	return cmocka_run_group_tests(tests, NULL, NULL);
}
