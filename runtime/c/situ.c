/* situ.c -- out-of-line part of the situ runtime. */

#include "situ.h"

void situ_msg_init(situ_msg_t *msg, uint8_t *buf, uint32_t size)
{
	msg->base	= buf;
	msg->size	= size;
	msg->generation	= 1u;
}

void situ_msg_touch(situ_msg_t *msg)
{
	msg->generation++;
	/* Generation 0 is reserved for a zero-initialised view, so skip it on
	 * wrap rather than letting such a view come back to life. */
	if (msg->generation == 0u) {
		msg->generation = 1u;
	}
}

situ_err_t situ_view_at(const situ_msg_t *msg, uint32_t offset, uint32_t extent, situ_view_t *out)
{
	/* Written to avoid overflowing offset+extent. */
	if (msg->base == NULL || extent > msg->size || offset > msg->size - extent) {
		return SITU_ERR_BOUNDS;
	}

	out->base	= msg->base + offset;
	out->limit	= extent;
	out->generation	= msg->generation;
	return SITU_OK;
}

situ_err_t situ_view_sub(situ_view_t view, uint32_t offset, uint32_t extent, situ_view_t *out)
{
	if (view.base == NULL || !situ_in_bounds(view, offset, extent)) {
		return SITU_ERR_BOUNDS;
	}

	out->base	= view.base + offset;
	out->limit	= extent;
	out->generation	= view.generation;
	return SITU_OK;
}

const char *situ_err_str(situ_err_t err)
{
	switch (err) {
	case SITU_OK:			return "ok";
	case SITU_ERR_BOUNDS:		return "out of bounds";
	case SITU_ERR_CONSTRAINT:	return "constraint violated";
	case SITU_ERR_VERSION:		return "unknown version";
	case SITU_ERR_TAG:		return "tag stale or unverified";
	case SITU_ERR_STAGE:		return "stage gate not passed";
	case SITU_ERR_STALE:		return "stale view";
	}
	return "unknown error";
}
