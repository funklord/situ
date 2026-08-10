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

	const uint8_t *varints;
	uint32_t       varint_count;
	uint32_t       varint_stride;

	const uint8_t *delimiters;
	uint32_t       delimiter_count;
	uint32_t       delimiter_stride;
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
	uint32_t repeat_code;
	uint32_t type_struct;
	uint32_t located_code;
	/* A text number's base, 2 to 16, and zero for a member that is not one.
	 * `radix_digits` is how many digits the schema declared, which is the
	 * fixed-width form's width; the delimited form's comes from the scan. */
	uint8_t  radix;
	uint16_t radix_digits;
	/* `max` on a `while` run: the ceiling the schema put on how many
	 * elements one may hold, and zero where it stated none. */
	uint16_t repeat_cap;
} situ_walk_placement;

/* Bits of `situ_walk_placement.flags` a caller needs.
 *
 * `SITU_WALK_SIGNED` is not a detail. A value comes back in a `uint64_t`
 * with the sign extended through it, so `-2` and `18446744073709551614` are
 * the same answer and the caller decides which it is looking at -- and a
 * caller with no way to ask decides wrongly. The differential printed a
 * signed element unsigned and reported a disagreement that was not there,
 * which is what a missing accessor looks like from outside. */
#define SITU_WALK_OFFSET_KNOWN 0x01u
#define SITU_WALK_SIGNED       0x10u

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

/* Decode one varint at `at`, answering the bytes it consumed and the value.
 *
 * Two encodings, differing in which end the groups come from: `leb128` puts
 * the low group first, `be128` the high one -- ASN.1's identifier octets,
 * MIDI's delta times, SQLite's record varints. `terminal_bits` of eight is
 * the case worth naming: the last permitted byte has no spare bit for a
 * continuation flag, so it is read whole and ends the value whatever its
 * high bit says. That is SQLite's ninth byte, and it is why nine bytes hold
 * sixty-four bits where seven-bit groups would need ten.
 *
 * SITU_WALK_BOUNDS where the buffer ends mid-value, which is what the getter
 * does in every backend. */
situ_walk_err situ_walk_varint(const situ_walk_image *image,
	                               const uint8_t *message, uint32_t len,
	                               uint32_t index, uint32_t at,
	                               uint32_t *consumed, uint64_t *value);

/* Where a delimited member's content stops, and whether the delimiter was
 * there. `at` is the member's own byte offset.
 *
 * The two answers are separate on purpose, and the C runtime says why: a
 * member whose delimiter is absent is *truncated*, not empty, and it reaches
 * as far as the cap or the buffer allowed -- so the member after it starts
 * at that point rather than being unplaceable. The member's span is the
 * content plus the delimiter where there is one, which is `situ_walk_size_
 * bits`; the content alone is what a backend's `_ptr` and `_len` hand back.
 *
 * Naive matching, as the generated code does it: a delimiter is one or two
 * bytes in every format this targets, and a reader has to be able to check
 * it against the specification they are implementing. */
situ_walk_err situ_walk_scan(const situ_walk_image *image,
	                             const uint8_t *message, uint32_t len,
	                             uint32_t index, uint32_t at,
	                             uint32_t *content, int *terminated);

/* How wide a member is, in bits. A constant where the image knows one; a
 * `size_code` program otherwise, which is what `size = Bounded` costs.
 * SITU_WALK_UNSUPPORTED for a width this build cannot compute -- a `while`
 * run or a variant arm. */
situ_walk_err situ_walk_size_bits(const situ_walk_image *image,
	                                  const uint8_t *message, uint32_t len,
	                                  uint32_t shape, uint32_t index,
	                                  uint32_t *out);

/* Where a member starts, in bits from the message base.
 *
 * A constant where the image knows one, and otherwise the members before it
 * summed -- which is the answer `offset = Dynamic` names, and the reason a
 * walk costs what the capability map says it costs. */
situ_walk_err situ_walk_offset_bits(const situ_walk_image *image,
	                                    const uint8_t *message, uint32_t len,
	                                    uint32_t shape, uint32_t index,
	                                    uint32_t *out);

/* A member's value, from a message. Fixed offsets and widths up to 64 bits;
 * anything else answers SITU_WALK_UNSUPPORTED rather than a number, because
 * a wrong length is indistinguishable from a right one once it leaves. */
situ_walk_err situ_walk_read(const situ_walk_image *image,
	                             const uint8_t *message, uint32_t len,
	                             uint32_t shape, uint32_t index,
	                             uint64_t *out);

/* How many elements a run holds: a declared count, or the `size_code`
 * program the message answers.
 *
 * SITU_WALK_UNSUPPORTED for a member that is not a run, and for a `while`
 * run -- how many elements one holds is whichever first fails the predicate,
 * which is a walk this build does not have. */
situ_walk_err situ_walk_count(const situ_walk_image *image,
	                              const uint8_t *message, uint32_t len,
	                              uint32_t shape, uint32_t index,
	                              uint32_t *out);

/* One element of a run, by index rather than by pointer.
 *
 * A run has no single value and `situ_walk_read` refuses one; this is how a
 * caller asks for the values it does have. The elements are the bytes, so
 * this is the same read at a different offset -- which is deliberately one
 * function rather than two, a backend and its own run accessor having once
 * disagreed about exactly that. */
situ_walk_err situ_walk_element(const situ_walk_image *image,
	                               const uint8_t *message, uint32_t len,
	                               uint32_t shape, uint32_t index,
	                               uint32_t at, uint64_t *out);

/* A member's bytes: where they start in `message`, and how many.
 *
 * For the runs and arrays that have no scalar value, and for a delimited
 * member, whose span carries its delimiter because that is what places the
 * member after it. Points into the caller's buffer and copies nothing: an
 * embedded walker in a fixed arena has nowhere to copy to. */
situ_walk_err situ_walk_bytes(const situ_walk_image *image,
	                              const uint8_t *message, uint32_t len,
	                              uint32_t shape, uint32_t index,
	                              const uint8_t **out, uint32_t *count);

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
