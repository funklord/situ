#include "situ_walk.h"

/* Section tags, from `image_section_tag` in std/image.situ. Only the ones
 * this build reads are named; a tag it does not know is skipped, which is
 * what the directory is for. */
#define TAG_STRUCTS    1u
#define TAG_PLACEMENTS 2u
#define TAG_CODE       3u
#define TAG_ARMS       5u
#define TAG_DELIMITERS 6u
#define TAG_VARINTS    9u
#define TAG_CONSTRAINTS 15u
#define TAG_ENUM_VALUES 16u

#define HEADER_BYTES  20u
#define SECTION_BYTES 16u

/* How far into a row this build reads, per table. Not the record's declared
 * width -- these are what `situ_walk_members`, `situ_walk_placement_at`,
 * `situ_walk_varint` and `situ_walk_scan` touch, and a row narrower than its
 * entry here would be read past. `fits` bounds the *table*, which leaves
 * exactly this hole at the last row of it. */
#define STRUCT_READS     8u
#define PLACEMENT_READS 48u
#define VARINT_READS    11u
#define DELIMITER_READS 32u
#define ARM_READS       21u
#define CONSTRAINT_READS 13u
#define ENUM_VALUE_READS 12u

/* The image is little endian by declaration (`endian little` in
 * image.situ): it is produced and consumed by the same toolchain, so there
 * is nothing to negotiate and no marker to read. */
static uint32_t u32_at(const uint8_t *at)
{
	return (uint32_t)at[0] | ((uint32_t)at[1] << 8)
	     | ((uint32_t)at[2] << 16) | ((uint32_t)at[3] << 24);
}

static uint16_t u16_at(const uint8_t *at)
{
	return (uint16_t)((uint32_t)at[0] | ((uint32_t)at[1] << 8));
}

static int64_t i64_at(const uint8_t *at)
{
	uint64_t held = 0;
	for (unsigned i = 0; i < 8u; i++) {
		held |= (uint64_t)at[i] << (8u * i);
	}
	return (int64_t)held;
}

/* A section's extent has to fit the image before anything indexes it. The
 * image is the least trusted input here, and a table that runs off the end
 * is the first thing a hostile one would try. */
static int fits(uint32_t offset, uint32_t count, uint32_t stride,
                uint32_t len)
{
	if (stride != 0u && count > (0xffffffffu / stride)) {
		return 0;
	}
	const uint32_t span = count * stride;
	return offset <= len && span <= len - offset;
}

situ_walk_err situ_walk_open(situ_walk_image *out,
                             const uint8_t *image, uint32_t len)
{
	if (out == NULL || image == NULL || len < HEADER_BYTES) {
		return SITU_WALK_MALFORMED;
	}
	if (image[0] != 'S' || image[1] != 'I' || image[2] != 'T'
	                || image[3] != 'U') {
		return SITU_WALK_MALFORMED;
	}
	if (u16_at(image + 4) != 2u) {
		return SITU_WALK_MALFORMED;	/* a format this build predates */
	}
	if (u32_at(image + 8) != len) {
		return SITU_WALK_MALFORMED;
	}

	const uint32_t count     = u32_at(image + 12);
	const uint32_t directory = u32_at(image + 16);
	if (!fits(directory, count, SECTION_BYTES, len)) {
		return SITU_WALK_MALFORMED;
	}

	/* Everything at once, rather than a field per table. A section the image
	 * does not carry leaves its entry untouched by the loop below, so an
	 * absent table has to read as empty -- and the varint table was added to
	 * the struct and to the loop and not to the list that used to stand here.
	 * An image without varints then searched whatever the caller's stack held,
	 * which is a segfault under a memset of 0xAA and passed here for as long
	 * as that stack happened to be zero. A list of what to clear is a list
	 * that goes stale; this cannot. */
	*out = (situ_walk_image){0};
	out->image     = image;
	out->image_len = len;

	for (uint32_t i = 0u; i < count; i++) {
		const uint8_t *entry = image + directory + i * SECTION_BYTES;
		const uint32_t kind   = u32_at(entry);
		const uint32_t offset = u32_at(entry + 4);
		const uint32_t items  = u32_at(entry + 8);
		const uint32_t stride = u32_at(entry + 12);

		if (!fits(offset, items, stride, len)) {
			return SITU_WALK_MALFORMED;
		}

		/* A tag this build does not know is skipped rather than refused.
		 * That is the whole point of a directory: an image from a later
		 * situc is readable, not an error.
		 *
		 * A tag it *does* know is checked against the width this build reads
		 * out of a row, which `fits` cannot do for it: that bounds the table,
		 * and a record shorter than the fields read from it runs off the end
		 * of the last row rather than off the end of the table. A stride
		 * wider than this build expects is fine and is how the format grows.
		 */
		if (kind == TAG_STRUCTS) {
			if (stride < STRUCT_READS) {
				return SITU_WALK_MALFORMED;
			}
			out->structs       = image + offset;
			out->struct_count  = items;
			out->struct_stride = stride;
		} else if (kind == TAG_PLACEMENTS) {
			if (stride < PLACEMENT_READS) {
				return SITU_WALK_MALFORMED;
			}
			out->placements       = image + offset;
			out->placement_count  = items;
			out->placement_stride = stride;
		} else if (kind == TAG_CODE) {
			out->code     = image + offset;
			out->code_len = items * stride;
		} else if (kind == TAG_VARINTS) {
			if (stride < VARINT_READS) {
				return SITU_WALK_MALFORMED;
			}
			out->varints       = image + offset;
			out->varint_count  = items;
			out->varint_stride = stride;
		} else if (kind == TAG_ARMS) {
			if (stride < ARM_READS) {
				return SITU_WALK_MALFORMED;
			}
			out->arms       = image + offset;
			out->arm_count  = items;
			out->arm_stride = stride;
		} else if (kind == TAG_CONSTRAINTS) {
			if (stride < CONSTRAINT_READS) {
				return SITU_WALK_MALFORMED;
			}
			out->constraints       = image + offset;
			out->constraint_count  = items;
			out->constraint_stride = stride;
		} else if (kind == TAG_ENUM_VALUES) {
			if (stride < ENUM_VALUE_READS) {
				return SITU_WALK_MALFORMED;
			}
			out->enum_values       = image + offset;
			out->enum_value_count  = items;
			out->enum_value_stride = stride;
		} else if (kind == TAG_DELIMITERS) {
			if (stride < DELIMITER_READS) {
				return SITU_WALK_MALFORMED;
			}
			out->delimiters       = image + offset;
			out->delimiter_count  = items;
			out->delimiter_stride = stride;
		}
	}

	return (out->structs == NULL || out->placements == NULL)
	        ? SITU_WALK_MALFORMED : SITU_WALK_OK;
}

situ_walk_err situ_walk_members(const situ_walk_image *image, uint32_t shape,
                                uint32_t *first, uint32_t *count)
{
	if (shape >= image->struct_count) {
		return SITU_WALK_BOUNDS;
	}
	const uint8_t *entry = image->structs + shape * image->struct_stride;
	*first = u32_at(entry);
	*count = u32_at(entry + 4);

	if (*first > image->placement_count
	                || *count > image->placement_count - *first) {
		return SITU_WALK_MALFORMED;
	}
	return SITU_WALK_OK;
}

situ_walk_err situ_walk_placement_at(const situ_walk_image *image,
                                     uint32_t index,
                                     situ_walk_placement *out)
{
	if (index >= image->placement_count) {
		return SITU_WALK_BOUNDS;
	}
	const uint8_t *at = image->placements + index * image->placement_stride;

	out->kind         = at[0];
	out->endian       = at[1];
	out->flags        = at[3];
	out->offset_bits  = u32_at(at + 4);
	out->size_bits    = u32_at(at + 8);
	out->element_bits = u32_at(at + 16);
	out->array_count  = u32_at(at + 20);
	out->size_code    = u32_at(at + 24);
	out->type_struct  = u32_at(at + 28);
	out->located_code = u32_at(at + 32);
	out->repeat_code  = u32_at(at + 36);
	out->radix        = at[40];
	out->radix_digits = u16_at(at + 42);
	out->repeat_cap   = u16_at(at + 46);
	return SITU_WALK_OK;
}

/* -- delimiters --------------------------------------------------------- */

/* What a record can hold, from `DELIMITER_BYTES` in `situc/pack.py`: four
 * words, a length byte, and fifteen bytes of delimiter. */
#define DELIMITER_MAX 15u

static const uint8_t *table_row(const uint8_t *table, uint32_t count,
                                uint32_t stride, uint32_t index)
{
	uint32_t low  = 0u;
	uint32_t high = count;

	/* Sorted by placement, which the image format guarantees precisely so
	 * that a walker can search rather than build a reverse map in an arena
	 * it may not have. */
	while (low < high) {
		const uint32_t mid = low + (high - low) / 2u;
		const uint8_t *at  = table + mid * stride;
		const uint32_t who = u32_at(at);

		if (who == index) {
			return at;
		}
		if (who < index) {
			low = mid + 1u;
		} else {
			high = mid;
		}
	}
	return NULL;
}

static const uint8_t *delimiter_rules(const situ_walk_image *image,
                                      uint32_t index)
{
	return table_row(image->delimiters, image->delimiter_count,
	                 image->delimiter_stride, index);
}

static int delimiter_at(const uint8_t *message, const uint8_t *delim,
                        uint32_t width)
{
	for (uint32_t i = 0u; i < width; i++) {
		if (message[i] != delim[i]) {
			return 0;
		}
	}
	return 1;
}

situ_walk_err situ_walk_scan(const situ_walk_image *image,
                             const uint8_t *message, uint32_t len,
                             uint32_t index, uint32_t at,
                             uint32_t *content, int *terminated)
{
	const uint8_t *rules = delimiter_rules(image, index);
	if (rules == NULL) {
		return SITU_WALK_UNSUPPORTED;
	}

	const uint32_t quote  = u32_at(rules + 4);
	const uint32_t escape = u32_at(rules + 8);
	const uint32_t cap    = u32_at(rules + 12);
	const uint32_t width  = rules[16];
	const uint8_t *delim  = rules + 17;

	if (width == 0u || width > DELIMITER_MAX) {
		return SITU_WALK_MALFORMED;
	}
	if (at > len) {
		return SITU_WALK_BOUNDS;
	}

	/* `max` bounds the search, not just the member: a delimiter that never
	 * arrives must stop being looked for somewhere the schema chose. */
	uint32_t limit = len - at;
	if (cap != SITU_WALK_NONE && cap < limit) {
		limit = cap;
	}

	int      quoted = 0;
	uint32_t i      = 0u;
	while (width <= limit && i <= limit - width) {
		const uint8_t byte = message[at + i];

		if (escape != SITU_WALK_NONE && byte == (uint8_t)escape) {
			i += 2u;	/* the next byte is content, whatever it is */
			continue;
		}
		if (quote != SITU_WALK_NONE && byte == (uint8_t)quote) {
			quoted = !quoted;
			i += 1u;
			continue;
		}
		if (!quoted && delimiter_at(message + at + i, delim, width)) {
			*content    = i;
			*terminated = 1;
			return SITU_WALK_OK;
		}
		i += 1u;
	}

	/* Truncated, and that is not a refusal: the member reached as far as the
	 * cap or the buffer allowed, and the member after it starts there. */
	*content    = limit;
	*terminated = 0;
	return SITU_WALK_OK;
}

/* -- text numbers -------------------------------------------------------- */

/* The digits of a text number, in the member's own terms: `situ_parse_uint`
 * without the declared domain, because that is a `validate` question and this
 * answers what the field holds. Refuses on overflow of `uint64_t` rather than
 * wrapping -- the Python walker has arbitrary precision and no schema here
 * writes a number that needs it, so the two agree for every digit count the
 * tree contains and this says where they would stop. */
static situ_walk_err parse_digits(const uint8_t *data, uint32_t len,
                                  uint32_t radix, uint64_t *out)
{
	uint64_t value = 0u;

	if (len == 0u || radix < 2u || radix > 16u) {
		return SITU_WALK_CONSTRAINT;	/* no digits is not the number zero */
	}

	for (uint32_t i = 0u; i < len; i++) {
		const uint8_t byte = data[i];
		uint32_t      digit;

		if (byte >= (uint8_t)'0' && byte <= (uint8_t)'9') {
			digit = (uint32_t)(byte - (uint8_t)'0');
		} else if (byte >= (uint8_t)'a' && byte <= (uint8_t)'f') {
			digit = (uint32_t)(byte - (uint8_t)'a') + 10u;
		} else if (byte >= (uint8_t)'A' && byte <= (uint8_t)'F') {
			digit = (uint32_t)(byte - (uint8_t)'A') + 10u;
		} else {
			return SITU_WALK_CONSTRAINT;
		}

		if (digit >= radix) {
			return SITU_WALK_CONSTRAINT;
		}
		/* Before it happens, not after: a result that got smaller is a wrap
		 * rather than a detection. */
		if (value > (0xffffffffffffffffu - digit) / radix) {
			return SITU_WALK_CONSTRAINT;
		}
		value = value * radix + digit;
	}

	*out = value;
	return SITU_WALK_OK;
}

/* -- varints ------------------------------------------------------------ */

#define VARINT_BIG 0x02u

static const uint8_t *varint_rules(const situ_walk_image *image,
                                   uint32_t index)
{
	return table_row(image->varints, image->varint_count,
	                 image->varint_stride, index);
}

situ_walk_err situ_walk_varint(const situ_walk_image *image,
                               const uint8_t *message, uint32_t len,
                               uint32_t index, uint32_t at,
                               uint32_t *consumed, uint64_t *value)
{
	const uint8_t *rules = varint_rules(image, index);
	if (rules == NULL) {
		return SITU_WALK_UNSUPPORTED;
	}

	const uint32_t max_bytes     = rules[8];
	const uint32_t terminal_bits = rules[9];
	const int      big           = (rules[10] & VARINT_BIG) != 0;

	if (at > len) {
		return SITU_WALK_BOUNDS;
	}
	uint32_t avail = len - at;
	if (avail > max_bytes) {
		avail = max_bytes;
	}

	uint64_t acc = 0u;
	for (uint32_t i = 0u; i < avail; i++) {
		const uint8_t byte = message[at + i];

		if (big) {
			/* The last permitted byte has no continuation bit to spare, so
			 * it is read whole and ends the value. */
			if (terminal_bits == 8u && i + 1u == max_bytes) {
				*consumed = i + 1u;
				*value    = (acc << 8) | byte;
				return SITU_WALK_OK;
			}
			acc = (acc << 7) | (uint64_t)(byte & 0x7fu);
		} else if (i * 7u < 64u) {
			acc |= (uint64_t)(byte & 0x7fu) << (i * 7u);
		}

		if ((byte & 0x80u) == 0u) {
			*consumed = i + 1u;
			*value    = acc;
			return SITU_WALK_OK;
		}
	}

	return SITU_WALK_BOUNDS;	/* the buffer ends mid-varint */
}

/* `image_endian`: 1 big, 2 little. */
#define ENDIAN_BIG    1u
#define ENDIAN_LITTLE 2u

/* Named in the header now, a caller needing the signedness to print a value
 * at all. Kept as the short spellings this file already reads by. */
#define FLAG_OFFSET_KNOWN SITU_WALK_OFFSET_KNOWN
#define FLAG_SIGNED       SITU_WALK_SIGNED

/* The load callback for a size expression: a field of this same message,
 * read at whatever offset the chain has reached.
 *
 * It carries the depth because it is one of the ways the measurement
 * descends: an expression names a field, reading that field may sum the
 * members before it, and one of those may be a run whose elements are
 * structs. A callback that started the count again would be a hole in the
 * bound rather than a bound. */
typedef struct {
	const situ_walk_image *image;
	const uint8_t         *message;
	uint32_t               len;
	uint32_t               shape;
	uint32_t               depth;
} walk_ctx;

static situ_walk_err read_deep(const situ_walk_image *image,
                               const uint8_t *message, uint32_t len,
                               uint32_t shape, uint32_t index,
                               uint32_t depth, uint64_t *out);

static situ_walk_err offset_bits_deep(const situ_walk_image *image,
                                      const uint8_t *message, uint32_t len,
                                      uint32_t shape, uint32_t index,
                                      uint32_t depth, uint32_t *out);

/* How deep a measurement may nest before this build refuses.
 *
 * A `while` run's extent is the sum of its elements' extents, and an element
 * is a struct whose members may include another such run. That is mutual
 * recursion -- `size_bits` to the walk to `struct_extent` and back -- bounded
 * in principle by the schema's nesting and in practice by nothing this
 * program controls, since the schema arrives in an image at run time. On a
 * device the stack is the arena's neighbour, so the depth is a number here
 * rather than a property of the input.
 *
 * Eight, because the deepest nesting in this repository's corpus is three and
 * a walker that refuses a legitimate schema is worse than one that costs a
 * few frames. Refused by name when it is reached, never guessed at. */
#define WALK_DEPTH_MAX 8u

static situ_walk_err size_bits_deep(const situ_walk_image *image,
                                    const uint8_t *message, uint32_t len,
                                    uint32_t shape, uint32_t index,
                                    uint32_t depth, uint32_t *out);

static situ_walk_err ctx_load(void *raw, uint32_t index, int64_t *out)
{
	walk_ctx *ctx = (walk_ctx *)raw;
	uint64_t value = 0u;
	const situ_walk_err err = read_deep(ctx->image, ctx->message, ctx->len,
	                                    ctx->shape, index, ctx->depth,
	                                    &value);
	if (err != SITU_WALK_OK) {
		return err;
	}
	*out = (int64_t)value;
	return SITU_WALK_OK;
}

/* -- variants ------------------------------------------------------------ */

/* `image_arm.arm_flags`: which of the three kinds of arm a row is. */
#define ARM_DEFAULT 0x01u	/* `default:` -- selected by anything unmatched */
#define ARM_ERROR   0x02u	/* `default: error` -- selects nothing at all */

/* The first row of a variant's arms, or NULL where it has none.
 *
 * One row per arm, so a variant has several. They are contiguous because the
 * table is sorted by placement, and the search lands on one of them rather
 * than on the first -- so walk back. Bounded by the table, and by the rows
 * before it belonging to a different placement. */
static const uint8_t *arm_rows(const situ_walk_image *image, uint32_t index,
                               uint32_t *count)
{
	const uint8_t *found = table_row(image->arms, image->arm_count,
	                                 image->arm_stride, index);
	if (found == NULL) {
		return NULL;
	}

	while (found > image->arms
	                && u32_at(found - image->arm_stride) == index) {
		found -= image->arm_stride;
	}

	const uint8_t *last = image->arms + image->arm_count * image->arm_stride;
	uint32_t       n    = 0u;
	while (found + n * image->arm_stride < last
	                && u32_at(found + n * image->arm_stride) == index) {
		n += 1u;
	}

	*count = n;
	return found;
}

/* A variant's extent: the arm the discriminant selects, not the worst case
 * and not the minimum.
 *
 * "It cannot be computed" is often "it is not a constant" (invariant 37). A
 * variant's extent is a switch: read the discriminant this message carries,
 * take the arm it names, and the answer is that arm's size. Using the
 * minimum instead made a dnsname label one byte long and walked thirty-nine
 * of them through a thirty-eight byte buffer.
 *
 * A discriminant naming no arm is nought bytes rather than a refusal. That
 * is a malformed message and saying so is `validate`'s job, not the
 * extent's -- the generated C has the same `: 0u`, and refusing here counted
 * zero dnsname labels where every backend counted one.
 */
static situ_walk_err variant_bits(const situ_walk_image *image,
                                  const uint8_t *message, uint32_t len,
                                  uint32_t shape, uint32_t index,
                                  uint32_t depth, uint32_t *out)
{
	uint32_t       count = 0u;
	const uint8_t *rows  = arm_rows(image, index, &count);
	if (rows == NULL || count == 0u) {
		return SITU_WALK_UNSUPPORTED;
	}
	if (depth >= WALK_DEPTH_MAX) {
		return SITU_WALK_UNSUPPORTED;
	}

	const uint32_t selects = u32_at(rows + 16);
	if (selects == SITU_WALK_NONE) {
		return SITU_WALK_UNSUPPORTED;	/* no discriminant in this image */
	}

	uint64_t value = 0u;
	situ_walk_err err = read_deep(image, message, len, shape, selects,
	                              depth + 1u, &value);
	if (err != SITU_WALK_OK) {
		return err;
	}

	uint32_t fallback = SITU_WALK_NONE;
	for (uint32_t i = 0u; i < count; i++) {
		const uint8_t *row    = rows + i * image->arm_stride;
		const uint32_t chosen = u32_at(row + 4);
		const int64_t  when   = i64_at(row + 8);
		const uint8_t  flags  = row[20];

		if ((flags & ARM_ERROR) != 0u) {
			continue;
		}
		if ((flags & ARM_DEFAULT) != 0u) {
			fallback = chosen;
			continue;
		}
		if ((uint64_t)when == value) {
			if (chosen == SITU_WALK_NONE) {
				*out = 0u;
				return SITU_WALK_OK;
			}
			return size_bits_deep(image, message, len, shape, chosen,
			                      depth + 1u, out);
		}
	}

	if (fallback != SITU_WALK_NONE) {
		return size_bits_deep(image, message, len, shape, fallback,
		                      depth + 1u, out);
	}

	*out = 0u;
	return SITU_WALK_OK;
}

/* -- `while` runs -------------------------------------------------------- */


/* How many bytes one instance of a struct occupies, from its own bytes.
 *
 * A fixed struct answers from the image; anything else is the sum of what its
 * members turn out to be, which is what makes a run of them walkable rather
 * than indexable. A located member contributes nothing -- it says where it is
 * and joins no chain.
 *
 * Zero is an answer rather than a refusal. A `name` whose first label does not
 * fit holds no labels and is zero bytes long. The guard against a zero extent
 * belongs where it stops something, which is the walk below. */
static situ_walk_err struct_extent(const situ_walk_image *image,
                                   const uint8_t *message, uint32_t len,
                                   uint32_t shape, uint32_t depth,
                                   uint32_t *out)
{
	if (depth >= WALK_DEPTH_MAX) {
		return SITU_WALK_UNSUPPORTED;
	}
	if (shape >= image->struct_count) {
		return SITU_WALK_BOUNDS;
	}

	const uint8_t *entry = image->structs + shape * image->struct_stride;
	uint32_t       first = 0u;
	uint32_t       count = 0u;
	situ_walk_err  err   = situ_walk_members(image, shape, &first, &count);
	if (err != SITU_WALK_OK) {
		return err;
	}

	/* Byte 8 of an `image_struct` is its own fixed size in bits, and
	 * SITU_WALK_NONE where it has none. */
	const uint32_t fixed = u32_at(entry + 8);
	if (fixed != SITU_WALK_NONE) {
		*out = (fixed + 7u) / 8u;
		return SITU_WALK_OK;
	}

	uint32_t total = 0u;
	for (uint32_t i = 0u; i < count; i++) {
		situ_walk_placement held;
		err = situ_walk_placement_at(image, first + i, &held);
		if (err != SITU_WALK_OK) {
			return err;
		}
		if (held.located_code != SITU_WALK_NONE) {
			continue;
		}

		uint32_t wide = 0u;
		err = size_bits_deep(image, message, len, shape, first + i,
		                     depth + 1u, &wide);
		if (err != SITU_WALK_OK) {
			return err;
		}
		if (wide > 0xffffffffu - total) {
			return SITU_WALK_BOUNDS;
		}
		total += wide;
	}

	*out = (total + 7u) / 8u;
	return SITU_WALK_OK;
}

/* How many elements a `while` run holds, and how many bytes they occupy.
 *
 * THE LOOP STOPS FOUR WAYS, and two of them are independent of the message:
 *
 *   - `count` reaches the cap, which is the schema's `max` or 0xFFFF. A
 *     ceiling that does not depend on the bytes at all.
 *   - `at` reaches the frame. Every iteration advances `at` by at least one
 *     byte, because a zero extent breaks below, so this alone bounds the
 *     loop by the length of the message.
 *   - an element does not fit, or measures zero. An element whose bytes the
 *     frame does not hold is not a short element, it is not an element --
 *     every backend answers zero for netlink's first attribute, which
 *     declares a length past the end of the buffer. And a record whose
 *     members are all delimited and all empty occupies nothing, which is
 *     invariant 24's denial of service rather than a wrong answer.
 *   - the predicate is false, which is the construct's own reason to stop.
 *
 * The predicate is asked about the element *just parsed*, which is the whole
 * difference from `until`: `until` asks about the position before an element
 * and `while` about the one behind it.
 */
static situ_walk_err while_walk(const situ_walk_image *image,
                                const uint8_t *message, uint32_t len,
                                uint32_t shape, uint32_t index,
                                uint32_t depth, uint32_t *count,
                                uint32_t *bytes)
{
	situ_walk_placement held;
	situ_walk_err err = situ_walk_placement_at(image, index, &held);
	if (err != SITU_WALK_OK) {
		return err;
	}
	if (held.repeat_code == SITU_WALK_NONE
	                || held.type_struct == SITU_WALK_NONE) {
		return SITU_WALK_UNSUPPORTED;	/* not a `while` run */
	}
	if (depth >= WALK_DEPTH_MAX) {
		return SITU_WALK_UNSUPPORTED;
	}

	uint32_t start = 0u;
	err = offset_bits_deep(image, message, len, shape, index, depth,
	                       &start);
	if (err != SITU_WALK_OK) {
		return err;
	}
	if (start % 8u) {
		return SITU_WALK_UNSUPPORTED;
	}
	start /= 8u;
	if (start > len) {
		return SITU_WALK_BOUNDS;
	}

	const uint32_t cap = (held.repeat_cap != 0u) ? held.repeat_cap : 0xffffu;
	uint32_t       at  = start;
	uint32_t       n   = 0u;

	while (n < cap && at < len) {
		uint32_t extent = 0u;
		err = struct_extent(image, message + at, len - at, held.type_struct,
		                    depth + 1u, &extent);
		/* Two different things wear one error here, and folding them
		 * together is how a walk answers a wrong length confidently. "This
		 * build cannot measure that element" is a limit of the walker and
		 * has to propagate: absorbing it would report the run as ending
		 * where the measurement stopped, which is a short extent that reads
		 * exactly like a right one. Anything else is the data running out,
		 * and the elements that fit still count. */
		if (err == SITU_WALK_UNSUPPORTED) {
			return err;
		}
		if (err != SITU_WALK_OK) {
			break;
		}
		if (extent == 0u || extent > len - at) {
			break;
		}

		n  += 1u;
		at += extent;

		/* The predicate reads the element just parsed, so the context is
		 * that element's frame rather than this struct's, and it descends
		 * one level -- which is what the depth has to say for the bound to
		 * mean anything. */
		const uint8_t *from = message + at - extent;
		const uint32_t left = len - at + extent;

		walk_ctx ctx  = {image, from, left, held.type_struct, depth + 1u};
		int64_t  more = 0;
		err = situ_walk_eval(image, held.repeat_code, ctx_load, &ctx,
		                     (int64_t)left, &more);
		if (err != SITU_WALK_OK || more == 0) {
			break;
		}
	}

	*count = n;
	*bytes = at - start;
	return SITU_WALK_OK;
}

situ_walk_err situ_walk_size_bits(const situ_walk_image *image,
                                  const uint8_t *message, uint32_t len,
                                  uint32_t shape, uint32_t index,
                                  uint32_t *out)
{
	return size_bits_deep(image, message, len, shape, index, 0u, out);
}

static situ_walk_err size_bits_deep(const situ_walk_image *image,
                                    const uint8_t *message, uint32_t len,
                                    uint32_t shape, uint32_t index,
                                    uint32_t depth, uint32_t *out)
{
	situ_walk_placement held;
	situ_walk_err err = situ_walk_placement_at(image, index, &held);
	if (err != SITU_WALK_OK) {
		return err;
	}

	/* A variant's extent is the arm the discriminant selects. Before the
	 * branches below, as `walk.py` has it: an arm is not a member of the
	 * struct holding the variant, and asking the chain to place one walks
	 * back into the variant whose extent is that arm's. */
	{
		uint32_t arms = 0u;
		if (arm_rows(image, index, &arms) != NULL && arms > 0u) {
			return variant_bits(image, message, len, shape, index, depth,
			                    out);
		}
	}

	/* A `while` run's extent is however far the walk got. Falling through to
	 * the record's `size_bits` gives the *minimum* -- one element -- so a
	 * struct holding one measured a byte where it held two. */
	if (held.repeat_code != SITU_WALK_NONE) {
		if (held.type_struct == SITU_WALK_NONE) {
			return SITU_WALK_UNSUPPORTED;	/* a variant arm, not a run */
		}
		uint32_t count = 0u;
		uint32_t bytes = 0u;
		err = while_walk(image, message, len, shape, index, depth,
		                 &count, &bytes);
		if (err != SITU_WALK_OK) {
			return err;
		}
		*out = bytes * 8u;
		return SITU_WALK_OK;
	}

	/* A varint's width is in its own bytes. The record's `size_bits` is a
	 * lower bound, and taking it would place everything after one at the
	 * wrong offset. */
	if (varint_rules(image, index) != NULL) {
		uint32_t at = 0u;
		err = offset_bits_deep(image, message, len, shape, index, depth,
		                       &at);
		if (err != SITU_WALK_OK) {
			return err;
		}
		uint32_t consumed = 0u;
		uint64_t value    = 0u;
		err = situ_walk_varint(image, message, len, index, at / 8u,
		                       &consumed, &value);
		if (err == SITU_WALK_BOUNDS) {
			/* A truncated one is zero bytes wide, not a refusal: that is
			 * what the generated `_len` answers, and it keeps every offset
			 * derived from it inside the frame. Refusing here dropped every
			 * member after a varint whose last byte never arrived, out of a
			 * struct four backends read to the end. The *value* still
			 * refuses -- two readers, as for a text number. */
			*out = 0u;
			return SITU_WALK_OK;
		}
		if (err != SITU_WALK_OK) {
			return err;
		}
		*out = consumed * 8u;
		return SITU_WALK_OK;
	}

	/* A delimited member ends where its delimiter starts, and the *member*
	 * ends where the delimiter does -- or the member after it would begin on
	 * the terminator. Where the delimiter is absent there is none to add and
	 * the member reaches as far as it got. `type_struct` separates the two
	 * uses of `until`: this is a member ending at the first occurrence
	 * anywhere, not a run of records checked at each element boundary.
	 *
	 * Taking the record's `size_bits` instead gives the *delimiter's* width
	 * -- a true lower bound, and the one number that is not the answer -- so
	 * a `u16` after `verb[] until " "` was read at offset 1. */
	/* A text number's width is its digits, not its value's. `decimal u32
	 * n[4]` is four bytes holding one number, and the sized-run branch below
	 * reads `[4]` as four 32-bit elements and answers sixteen -- which is
	 * what put `edges`' `text_driver` tail twelve bytes past where every
	 * backend places it, in the Python walker, and was invisible here
	 * because the solver hands a member after a fixed-width text number a
	 * constant offset. The delimited form is measured by its scan below;
	 * this is the fixed-width one, whose digit count the image carries. */
	if (held.radix != 0u && held.radix_digits != 0u
	                && delimiter_rules(image, index) == NULL) {
		*out = (uint32_t)held.radix_digits * 8u;
		return SITU_WALK_OK;
	}

	const uint8_t *delim = (held.type_struct == SITU_WALK_NONE)
	                     ? delimiter_rules(image, index) : NULL;
	if (delim != NULL) {
		uint32_t at = 0u;
		err = offset_bits_deep(image, message, len, shape, index, depth,
		                       &at);
		if (err != SITU_WALK_OK) {
			return err;
		}

		uint32_t content    = 0u;
		int      terminated = 0;
		err = situ_walk_scan(image, message, len, index, at / 8u,
		                     &content, &terminated);
		if (err != SITU_WALK_OK) {
			return err;
		}

		*out = (content + (terminated ? delim[16] : 0u)) * 8u;
		return SITU_WALK_OK;
	}

	if (held.size_code == SITU_WALK_NONE) {
		*out = held.size_bits;
		return SITU_WALK_OK;
	}

	/* A sized run: the program answers a count of elements, and the
	 * element width is what each costs. */
	if (held.element_bits == SITU_WALK_NONE) {
		return SITU_WALK_UNSUPPORTED;
	}

	walk_ctx ctx = {image, message, len, shape, depth};
	int64_t  count = 0;
	err = situ_walk_eval(image, held.size_code, ctx_load, &ctx,
	                     (int64_t)len, &count);
	if (err != SITU_WALK_OK) {
		return err;
	}
	if (count < 0 || (uint64_t)count > 0xffffffffu / held.element_bits) {
		return SITU_WALK_BOUNDS;
	}

	*out = (uint32_t)count * held.element_bits;
	return SITU_WALK_OK;
}

situ_walk_err situ_walk_offset_bits(const situ_walk_image *image,
                                    const uint8_t *message, uint32_t len,
                                    uint32_t shape, uint32_t index,
                                    uint32_t *out)
{
	return offset_bits_deep(image, message, len, shape, index, 0u, out);
}

static situ_walk_err offset_bits_deep(const situ_walk_image *image,
                                      const uint8_t *message, uint32_t len,
                                      uint32_t shape, uint32_t index,
                                      uint32_t depth, uint32_t *out)
{
	situ_walk_placement held;
	situ_walk_err err = situ_walk_placement_at(image, index, &held);
	if (err != SITU_WALK_OK) {
		return err;
	}

	/* `at expr` says where the member is rather than joining the chain, so
	 * the program answers the offset outright. The loop below already knows
	 * to skip one when summing what comes before: a located member
	 * contributes nothing to the members after it, which is the whole of what
	 * separates it from an ordinary one. */
	if (held.located_code != SITU_WALK_NONE) {
		walk_ctx ctx = {image, message, len, shape, depth};
		int64_t  at  = 0;

		err = situ_walk_eval(image, held.located_code, ctx_load, &ctx,
		                     (int64_t)len, &at);
		if (err != SITU_WALK_OK) {
			return err;
		}
		if (at < 0 || (uint64_t)at > 0xffffffffu / 8u) {
			return SITU_WALK_BOUNDS;
		}
		*out = (uint32_t)at * 8u;
		return SITU_WALK_OK;
	}
	if ((held.flags & FLAG_OFFSET_KNOWN) != 0u
	                && held.offset_bits != SITU_WALK_NONE) {
		*out = held.offset_bits;
		return SITU_WALK_OK;
	}

	/* Sum what comes before. The loop is bounded by the struct's member
	 * count, which the image states, so this needs no guard of its own. */
	uint32_t first = 0u;
	uint32_t count = 0u;
	err = situ_walk_members(image, shape, &first, &count);
	if (err != SITU_WALK_OK) {
		return err;
	}

	uint32_t total = 0u;
	for (uint32_t i = 0u; i < count; i++) {
		const uint32_t before = first + i;
		if (before == index) {
			*out = total;
			return SITU_WALK_OK;
		}

		situ_walk_placement earlier;
		err = situ_walk_placement_at(image, before, &earlier);
		if (err != SITU_WALK_OK) {
			return err;
		}
		/* A located member joins no offset chain: it says where it is. */
		if (earlier.located_code != SITU_WALK_NONE) {
			continue;
		}

		uint32_t wide = 0u;
		err = size_bits_deep(image, message, len, shape, before, depth,
		                     &wide);
		if (err != SITU_WALK_OK) {
			return err;
		}
		if (wide > 0xffffffffu - total) {
			return SITU_WALK_BOUNDS;
		}
		total += wide;
	}

	return SITU_WALK_BOUNDS;	/* not a member of this struct */
}

/* One value of `width_bits` bits at `start_bits`, in the member's own terms.
 *
 * Split out because an element of a run is the same read at a different
 * offset, and two spellings of "how do these bits become a number" is how a
 * backend and its own run accessor once disagreed. The width is a parameter
 * rather than the placement's, which is the whole of the difference: an
 * element is as wide as an element, and sign extension follows it.
 *
 * A member that does not start or end on a byte is refused rather than
 * assembled. The layout solver will not place a bit-packed field at a
 * dynamic offset, so the case this declines to render is one that cannot
 * arise -- and a walker that guessed at it would be inventing an answer for
 * a construct that has none. */
static situ_walk_err read_at(const uint8_t *message, uint32_t len,
                             const situ_walk_placement *held,
                             uint32_t start_bits, uint32_t width_bits,
                             uint64_t *out)
{
	if (start_bits % 8u || width_bits % 8u || width_bits == 0u
	                || width_bits > 64u) {
		return SITU_WALK_UNSUPPORTED;
	}

	const uint32_t at    = start_bits / 8u;
	const uint32_t width = width_bits / 8u;
	if (at > len || width > len - at) {
		return SITU_WALK_BOUNDS;
	}

	uint64_t value = 0u;
	if (held->endian == ENDIAN_LITTLE) {
		for (uint32_t i = width; i > 0u; i--) {
			value = (value << 8) | message[at + i - 1u];
		}
	} else if (held->endian == ENDIAN_BIG) {
		for (uint32_t i = 0u; i < width; i++) {
			value = (value << 8) | message[at + i];
		}
	} else {
		/* `native` has no answer a walker can give: the capture and the
		 * machine reading it are different machines. */
		return SITU_WALK_UNSUPPORTED;
	}

	if ((held->flags & FLAG_SIGNED) != 0u && width < 8u) {
		const uint64_t sign = (uint64_t)1 << (width_bits - 1u);
		if (value & sign) {
			value |= ~(((uint64_t)1 << width_bits) - 1u);
		}
	}

	*out = value;
	return SITU_WALK_OK;
}

situ_walk_err situ_walk_read(const situ_walk_image *image,
                             const uint8_t *message, uint32_t len,
                             uint32_t shape, uint32_t index, uint64_t *out)
{
	return read_deep(image, message, len, shape, index, 0u, out);
}

static situ_walk_err read_deep(const situ_walk_image *image,
                               const uint8_t *message, uint32_t len,
                               uint32_t shape, uint32_t index,
                               uint32_t depth, uint64_t *out)
{
	situ_walk_placement held;
	situ_walk_err err = situ_walk_placement_at(image, index, &held);
	if (err != SITU_WALK_OK) {
		return err;
	}

	/* A text number is digits, not bits, and it comes first for the same
	 * reason `traverse.classify` puts it before the array branch: `decimal
	 * u16 code[3]` is one number in three digits, so the run refusal below
	 * would decline it -- the right answer for the wrong reason, since its
	 * digit count is not a count of numbers. `validate` is where the declared
	 * domain is asked; this answers what the field holds. */
	if (held.radix != 0u) {
		uint32_t at    = 0u;
		uint32_t width = 0u;

		err = offset_bits_deep(image, message, len, shape, index, depth, &at);
		if (err != SITU_WALK_OK) {
			return err;
		}
		err = size_bits_deep(image, message, len, shape, index, depth, &width);
		if (err != SITU_WALK_OK) {
			return err;
		}
		if (at % 8u || width % 8u) {
			return SITU_WALK_UNSUPPORTED;
		}

		/* The digits, which for the delimited form stop where the scan
		 * stopped: its *span* carries the delimiter, and a delimiter is not
		 * a digit of any radix. */
		at /= 8u;
		width /= 8u;
		if (delimiter_rules(image, index) != NULL) {
			int terminated = 0;
			err = situ_walk_scan(image, message, len, index, at,
			                     &width, &terminated);
			if (err != SITU_WALK_OK) {
				return err;
			}
		}
		if (at > len || width > len - at) {
			return SITU_WALK_BOUNDS;
		}
		return parse_digits(message + at, width, held.radix, out);
	}

	/* A run has no single value, and neither does a member whose width the
	 * data decides. Declined explicitly: a number returned for one of those
	 * is a wrong answer that reads exactly like a right one. */
	if (held.size_code != SITU_WALK_NONE
	                || held.array_count != SITU_WALK_NONE
	                || held.repeat_code != SITU_WALK_NONE) {
		return SITU_WALK_UNSUPPORTED;
	}

	uint32_t offset = 0u;
	err = offset_bits_deep(image, message, len, shape, index, depth, &offset);
	if (err != SITU_WALK_OK) {
		return err;
	}

	/* A varint *is* a scalar, and its value is what it encodes rather than
	 * the bytes it is written in. The read below would take `size_bits`,
	 * which for a varint is the one-byte lower bound: `ac 02` answered 172
	 * where leb128 says 300, and a one-byte encoding answered correctly by
	 * coincidence, which is how this survived a differential. Decoding is
	 * what every compiled backend's `_get` does. */
	if (varint_rules(image, index) != NULL) {
		uint32_t consumed = 0u;
		if (offset % 8u) {
			return SITU_WALK_UNSUPPORTED;
		}
		return situ_walk_varint(image, message, len, index, offset / 8u,
		                        &consumed, out);
	}

	/* A delimited member is a byte run whose end the data decides, so it has
	 * no more of a value than a counted run has. Reading `size_bits` gave the
	 * delimiter's width and answered `"GET "` as 0x47; the Python walker read
	 * the whole span and answered 1195725856. Both are the same mistake, and
	 * the answer to it is that neither is a number this returns. */
	if (held.type_struct == SITU_WALK_NONE
	                && delimiter_rules(image, index) != NULL) {
		return SITU_WALK_UNSUPPORTED;
	}

	return read_at(message, len, &held, offset, held.size_bits, out);
}

situ_walk_err situ_walk_count(const situ_walk_image *image,
                              const uint8_t *message, uint32_t len,
                              uint32_t shape, uint32_t index,
                              uint32_t *out)
{
	situ_walk_placement held;
	situ_walk_err err = situ_walk_placement_at(image, index, &held);
	if (err != SITU_WALK_OK) {
		return err;
	}

	/* A text number's bracket is digits, not elements: `decimal u32 n[4]` is
	 * one number and asking it for four would be the array reading of the
	 * bracket that `traverse.classify` exists to stop. */
	if (held.radix != 0u) {
		return SITU_WALK_UNSUPPORTED;
	}
	/* A `while` run's count is whichever element first fails the predicate,
	 * which is a walk this build does not have. Named rather than
	 * approximated. */
	if (held.repeat_code != SITU_WALK_NONE) {
		return SITU_WALK_UNSUPPORTED;
	}

	if (held.array_count != SITU_WALK_NONE) {
		*out = held.array_count;
		return SITU_WALK_OK;
	}
	if (held.size_code == SITU_WALK_NONE) {
		return SITU_WALK_UNSUPPORTED;	/* not a run */
	}

	walk_ctx ctx   = {image, message, len, shape, 0u};
	int64_t  count = 0;
	err = situ_walk_eval(image, held.size_code, ctx_load, &ctx,
	                     (int64_t)len, &count);
	if (err != SITU_WALK_OK) {
		return err;
	}
	if (count < 0 || (uint64_t)count > 0xffffffffu) {
		return SITU_WALK_BOUNDS;
	}

	*out = (uint32_t)count;
	return SITU_WALK_OK;
}

situ_walk_err situ_walk_element(const situ_walk_image *image,
                               const uint8_t *message, uint32_t len,
                               uint32_t shape, uint32_t index,
                               uint32_t at, uint64_t *out)
{
	situ_walk_placement held;
	situ_walk_err err = situ_walk_placement_at(image, index, &held);
	if (err != SITU_WALK_OK) {
		return err;
	}
	if (held.element_bits == SITU_WALK_NONE || held.element_bits == 0u) {
		return SITU_WALK_UNSUPPORTED;
	}

	uint32_t count = 0u;
	err = situ_walk_count(image, message, len, shape, index, &count);
	if (err != SITU_WALK_OK) {
		return err;
	}
	if (at >= count) {
		return SITU_WALK_BOUNDS;
	}

	uint32_t start = 0u;
	err = situ_walk_offset_bits(image, message, len, shape, index, &start);
	if (err != SITU_WALK_OK) {
		return err;
	}
	if (at > 0xffffffffu / held.element_bits) {
		return SITU_WALK_BOUNDS;
	}
	const uint32_t into = at * held.element_bits;
	if (into > 0xffffffffu - start) {
		return SITU_WALK_BOUNDS;
	}

	return read_at(message, len, &held, start + into, held.element_bits, out);
}

/* -- validate ------------------------------------------------------------ */

/* `image_check`: what one constraint row asks. The numbering is the packer's
 * and the order rows appear in is the order they are asked, the first
 * failure being the answer. */
#define CHECK_MUST_EQ        0u
#define CHECK_MINIMUM        1u
#define CHECK_MAXIMUM        2u
#define CHECK_MUST_BE_ZERO   3u
#define CHECK_MUST_BE_ONE    4u
#define CHECK_ENUM_KNOWN     5u
#define CHECK_FITS_FRAME     6u
#define CHECK_DIGITS_VALID   9u
#define CHECK_DIGITS_MINIMAL 10u

/* `image_struct.struct_flags` bit 0: whether the image carries *every* check
 * this struct needs. The packer sets it, and a walker that ignored it would
 * report OK for a struct whose rules it was never given. */
#define STRUCT_VALIDATABLE 0x01u

/* The first constraint row for a member, or NULL. Contiguous, like the arms
 * table and for the same reason. */
static const uint8_t *check_rows(const situ_walk_image *image, uint32_t index,
                                 uint32_t *count)
{
	const uint8_t *found = table_row(image->constraints,
	                                 image->constraint_count,
	                                 image->constraint_stride, index);
	if (found == NULL) {
		*count = 0u;
		return NULL;
	}

	while (found > image->constraints
	                && u32_at(found - image->constraint_stride) == index) {
		found -= image->constraint_stride;
	}

	const uint8_t *last = image->constraints
	                    + image->constraint_count * image->constraint_stride;
	uint32_t       n    = 0u;
	while (found + n * image->constraint_stride < last
	                && u32_at(found + n * image->constraint_stride) == index) {
		n += 1u;
	}

	*count = n;
	return found;
}

static int enum_admits(const situ_walk_image *image, uint32_t which,
                       int64_t value)
{
	for (uint32_t i = 0u; i < image->enum_value_count; i++) {
		const uint8_t *row = image->enum_values + i * image->enum_value_stride;
		if (u32_at(row) == which && i64_at(row + 4) == value) {
			return 1;
		}
	}
	return 0;
}

static situ_walk_err validate_deep(const situ_walk_image *image,
                                   const uint8_t *message, uint32_t len,
                                   uint32_t shape, uint32_t depth,
                                   situ_walk_err *verdict);

situ_walk_err situ_walk_validate(const situ_walk_image *image,
                                 const uint8_t *message, uint32_t len,
                                 uint32_t shape, situ_walk_err *verdict)
{
	return validate_deep(image, message, len, shape, 0u, verdict);
}

static situ_walk_err validate_deep(const situ_walk_image *image,
                                   const uint8_t *message, uint32_t len,
                                   uint32_t shape, uint32_t depth,
                                   situ_walk_err *verdict)
{
	if (shape >= image->struct_count) {
		return SITU_WALK_BOUNDS;
	}
	if (depth >= WALK_DEPTH_MAX) {
		return SITU_WALK_UNSUPPORTED;
	}

	/* The image says whether it carries every check for this struct. Where
	 * it does not, there is no partial answer to give: `validate` is one
	 * verdict about a whole struct, and a partial one reports OK for the
	 * rules it happened to be given. */
	const uint8_t *entry = image->structs + shape * image->struct_stride;
	if ((u32_at(entry + 12) & STRUCT_VALIDATABLE) == 0u) {
		return SITU_WALK_UNSUPPORTED;
	}

	/* The frame has to hold the struct's own minimum before anything in it
	 * is placed -- section 20.2's check, the one every constant-offset
	 * access below it depends on, and what the Python walk does when it
	 * acquires a view. Without it the two agreed about every well-formed
	 * message and disagreed about short ones, C answering for the members
	 * that happened to fit. */
	const uint32_t fixed = u32_at(entry + 8);
	if (fixed != SITU_WALK_NONE && len < (fixed + 7u) / 8u) {
		*verdict = SITU_WALK_BOUNDS;
		return SITU_WALK_OK;
	}

	uint32_t first = 0u;
	uint32_t count = 0u;
	situ_walk_err err = situ_walk_members(image, shape, &first, &count);
	if (err != SITU_WALK_OK) {
		return err;
	}

	for (uint32_t i = 0u; i < count; i++) {
		const uint32_t index = first + i;
		situ_walk_placement held;

		err = situ_walk_placement_at(image, index, &held);
		if (err != SITU_WALK_OK) {
			return err;
		}

		uint32_t rows = 0u;
		const uint8_t *checks = check_rows(image, index, &rows);

		/* A kind of check this build does not render makes the whole
		 * struct unanswerable rather than the member unchecked. Skipping
		 * one would answer OK for a message that breaks it, which is the
		 * one wrong answer indistinguishable from a right one. */
		for (uint32_t c = 0u; c < rows; c++) {
			const uint8_t kind = checks[c * image->constraint_stride + 12];
			if (kind != CHECK_MUST_EQ && kind != CHECK_MINIMUM
			                && kind != CHECK_MAXIMUM
			                && kind != CHECK_MUST_BE_ZERO
			                && kind != CHECK_MUST_BE_ONE
			                && kind != CHECK_ENUM_KNOWN
			                && kind != CHECK_FITS_FRAME
			                && kind != CHECK_DIGITS_VALID
			                && kind != CHECK_DIGITS_MINIMAL) {
				return SITU_WALK_UNSUPPORTED;
			}
		}

		/* A `[since]` member is there only in a message whose own version
		 * reaches it, which is a rule this build does not have -- and a
		 * field that is not there is not a field that is wrong, so guessing
		 * would answer for bytes the message never claimed to carry. */
		if (u16_at(image->placements + index * image->placement_stride + 44)
		                != 0u) {
			return SITU_WALK_UNSUPPORTED;
		}

		/* One nested member, not a run of them and not a variant: a run gets
		 * the repeated check rather than the nested one, and recursing into
		 * it would validate element zero as though it were the member. */
		uint32_t on_arms = 0u;
		const int nested = (held.type_struct != SITU_WALK_NONE
		                    && held.repeat_code == SITU_WALK_NONE
		                    && held.array_count == SITU_WALK_NONE
		                    && held.size_code == SITU_WALK_NONE
		                    && arm_rows(image, index, &on_arms) == NULL);

		/* Every member is *placed*, not only the constrained ones: a struct
		 * whose later members the frame does not reach is BOUNDS before any
		 * constraint is asked. */
		uint32_t at   = 0u;
		uint32_t wide = 0u;
		err = situ_walk_offset_bits(image, message, len, shape, index, &at);
		if (err == SITU_WALK_UNSUPPORTED) {
			return err;
		}
		if (err != SITU_WALK_OK) {
			*verdict = SITU_WALK_BOUNDS;
			return SITU_WALK_OK;
		}
		err = situ_walk_size_bits(image, message, len, shape, index, &wide);
		if (err == SITU_WALK_UNSUPPORTED) {
			return err;
		}
		if (err != SITU_WALK_OK) {
			*verdict = SITU_WALK_BOUNDS;
			return SITU_WALK_OK;
		}
		if (at / 8u > len || (wide + 7u) / 8u > len - at / 8u) {
			*verdict = SITU_WALK_BOUNDS;
			return SITU_WALK_OK;
		}

		/* A nested member is `validate` called through, and its verdict is
		 * returned as it stands rather than folded into CONSTRAINT: the
		 * inner code is what the generated C propagates. Without this the
		 * enclosing struct validated nothing at all -- a header whose own
		 * version field was wrong parsed clean. */
		if (nested) {
			situ_walk_err inner = SITU_WALK_OK;
			err = validate_deep(image, message + at / 8u, len - at / 8u,
			                    held.type_struct, depth + 1u, &inner);
			if (err != SITU_WALK_OK) {
				return err;
			}
			if (inner != SITU_WALK_OK) {
				*verdict = inner;
				return SITU_WALK_OK;
			}
			continue;
		}

		for (uint32_t c = 0u; c < rows; c++) {
			const uint8_t *row   = checks + c * image->constraint_stride;
			const int64_t  want  = i64_at(row + 4);
			const uint8_t  kind  = row[12];

			if (kind == CHECK_FITS_FRAME) {
				/* The accessor clamps; this is where a message declaring
				 * more than it carries is called malformed, and it answers
				 * BOUNDS rather than CONSTRAINT. */
				uint32_t held_bits = 0u;
				err = situ_walk_size_bits(image, message, len, shape, index,
				                          &held_bits);
				if (err != SITU_WALK_OK) {
					return err;
				}
				if ((held_bits + 7u) / 8u > len - at / 8u) {
					*verdict = SITU_WALK_BOUNDS;
					return SITU_WALK_OK;
				}
				continue;
			}

			uint64_t value = 0u;
			err = situ_walk_read(image, message, len, shape, index, &value);
			if (err == SITU_WALK_UNSUPPORTED) {
				return err;
			}
			if (err != SITU_WALK_OK) {
				*verdict = (kind == CHECK_DIGITS_VALID
				            || kind == CHECK_DIGITS_MINIMAL)
				         ? SITU_WALK_CONSTRAINT : SITU_WALK_BOUNDS;
				return SITU_WALK_OK;
			}

			int broken = 0;
			switch (kind) {
			case CHECK_MUST_EQ:
				broken = ((int64_t)value != want);
				break;
			case CHECK_MINIMUM:
				broken = ((int64_t)value < want);
				break;
			case CHECK_MAXIMUM:
				broken = ((int64_t)value > want);
				break;
			case CHECK_MUST_BE_ZERO:
				broken = (value != 0u);
				break;
			case CHECK_MUST_BE_ONE:
				broken = ((int64_t)value != want);
				break;
			case CHECK_ENUM_KNOWN:
				broken = !enum_admits(image, (uint32_t)want, (int64_t)value);
				break;
			case CHECK_DIGITS_VALID:
				/* The read has already parsed the digits; what is left is
				 * the declared domain the row carries. */
				broken = (value > (uint64_t)want);
				break;
			case CHECK_DIGITS_MINIMAL:
				broken = 0;	/* the spelling, checked below */
				break;
			default:
				return SITU_WALK_UNSUPPORTED;
			}

			if (kind == CHECK_DIGITS_MINIMAL) {
				const uint8_t *digits = NULL;
				uint32_t       count_of = 0u;
				err = situ_walk_bytes(image, message, len, shape, index,
				                      &digits, &count_of);
				if (err == SITU_WALK_UNSUPPORTED) {
					return err;
				}
				if (err != SITU_WALK_OK) {
					*verdict = SITU_WALK_CONSTRAINT;
					return SITU_WALK_OK;
				}
				if (count_of == 0u
				                || (count_of > 1u && digits[0] == (uint8_t)'0')) {
					broken = 1;
				}
				for (uint32_t d = 0u; want > 10 && d < count_of; d++) {
					if (digits[d] >= (uint8_t)'A' && digits[d] <= (uint8_t)'F') {
						broken = 1;
					}
				}
			}

			if (broken) {
				*verdict = SITU_WALK_CONSTRAINT;
				return SITU_WALK_OK;
			}
		}
	}

	*verdict = SITU_WALK_OK;
	return SITU_WALK_OK;
}

situ_walk_err situ_walk_bytes(const situ_walk_image *image,
                              const uint8_t *message, uint32_t len,
                              uint32_t shape, uint32_t index,
                              const uint8_t **out, uint32_t *count)
{
	uint32_t start = 0u;
	uint32_t width = 0u;
	situ_walk_err err = situ_walk_offset_bits(image, message, len, shape,
	                                          index, &start);
	if (err != SITU_WALK_OK) {
		return err;
	}
	err = situ_walk_size_bits(image, message, len, shape, index, &width);
	if (err != SITU_WALK_OK) {
		return err;
	}
	if (start % 8u || width % 8u) {
		return SITU_WALK_UNSUPPORTED;	/* a run that does not start on a byte */
	}

	start /= 8u;
	width /= 8u;
	if (start > len || width > len - start) {
		return SITU_WALK_BOUNDS;
	}

	*out   = message + start;
	*count = width;
	return SITU_WALK_OK;
}

/* -- the expression evaluator ------------------------------------------- */

#define OP_END 0x00u
#define OP_PUSH 0x01u
#define OP_FIELD 0x02u
#define OP_REMAINING 0x03u
#define OP_SIZE 0x04u
#define OP_OFFSET 0x05u
#define OP_COUNT 0x06u
#define OP_ARG_FIELD 0x07u

#define STACK_DEPTH 32u

situ_walk_err situ_walk_eval(const situ_walk_image *image, uint32_t at,
                             situ_walk_load load, void *ctx,
                             int64_t remaining, int64_t *out)
{
	int64_t  stack[STACK_DEPTH];
	unsigned depth = 0u;
	uint32_t pc    = at;

	/* The loop terminates because the program counter only ever advances
	 * and the program has a length: that is section 10 being total, and it
	 * is why there is no step limit here (0026). */
	while (pc < image->code_len) {
		const uint8_t op = image->code[pc];
		pc++;

		if (op == OP_END) {
			if (depth != 1u) {
				return SITU_WALK_MALFORMED;
			}
			*out = stack[0];
			return SITU_WALK_OK;
		}

		if (op == OP_PUSH) {
			if (pc + 8u > image->code_len || depth >= STACK_DEPTH) {
				return SITU_WALK_MALFORMED;
			}
			stack[depth++] = i64_at(image->code + pc);
			pc += 8u;
			continue;
		}

		if (op == OP_FIELD) {
			if (pc + 4u > image->code_len || depth >= STACK_DEPTH) {
				return SITU_WALK_MALFORMED;
			}
			int64_t value = 0;
			if (load == NULL) {
				return SITU_WALK_UNSUPPORTED;
			}
			const situ_walk_err err = load(ctx, u32_at(image->code + pc),
			                               &value);
			if (err != SITU_WALK_OK) {
				return err;
			}
			stack[depth++] = value;
			pc += 4u;
			continue;
		}

		if (op == OP_REMAINING) {
			if (depth >= STACK_DEPTH) {
				return SITU_WALK_MALFORMED;
			}
			stack[depth++] = remaining;
			continue;
		}

		/* `size`, `offset`, `count` and `arg_field` need the walk this
		 * build does not have yet. Refused by name rather than guessed. */
		if (op == OP_SIZE || op == OP_OFFSET || op == OP_COUNT
		                || op == OP_ARG_FIELD) {
			return SITU_WALK_UNSUPPORTED;
		}

		if (op == 0x1au || op == 0x1bu) {		/* NEG, NOT */
			if (depth < 1u) {
				return SITU_WALK_MALFORMED;
			}
			stack[depth - 1u] = (op == 0x1au) ? -stack[depth - 1u]
			                                  : ~stack[depth - 1u];
			continue;
		}

		if (depth < 2u) {
			return SITU_WALK_MALFORMED;
		}
		const int64_t right = stack[--depth];
		const int64_t left  = stack[depth - 1u];
		int64_t answer      = 0;

		switch (op) {
		case 0x10u: answer = left + right; break;
		case 0x11u: answer = left - right; break;
		case 0x12u: answer = left * right; break;
		case 0x13u:
			/* Division by zero raises rather than answering zero: a length
			 * computed from an unchecked division is a buffer overrun in
			 * whatever reads it next. */
			if (right == 0) {
				return SITU_WALK_MALFORMED;
			}
			answer = left / right;
			break;
		case 0x14u:
			if (right == 0) {
				return SITU_WALK_MALFORMED;
			}
			answer = left % right;
			break;
		case 0x15u: answer = left & right; break;
		case 0x16u: answer = left | right; break;
		case 0x17u: answer = left ^ right; break;
		case 0x18u:
			answer = (right >= 0 && right < 64) ? (int64_t)
				((uint64_t)left << right) : 0;
			break;
		case 0x19u:
			answer = (right >= 0 && right < 64) ? (left >> right) : 0;
			break;
		case 0x20u: answer = (left == right); break;
		case 0x21u: answer = (left != right); break;
		case 0x22u: answer = (left <  right); break;
		case 0x23u: answer = (left <= right); break;
		case 0x24u: answer = (left >  right); break;
		case 0x25u: answer = (left >= right); break;
		case 0x26u: answer = (left && right); break;
		case 0x27u: answer = (left || right); break;
		case 0x30u: answer = (left < right) ? left : right; break;
		case 0x31u: answer = (left > right) ? left : right; break;
		case 0x32u:
			if (right <= 0) {
				return SITU_WALK_MALFORMED;
			}
			answer = ((left + right - 1) / right) * right;
			break;
		default:
			return SITU_WALK_UNSUPPORTED;
		}

		stack[depth - 1u] = answer;
	}

	return SITU_WALK_MALFORMED;	/* ran off the end without END */
}
