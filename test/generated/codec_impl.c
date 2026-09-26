/* my_doubling -- a tier-1 codec implementation, so the property tests run.
 *
 * `situc gen-codec-tests` has emitted property tests since phase 7 and had
 * never been run against anything: it named `situ_codec_<codec>_encode`, which
 * is not what the accessors call, not what the spec names, and not what `impl
 * doubling extern "my_doubling"` binds. So the harness could not be linked
 * with any implementation this repository produces, and "these are the tests
 * that would catch a lying signature" was a claim nothing exercised (26.35).
 *
 * It calls the ABI of section 13.2a now, under the symbol the schema binds,
 * and this is a codec to call. Deliberately trivial: each byte becomes two of
 * itself, which is `ratio_exact(2, 1)`, byte granularity, linear seekability,
 * invertible and deterministic -- exactly what `test/schema/edges.situ`
 * declares for it. The point is not the algorithm. The point is that the four
 * properties are checked against a running implementation rather than
 * asserted about an absent one.
 *
 * A reviewer wanting to see the tests bite can break one property here -- make
 * the encode append a byte, or the decode drop the last pair -- and watch
 * which test fails.
 */

#include <stdint.h>
#include <stddef.h>

#include "situ.h"

situ_err_t my_doubling_encode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len);
situ_err_t my_doubling_decode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len);

situ_err_t my_doubling_encode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len)
{
	uint32_t i;

	if (in_len > UINT32_MAX / 2u || out_cap < in_len * 2u) {
		return SITU_ERR_BOUNDS;
	}

	for (i = 0u; i < in_len; i++) {
		out[i * 2u]      = in[i];
		out[i * 2u + 1u] = in[i];
	}

	*out_len = in_len * 2u;
	return SITU_OK;
}

situ_err_t my_doubling_decode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len)
{
	uint32_t i;

	/* An odd length is not something this codec produces, so it is not
	 * something it decodes: half a pair is a malformed region rather than a
	 * shorter one. */
	if (in_len % 2u != 0u) {
		return SITU_ERR_CONSTRAINT;
	}
	if (out_cap < in_len / 2u) {
		return SITU_ERR_BOUNDS;
	}

	for (i = 0u; i < in_len / 2u; i++) {
		if (in[i * 2u] != in[i * 2u + 1u]) {
			return SITU_ERR_CONSTRAINT;
		}
		out[i] = in[i * 2u];
	}

	*out_len = in_len / 2u;
	return SITU_OK;
}

/* my_sealing_aead -- length-preserving, so the sealed region in
 * `test/schema/edges.situ` has something to link against.
 *
 * `[allow_unverified_read]` needs a codec that *authenticates* -- a gate
 * cannot be waived where there is no gate -- and the doubling codec above
 * does not. This one declares the four properties that sealing asks for and
 * keeps them: XOR with a constant is its own inverse, so it is invertible and
 * deterministic; it changes no length, so it is length-preserving and every
 * interior offset survives it.
 *
 * It authenticates nothing, which is the one property here that is a lie. It
 * is a lie the schema tells too -- `impl ... extern` binds a symbol and says
 * nothing about what is behind it -- and the whole of section 13.1 is that
 * situ describes a transform's *properties* and never its algorithm. A test
 * that needed real AES-GCM to exercise the stage gate would be a test of
 * OpenSSL.
 */

situ_err_t my_sealing_aead_encode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len);
situ_err_t my_sealing_aead_decode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len);

situ_err_t my_sealing_aead_encode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len)
{
	uint32_t i;

	if (out_cap < in_len) {
		return SITU_ERR_BOUNDS;
	}
	for (i = 0u; i < in_len; i++) {
		out[i] = (uint8_t)(in[i] ^ 0x5Au);
	}
	*out_len = in_len;
	return SITU_OK;
}

situ_err_t my_sealing_aead_decode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len)
{
	return my_sealing_aead_encode(in, in_len, out, out_cap, out_len);
}

/* app_header_mask -- the codec behind `coded pn(masking) covers(first)` in
 * `test/schema/edges.situ`, so section 14.1a's clause has an implementation
 * its property tests can run against.
 *
 * A mask is what `covers` exists for. Header protection in QUIC derives one
 * from a sample of the ciphertext and XORs it across the first byte and the
 * packet number under a single operation, and the clause's whole reason to
 * exist is that those are two spans rather than one field. What matters here
 * is only that the transform preserves length -- 14.1a refuses the clause
 * otherwise, because a covered span sits at an offset the layout has already
 * fixed and a codec returning a different count would move it.
 *
 * XOR with a constant again, for `my_sealing_aead`'s reasons: it is its own
 * inverse, changes no length, and touches each byte independently, which is
 * exactly the four properties the schema declares. A different constant so
 * that a test confusing the two codecs fails rather than passing by accident.
 */

situ_err_t app_header_mask_encode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len);
situ_err_t app_header_mask_decode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len);

situ_err_t app_header_mask_encode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len)
{
	uint32_t i;

	if (out_cap < in_len) {
		return SITU_ERR_BOUNDS;
	}
	for (i = 0u; i < in_len; i++) {
		out[i] = (uint8_t)(in[i] ^ 0xA5u);
	}
	*out_len = in_len;
	return SITU_OK;
}

situ_err_t app_header_mask_decode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len)
{
	return app_header_mask_encode(in, in_len, out, out_cap, out_len);
}

/* The scattered half of the same mask (13.2b).
 *
 * What `covers` actually reaches. The accessors call this pair rather than
 * the contiguous one above whenever a `coded` region carries a `covers`
 * clause, because the spans it names need not be adjacent -- QUIC's header
 * protection masks the first byte and the packet number with the connection
 * id between them.
 *
 * In place, so there is no output buffer: 14.1a admits only a
 * length-preserving codec here, and in place is meaningful only where the
 * answer is the same size as the question.
 *
 * The same constant as above, so that a test can check the two forms agree
 * on a single contiguous span -- which is the property that says the
 * scattered path did not quietly become a second algorithm.
 */

situ_err_t app_header_mask_encode_spans(const situ_span_t *spans,
        uint32_t count);
situ_err_t app_header_mask_decode_spans(const situ_span_t *spans,
        uint32_t count);

situ_err_t app_header_mask_encode_spans(const situ_span_t *spans,
        uint32_t count)
{
	uint32_t which;
	uint32_t i;

	if (spans == NULL && count != 0u) {
		return SITU_ERR_CONSTRAINT;
	}

	for (which = 0u; which < count; which++) {
		if (spans[which].base == NULL && spans[which].len != 0u) {
			return SITU_ERR_CONSTRAINT;
		}
		for (i = 0u; i < spans[which].len; i++) {
			spans[which].base[i] = (uint8_t)(spans[which].base[i] ^ 0xA5u);
		}
	}

	return SITU_OK;
}

situ_err_t app_header_mask_decode_spans(const situ_span_t *spans,
        uint32_t count)
{
	return app_header_mask_encode_spans(spans, count);
}

/* my_stuffed_apart -- SLIP, supplied rather than derived.
 *
 * `edges` binds a codec that HAS a kernel to an extern implementation, which
 * no schema here did before: the kernel is what the signature is derived
 * from and the `impl` is who supplies the code, and nothing had separated
 * them (26.514). So this has to be a real SLIP, not a placeholder: the
 * generated property tests hold it to `ratio_bounded(2, 1) + 1`,
 * `invertible` and `deterministic`, which are the kernel's claims and not
 * this file's.
 *
 * RFC 1055. END terminates a frame; a literal END in the payload becomes
 * ESC ESC_END and a literal ESC becomes ESC ESC_ESC. The trailing END is
 * the `+ 1` in the signature and is what makes the region's `until "\xC0"`
 * sound -- END appears exactly once in an encoded frame and at the end,
 * which is the property the stuffing exists to create.
 *
 * A reviewer wanting to see the tests bite can drop the trailing END and
 * watch the round-trip fail, or escape only END and watch it fail on an
 * input containing ESC.
 */

#define SLIP_END     0xC0u
#define SLIP_ESC     0xDBu
#define SLIP_ESC_END 0xDCu
#define SLIP_ESC_ESC 0xDDu

situ_err_t my_stuffed_apart_encode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len);
situ_err_t my_stuffed_apart_decode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len);

situ_err_t my_stuffed_apart_encode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len)
{
	uint32_t at = 0u;
	uint32_t i;

	if ((in == NULL && in_len != 0u) || out == NULL || out_len == NULL) {
		return SITU_ERR_CONSTRAINT;
	}

	for (i = 0u; i < in_len; i++) {
		/* Two bytes may be needed, so the room is checked for two. */
		if (at + 2u > out_cap) {
			return SITU_ERR_BOUNDS;
		}
		if (in[i] == SLIP_END) {
			out[at++] = (uint8_t)SLIP_ESC;
			out[at++] = (uint8_t)SLIP_ESC_END;
		} else if (in[i] == SLIP_ESC) {
			out[at++] = (uint8_t)SLIP_ESC;
			out[at++] = (uint8_t)SLIP_ESC_ESC;
		} else {
			out[at++] = in[i];
		}
	}

	if (at + 1u > out_cap) {
		return SITU_ERR_BOUNDS;
	}
	out[at++] = (uint8_t)SLIP_END;

	*out_len = at;
	return SITU_OK;
}

situ_err_t my_stuffed_apart_decode(const uint8_t *in, uint32_t in_len,
        uint8_t *out, uint32_t out_cap, uint32_t *out_len)
{
	uint32_t at = 0u;
	uint32_t i  = 0u;

	if ((in == NULL && in_len != 0u) || out == NULL || out_len == NULL) {
		return SITU_ERR_CONSTRAINT;
	}

	while (i < in_len) {
		uint8_t byte = in[i++];

		if (byte == SLIP_END) {
			break;		/* the frame ends here, by construction */
		}
		if (byte == SLIP_ESC) {
			if (i >= in_len) {
				/* An escape with nothing after it. CONSTRAINT
				 * rather than BOUNDS: the buffer was read
				 * correctly and what it holds is not a frame,
				 * which is the same code `my_doubling_decode`
				 * uses for an input its code cannot describe. */
				return SITU_ERR_CONSTRAINT;
			}
			byte = in[i++];
			if (byte == SLIP_ESC_END) {
				byte = (uint8_t)SLIP_END;
			} else if (byte == SLIP_ESC_ESC) {
				byte = (uint8_t)SLIP_ESC;
			} else {
				/* An escape the code does not define. Refusing rather
				 * than computing something: there is nothing to
				 * compute and guessing would invent a byte. */
				return SITU_ERR_CONSTRAINT;
			}
		}
		if (at >= out_cap) {
			return SITU_ERR_BOUNDS;
		}
		out[at++] = byte;
	}

	*out_len = at;
	return SITU_OK;
}
