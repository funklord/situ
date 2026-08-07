# situ -- the CMake entry point, beside the GNU Make one.
#
# Section 24 asks for both, as separate and independently usable entry points
# rather than one wrapping the other. What each has to do is build the runtime,
# install it beside `situc`, and give a consuming project a way to run the
# compiler as a build step -- which is the part a user actually wants from a
# build system they already use.
#
#     cmake -B build && cmake --build build && cmake --install build
#
# Cross-compiling goes through CMake's own mechanism, `-DCMAKE_TOOLCHAIN_FILE`,
# rather than through `CROSS_COMPILE`: a build system's users know its own
# convention, and inventing a second one here would be a third thing to keep
# in sync (invariant 8 -- nothing about the machine running `situc` may reach
# the generated code, and the toolchain file is how CMake says which machine
# that is not).

cmake_minimum_required(VERSION 3.16)
project(situ VERSION 0.1 LANGUAGES C)

find_package(Python3 3.11 COMPONENTS Interpreter REQUIRED)

# Cached, because `situ_generate()` is called from the *consuming* project's
# scope, where a variable set here is not visible. The first version of this
# passed an empty interpreter and the build ran `-m situc build` as a command,
# which the shell reported as "-m: not found".
set(SITU_PYTHON "${Python3_EXECUTABLE}" CACHE FILEPATH
	"Interpreter that runs situc")
set(SITU_LAUNCHER "${CMAKE_CURRENT_SOURCE_DIR}/bin/situc" CACHE FILEPATH
	"The situc launcher, which finds its own package")

add_subdirectory(runtime/c)

install(PROGRAMS bin/situc DESTINATION bin)
# `situc verify` runs the generated accessors in memory, so the Python
# runtime ships beside the package. Nothing else in an installed situc
# needs it, and the Makefile installs it to the same place.
install(FILES runtime/python/situ_runtime.py DESTINATION lib/situc/_runtime)
install(DIRECTORY situc DESTINATION lib FILES_MATCHING PATTERN "*.py")

# The walker is a second program, not a mode of the compiler (decision 0026).
# It installs beside `situc` and shares nothing with it but the image format.
install(PROGRAMS bin/situ-walk DESTINATION bin)
install(DIRECTORY walker DESTINATION lib FILES_MATCHING PATTERN "*.py")

# VERSION sits beside the module because situc.__version__ reads it there.
# The Makefile installs it too, and test_cmake.py holds the two installers
# to the same file list -- which is how this omission was caught.
install(FILES VERSION DESTINATION lib/situc)
install(DIRECTORY std/ DESTINATION share/situc/std FILES_MATCHING PATTERN "*.situ")

# The manual page, from the same source the Makefile and the deb use. One
# copy, three installers -- the arrangement test_cmake.py exists to keep.
install(FILES packaging/situc.1 DESTINATION share/man/man1)

# -- running the compiler from a consuming build ----------------------------

#: Generate accessors for one schema, as a target other targets can depend on.
#:
#:     situ_generate(packet_schema SCHEMA packet.situ TARGET c)
#:     target_link_libraries(my_app PRIVATE packet_schema situ::situ)
#:
#: The generated directory is an include path on the target, so a consumer
#: writes `#include "packet.h"` and nothing else. Regeneration is keyed on the
#: schema, so editing it rebuilds what reads it -- which is the whole point of
#: having the compiler in the build rather than checking its output in.
function(situ_generate name)
	cmake_parse_arguments(ARG "" "SCHEMA;TARGET;PREFIX" "" ${ARGN})
	if(NOT ARG_SCHEMA)
		message(FATAL_ERROR "situ_generate(${name}): SCHEMA is required")
	endif()
	if(NOT ARG_TARGET)
		set(ARG_TARGET c)
	endif()

	get_filename_component(schema "${ARG_SCHEMA}" ABSOLUTE)
	get_filename_component(stem "${schema}" NAME_WE)
	set(out "${CMAKE_CURRENT_BINARY_DIR}/situ/${name}")

	set(products "${out}/${stem}.h" "${out}/${stem}.c")
	if(NOT ARG_TARGET STREQUAL "c")
		message(FATAL_ERROR
			"situ_generate(${name}): only the C backend produces a library "
			"target; use situc build --target ${ARG_TARGET} directly")
	endif()

	set(prefix_argument "")
	if(ARG_PREFIX)
		set(prefix_argument --prefix "${ARG_PREFIX}")
	endif()

	add_custom_command(
		OUTPUT ${products}
		COMMAND ${CMAKE_COMMAND} -E make_directory "${out}"
		COMMAND "${SITU_PYTHON}" "${SITU_LAUNCHER}" build
			--target ${ARG_TARGET} ${prefix_argument}
			--out "${out}" "${schema}"
		DEPENDS "${schema}"
		COMMENT "situc build ${stem}.situ (${ARG_TARGET})"
		VERBATIM)

	add_library(${name} STATIC ${products})
	target_include_directories(${name} PUBLIC "${out}")
	target_link_libraries(${name} PUBLIC situ::situ)
endfunction()
