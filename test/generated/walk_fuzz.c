/* walk_fuzz.c -- the embedded C walker over bytes nobody wrote.
 *
 * The generated accessors have had a fuzz harness each since phase 22, and
 * running it found three memory-safety defects the first time anybody did
 * (26.322). The walker is the other reading of the same layout and had
 * none: it is checked against the Python walker on hand-written cases, so
 * every byte it had ever seen was a byte this repository chose.
 *
 * The IMAGE is fixed and compiled in, because a walker trusts its image --
 * that is the schema. The MESSAGE is the fuzzer's, which is the threat
 * model. One harness per schema, the image included by name so the same
 * source serves all of them.
 *
 * Every shape, every member, every question `main.c` asks and three it does
 * not: the byte span's LAST byte rather than its first, one element of a
 * run, and `validate`. Bounded by the image: the shape loop stops at the
 * first refusal and the member loop at the count the image gives.
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "situ_walk.h"
#include "walk_image.h"

static situ_walk_image image;
static int             ready;

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
	uint32_t shape;

	if (!ready) {
		if (situ_walk_open(&image, IMAGE_BLOB,
		                   (uint32_t)sizeof IMAGE_BLOB) != SITU_WALK_OK) {
			return 0;
		}
		ready = 1;
	}
	if (size > 4096u) {
		return 0;
	}

	for (shape = 0u; shape < 64u; shape++) {
		uint32_t first = 0u, count = 0u, i;
		situ_walk_err verdict = SITU_WALK_OK;

		if (situ_walk_members(&image, shape, &first, &count) != SITU_WALK_OK) {
			break;
		}
		(void)situ_walk_validate(&image, data, (uint32_t)size, shape,
		                         &verdict);

		for (i = 0u; i < count; i++) {
			const uint32_t at = first + i;
			uint32_t offset = 0u, bits = 0u, n = 0u, marker = 0u;
			uint64_t value = 0u;
			const uint8_t *span = NULL;

			(void)situ_walk_offset_bits(&image, data, (uint32_t)size,
			                            shape, at, &offset);
			(void)situ_walk_size_bits(&image, data, (uint32_t)size,
			                          shape, at, &bits);
			(void)situ_walk_read(&image, data, (uint32_t)size,
			                     shape, at, &value);
			(void)situ_walk_marker(&image, data, (uint32_t)size,
			                       shape, at, &marker);
			if (situ_walk_bytes(&image, data, (uint32_t)size, shape, at,
			                    &span, &n) == SITU_WALK_OK && span != NULL
			    && n != 0u) {
				/* Touch the last byte it handed back: a span that reaches
				 * past the message is the failure this is looking for, and
				 * reading only the first would miss every one of them. */
				volatile uint8_t sink = span[n - 1u];
				(void)sink;
			}
			if (situ_walk_count(&image, data, (uint32_t)size, shape, at,
			                    &n) == SITU_WALK_OK) {
				uint32_t k;

				for (k = 0u; k < n && k < 64u; k++) {
					uint64_t element = 0u;

					(void)situ_walk_element(&image, data, (uint32_t)size,
					                        shape, at, k, &element);
				}
			}
		}
	}
	return 0;
}
