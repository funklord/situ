#include "situ_walk.h"

/* Section tags, from `image_section_tag` in std/image.situ. Only the ones
 * this build reads are named; a tag it does not know is skipped, which is
 * what the directory is for. */
#define TAG_STRUCTS    1u
#define TAG_PLACEMENTS 2u
#define TAG_CODE       3u

#define HEADER_BYTES  20u
#define SECTION_BYTES 16u

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

	out->image            = image;
	out->image_len        = len;
	out->structs          = NULL;
	out->struct_count     = 0u;
	out->struct_stride    = 0u;
	out->placements       = NULL;
	out->placement_count  = 0u;
	out->placement_stride = 0u;
	out->code             = NULL;
	out->code_len         = 0u;

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
		 * situc is readable, not an error. */
		if (kind == TAG_STRUCTS) {
			out->structs       = image + offset;
			out->struct_count  = items;
			out->struct_stride = stride;
		} else if (kind == TAG_PLACEMENTS) {
			out->placements       = image + offset;
			out->placement_count  = items;
			out->placement_stride = stride;
		} else if (kind == TAG_CODE) {
			out->code     = image + offset;
			out->code_len = items * stride;
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
	return SITU_WALK_OK;
}

/* `image_endian`: 1 big, 2 little. */
#define ENDIAN_BIG    1u
#define ENDIAN_LITTLE 2u

#define FLAG_OFFSET_KNOWN 0x01u
#define FLAG_SIGNED       0x10u

situ_walk_err situ_walk_read(const situ_walk_image *image,
                             const uint8_t *message, uint32_t len,
                             uint32_t index, uint64_t *out)
{
	situ_walk_placement held;
	const situ_walk_err err = situ_walk_placement_at(image, index, &held);
	if (err != SITU_WALK_OK) {
		return err;
	}

	/* Everything this build declines, declined explicitly. A number
	 * returned for a member it cannot place is a wrong length that reads
	 * exactly like a right one. */
	if ((held.flags & FLAG_OFFSET_KNOWN) == 0u
	                || held.offset_bits == SITU_WALK_NONE
	                || held.size_code != SITU_WALK_NONE
	                || held.located_code != SITU_WALK_NONE
	                || held.array_count != SITU_WALK_NONE) {
		return SITU_WALK_UNSUPPORTED;
	}
	if (held.offset_bits % 8u || held.size_bits % 8u || held.size_bits == 0u
	                || held.size_bits > 64u) {
		return SITU_WALK_UNSUPPORTED;
	}

	const uint32_t at    = held.offset_bits / 8u;
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
