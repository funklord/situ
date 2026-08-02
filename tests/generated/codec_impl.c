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
