/* An embedded walker: read a packed layout image over live bytes.
 *
 * This is the walker decision 0026 was argued from -- a radio whose framing
 * must change without a firmware rebuild -- and decision 0035 records why it
 * is C. The Python walker in `walker/` is the fifth column of the
 * differential check and is not this; nothing here is a port of it.
 *
 * WHAT IT PROMISES.
 *
 *   * No allocation. Every function takes what it writes into. An embedded
 *     walker in a fixed arena is the caller 0031's caller buffers describe.
 *   * No recursion, and no unbounded loop. Section 10's language is total --
 *     no calls, no recursion, no iteration -- so a program's length is its
 *     own bound and the evaluator needs no step limit. A guard that cannot
 *     fire is worse than none, because it suggests the danger it does not
 *     address; that argument is 0026's and it is why this can be shipped to
 *     a device at all.
 *   * No libc beyond <stdint.h> and <stddef.h>.
 *
 * WHAT IT DOES NOT DO YET. Delimited members, varints, runs, variants,
 * regions and the `validate` probes. Those are the rest of the walk, and
 * 0035 sizes them. What is here is the spine: the image, the expression
 * evaluator, and a member placed and read.
 */
#ifndef SITU_WALK_H
#define SITU_WALK_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Matches `situ_err_t` in the runtime, so a caller mixing the two reads one
 * set of codes. The walker adds none of its own: a construct it cannot read
 * is `SITU_WALK_UNSUPPORTED`, which is a statement about this build rather
 * than about the bytes. */
typedef enum {
	SITU_WALK_OK          = 0,
	SITU_WALK_BOUNDS      = 1,
	SITU_WALK_CONSTRAINT  = 2,
	SITU_WALK_MALFORMED   = 8,   /* the image is not one */
	SITU_WALK_UNSUPPORTED = 9    /* a construct this build does not render */
} situ_walk_err;

/* `none`, as `std/image.situ` spells it. */
#define SITU_WALK_NONE 0xffffffffu

typedef struct {
	const uint8_t *image;
	uint32_t       image_len;

	const uint8_t *structs;
	uint32_t       struct_count;
	uint32_t       struct_stride;

	const uint8_t *placements;
	uint32_t       placement_count;
	uint32_t       placement_stride;

	const uint8_t *code;
	uint32_t       code_len;
} situ_walk_image;

/* One member, as the image describes it. */
typedef struct {
	uint8_t  kind;
	uint8_t  endian;
	uint8_t  flags;
	uint32_t offset_bits;
	uint32_t size_bits;
	uint32_t element_bits;
	uint32_t array_count;
	uint32_t size_code;
	uint32_t type_struct;
	uint32_t located_code;
} situ_walk_placement;

/* Bind an image. Every table it names is bounds-checked against the whole
 * before anything reads one, because the image is the least trusted input
 * this component has. */
situ_walk_err situ_walk_open(situ_walk_image *out,
	                             const uint8_t *image, uint32_t len);

/* How many members a struct has, and where they start. */
situ_walk_err situ_walk_members(const situ_walk_image *image, uint32_t shape,
	                                uint32_t *first, uint32_t *count);

/* One placement, decoded out of the table. */
situ_walk_err situ_walk_placement_at(const situ_walk_image *image,
	                                     uint32_t index,
	                                     situ_walk_placement *out);

/* A member's value, from a message. Fixed offsets and widths up to 64 bits;
 * anything else answers SITU_WALK_UNSUPPORTED rather than a number, because
 * a wrong length is indistinguishable from a right one once it leaves. */
situ_walk_err situ_walk_read(const situ_walk_image *image,
	                             const uint8_t *message, uint32_t len,
	                             uint32_t index, uint64_t *out);

/* Evaluate a section 10 program. `field` reads a placement's value for the
 * expression, and is the only thing tying this to a message. */
typedef situ_walk_err (*situ_walk_load)(void *ctx, uint32_t index,
	                                        int64_t *out);

situ_walk_err situ_walk_eval(const situ_walk_image *image, uint32_t at,
	                             situ_walk_load load, void *ctx,
	                             int64_t remaining, int64_t *out);

#ifdef __cplusplus
}
#endif

#endif /* SITU_WALK_H */
