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
 * invertible and deterministic -- exactly what `tests/schemas/edges.situ`
 * declares for it. The point is not the algorithm. The point is that the four
 * properties are checked against a running implementation rather than
 * asserted about an absent one.
 *
 * A reviewer wanting to see the tests bite can break one property here -- make
 * the encode append a byte, or the decode drop the last pair -- and watch
 * which test fails.
 */

#include <stdint.h>

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
 * `tests/schemas/edges.situ` has something to link against.
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
 * `tests/schemas/edges.situ`, so section 14.1a's clause has an implementation
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
