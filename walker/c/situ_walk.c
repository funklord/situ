#include "situ_walk.h"

/* Section tags, from `image_section_tag` in std/image.situ. Only the ones
 * this build reads are named; a tag it does not know is skipped, which is
 * what the directory is for. */
#define TAG_STRUCTS    1u
#define TAG_PLACEMENTS 2u
#define TAG_CODE       3u
#define TAG_DELIMITERS 6u
#define TAG_VARINTS    9u

#define HEADER_BYTES  20u
#define SECTION_BYTES 16u

/* How far into a row this build reads, per table. Not the record's declared
 * width -- these are what `situ_walk_members`, `situ_walk_placement_at`,
 * `situ_walk_varint` and `situ_walk_scan` touch, and a row narrower than its
 * entry here would be read past. `fits` bounds the *table*, which leaves
 * exactly this hole at the last row of it. */
#define STRUCT_READS     8u
#define PLACEMENT_READS 40u
#define VARINT_READS    11u
#define DELIMITER_READS 32u

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

#define FLAG_OFFSET_KNOWN 0x01u
#define FLAG_SIGNED       0x10u

/* The load callback for a size expression: a field of this same message,
 * read at whatever offset the chain has reached. */
typedef struct {
	const situ_walk_image *image;
	const uint8_t         *message;
	uint32_t               len;
	uint32_t               shape;
} walk_ctx;

static situ_walk_err ctx_load(void *raw, uint32_t index, int64_t *out)
{
	walk_ctx *ctx = (walk_ctx *)raw;
	uint64_t value = 0u;
	const situ_walk_err err = situ_walk_read(ctx->image, ctx->message,
	                                         ctx->len, ctx->shape, index,
	                                         &value);
	if (err != SITU_WALK_OK) {
		return err;
	}
	*out = (int64_t)value;
	return SITU_WALK_OK;
}

situ_walk_err situ_walk_size_bits(const situ_walk_image *image,
                                  const uint8_t *message, uint32_t len,
                                  uint32_t shape, uint32_t index,
                                  uint32_t *out)
{
	situ_walk_placement held;
	situ_walk_err err = situ_walk_placement_at(image, index, &held);
	if (err != SITU_WALK_OK) {
		return err;
	}

	/* A `while` run and a variant arm each need a walk this build does not
	 * have. Named rather than approximated: the whole reason an extent is
	 * refused is that a wrong one is indistinguishable from a right one. */
	if (held.repeat_code != SITU_WALK_NONE) {
		return SITU_WALK_UNSUPPORTED;
	}

	/* A varint's width is in its own bytes. The record's `size_bits` is a
	 * lower bound, and taking it would place everything after one at the
	 * wrong offset. */
	if (varint_rules(image, index) != NULL) {
		uint32_t at = 0u;
		err = situ_walk_offset_bits(image, message, len, shape, index, &at);
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
	const uint8_t *delim = (held.type_struct == SITU_WALK_NONE)
	                     ? delimiter_rules(image, index) : NULL;
	if (delim != NULL) {
		uint32_t at = 0u;
		err = situ_walk_offset_bits(image, message, len, shape, index, &at);
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

	walk_ctx ctx = {image, message, len, shape};
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
	situ_walk_placement held;
	situ_walk_err err = situ_walk_placement_at(image, index, &held);
	if (err != SITU_WALK_OK) {
		return err;
	}

	if (held.located_code != SITU_WALK_NONE) {
		return SITU_WALK_UNSUPPORTED;	/* `at expr`, not yet */
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
		err = situ_walk_size_bits(image, message, len, shape, before, &wide);
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

situ_walk_err situ_walk_read(const situ_walk_image *image,
                             const uint8_t *message, uint32_t len,
                             uint32_t shape, uint32_t index, uint64_t *out)
{
	situ_walk_placement held;
	situ_walk_err err = situ_walk_placement_at(image, index, &held);
	if (err != SITU_WALK_OK) {
		return err;
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
	err = situ_walk_offset_bits(image, message, len, shape, index, &offset);
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

	if (offset % 8u || held.size_bits % 8u || held.size_bits == 0u
	                || held.size_bits > 64u) {
		return SITU_WALK_UNSUPPORTED;
	}

	const uint32_t at    = offset / 8u;
	const uint32_t width = held.size_bits / 8u;
	if (at > len || width > len - at) {
		return SITU_WALK_BOUNDS;
	}

	uint64_t value = 0u;
	if (held.endian == ENDIAN_LITTLE) {
		for (uint32_t i = width; i > 0u; i--) {
			value = (value << 8) | message[at + i - 1u];
		}
	} else if (held.endian == ENDIAN_BIG) {
		for (uint32_t i = 0u; i < width; i++) {
			value = (value << 8) | message[at + i];
		}
	} else {
		/* `native` has no answer a walker can give: the capture and the
		 * machine reading it are different machines. */
		return SITU_WALK_UNSUPPORTED;
	}

	if ((held.flags & FLAG_SIGNED) != 0u && width < 8u) {
		const uint64_t sign = (uint64_t)1 << (held.size_bits - 1u);
		if (value & sign) {
			value |= ~(((uint64_t)1 << held.size_bits) - 1u);
		}
	}

	*out = value;
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
