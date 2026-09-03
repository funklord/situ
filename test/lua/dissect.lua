-- Run a generated dissector against real bytes, without Wireshark.
--
-- `situc gen-dissector` emits Lua for Wireshark's plugin API, and for a long
-- time nothing in this repository executed a line of it: the tests read the
-- text and checked that the numbers in it were the layout's. That is worth
-- having and is not the same as running it, and project.md said so plainly --
-- one semantic dependency was named as riding on the difference.
--
-- What a dissector needs from Wireshark is small: a protocol object, field
-- descriptors, a byte range that can be sliced and read, and a tree to add
-- fields to. All of it is stubbed below, which is enough to load a generated
-- dissector, run it over a buffer and report what it *showed*. What this
-- cannot check is that Wireshark accepts the plugin; what it can check is
-- that the dissection is right, which is the half that produces wrong answers
-- rather than an error message.
--
--     lua5.4 test/lua/dissect.lua <dissector.lua> <proto> <hex bytes>
--
-- One row per field the dissector added, in the order it added them:
--
--     abbreviation <TAB> offset <TAB> length <TAB> value
--
-- Offsets are absolute in the buffer handed in, including inside a sub-tvb a
-- nested dissector was called with. Wireshark numbers those from the start of
-- the sub-range; absolute is what makes a row comparable with the layout the
-- accessors were generated from, which is the thing being checked.

local rows    = {}
local protos  = {}

-- -- the byte range -------------------------------------------------------

local Range = {}
Range.__index = Range

local function range(buffer, base, offset, length)
	return setmetatable({ buffer = buffer, base = base,
	                      offset = offset, length = length }, Range)
end

function Range:len()
	return self.length
end

--- Big-endian, which is what `Tvb:uint()` is in Wireshark. The generated code
--- calls `add_le` for little-endian fields rather than reading them itself.
function Range:uint()
	local value = 0
	for i = 0, self.length - 1 do
		value = value * 256 + self.buffer:byte(self.offset + i + 1)
	end
	return value
end

function Range:le_uint()
	local value = 0
	for i = self.length - 1, 0, -1 do
		value = value * 256 + self.buffer:byte(self.offset + i + 1)
	end
	return value
end

function Range:bytes()
	return self.buffer:sub(self.offset + 1, self.offset + self.length)
end

--- The range as text, which is what Wireshark's `TvbRange:string()` gives.
--- A schema whose numbers are written as digits reads them through this and
--- `tonumber`, because `uint()` over eight ASCII characters is a number
--- nobody wrote -- and above four bytes Wireshark refuses it outright. The
--- stub had no `string`, so the first schema to use one died here rather than
--- in the dissector (26.42).
function Range:string()
	return self.buffer:sub(self.offset + 1, self.offset + self.length)
end

--- A sub-tvb over this range. The absolute base travels with it, so a field
--- a nested dissector adds is reported where it is in the original buffer.
function Range:tvb()
	local Tvb = getmetatable(self).Tvb
	return Tvb(self.buffer:sub(self.offset + 1, self.offset + self.length),
	           self.base + self.offset)
end

-- -- the tvb --------------------------------------------------------------

local function Tvb(buffer, base)
	local self = {}

	local function slice(offset, length)
		if offset == nil then
			return range(buffer, base, 0, #buffer)
		end
		-- `tvb(offset)` is the range from `offset` to the end, and so is a
		-- negative length: Wireshark's API, and what a generated dissector
		-- writes for a `[remaining]` member. The stub implemented only the
		-- two-argument form, so `subtree:add(f.payload, tvb(at))` -- correct
		-- Lua against the real API -- died here on `offset + nil`. Five
		-- dissectors could not be executed at all, and the reason was this
		-- file rather than any of them (26.35).
		if length == nil or length < 0 then
			length = #buffer - offset
		end
		if offset + length > #buffer then
			error(("tvb(%d, %d) runs past the %d bytes there are")
				:format(offset, length, #buffer), 2)
		end
		return range(buffer, base, offset, length)
	end

	return setmetatable(self, {
		__call  = function(_, offset, length) return slice(offset, length) end,
		__index = { len = function() return #buffer end },
		Tvb     = Tvb,
	})
end

-- Range:tvb() reaches the constructor through the metatable, which is set
-- above per tvb; this makes it reachable from a range made by any of them.
getmetatable(range("", 0, 0, 0)).Tvb = Tvb

-- -- the tree -------------------------------------------------------------

--- What Wireshark displays for a field: the masked bits, shifted down, and
--- the sign applied where the field was declared a signed one.
---
--- The width the sign extends from is the field's own -- the mask's bit count
--- where there is a mask, the range's bytes where there is not. That is
--- Wireshark's rule and it is situ's: `edges.trim` is `i5`, and the generated
--- C reads `situ_sign_extend(situ_bits_get_msb(view.base, 0u, 5u), 5u)`.
---
--- This stub read every range with `uint()`, so a signed member's row carried
--- an unsigned number, and the walker differential held all fifteen of them
--- out on the ground that the difference belonged to the stub rather than to
--- the dissector. It did -- and a stub that can be made faithful is a worse
--- reason to exclude an answer than to include it (26.210).
---
--- `value - (top << 1)` needs no case for a 64-bit field: the shift leaves
--- zero, and `uint()` has already wrapped to Lua's two's-complement integer,
--- which is the answer. `image`'s four `i64` members read through it.
local function display(field, value, bits)
	if field.mask then
		value = value & field.mask
		local mask = field.mask
		while mask & 1 == 0 do
			value = value >> 1
			mask  = mask >> 1
		end
		bits = 0
		while mask ~= 0 do
			bits = bits + 1
			mask = mask >> 1
		end
	end

	if field.kind == "int" then
		local top = 1 << (bits - 1)
		if value & top ~= 0 then
			value = value - (top << 1)
		end
	end
	return tostring(value)
end

local function shown(field, item)
	if field.kind == "bytes" then
		return (item:bytes():gsub(".", function (byte)
			return string.format("%02x", byte:byte())
		end))
	end
	return display(field, item:uint(), item.length * 8)
end

local Tree = {}
Tree.__index = Tree

local function tree()
	return setmetatable({}, Tree)
end

function Tree:add(field, item)
	-- `tree:add(proto, tvb())` opens the protocol's own subtree and shows no
	-- field of its own. Only a ProtoField is a row.
	if field.abbr ~= nil and item ~= nil then
		rows[#rows + 1] = ("%s\t%d\t%d\t%s"):format(
			field.abbr, item.base + item.offset, item.length, shown(field, item))
	end
	return tree()
end

--- No mask arm of its own: `display` has one, and no schema in the tree hands
--- `add_le` a masked field. Nine signed ones reach it, which is why it shares
--- the sign rather than restating it.
Tree.add_le = function(self, field, item)
	if field.abbr ~= nil and item ~= nil then
		rows[#rows + 1] = ("%s\t%d\t%d\t%s"):format(
			field.abbr, item.base + item.offset, item.length,
			display(field, item:le_uint(), item.length * 8))
	end
	return tree()
end

-- -- the globals a generated dissector reaches for -------------------------

function Proto(name, description)
	local self = { name = name, description = description, fields = {} }
	protos[name] = self
	return self
end

base = { DEC = "DEC", HEX = "HEX", OCT = "OCT" }

local function field(kind)
	return function (abbr, name, display, values, mask)
		return { kind = kind, abbr = abbr, name = name,
		         display = display, values = values, mask = mask }
	end
end

ProtoField = {
	bytes = field("bytes"), string = field("string"), none = field("none"),
}
for _, width in ipairs({ 8, 16, 24, 32, 64 }) do
	ProtoField["uint" .. width] = field("uint")
	ProtoField["int"  .. width] = field("int")
end

Dissector = {
	get = function (name)
		local held = protos[name]
		if held == nil then
			error("no dissector registered for " .. name, 2)
		end
		return { call = function (_, tvb, pinfo, into)
			return held.dissector(tvb, pinfo, into)
		end }
	end,
}

DissectorTable = { get = function () return { add = function () end } end }

-- -- the run ---------------------------------------------------------------

local path, proto, hex = ...
if path == nil or proto == nil or hex == nil then
	io.stderr:write("usage: dissect.lua <dissector.lua> <proto> <hex>\n")
	os.exit(2)
end

local buffer = (hex:gsub("%x%x", function (pair)
	return string.char(tonumber(pair, 16))
end))

local chunk, why = loadfile(path)
if chunk == nil then
	io.stderr:write(why .. "\n")
	os.exit(1)
end
chunk()

if protos[proto] == nil then
	io.stderr:write(("no `%s` in %s\n"):format(proto, path))
	os.exit(1)
end

local pinfo = { cols = {} }
local read  = protos[proto].dissector(Tvb(buffer, 0), pinfo, tree())

print(("consumed\t%d"):format(read))
for _, row in ipairs(rows) do
	print(row)
end
