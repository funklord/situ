# situ-edit-qt -- the Qt Widgets frontend of decision 0034.
#
# Its own build and its own binary, because it is the one part of situ with
# a third-party dependency: `situc`, `situ-edit` and `situ-edit-tui` need
# Python and nothing else, and putting Qt in their path would put it in the
# path of somebody who wanted to set a byte in CI (0034).
#
#     make gui            # or:
#     cmake -S gui -B build/gui && cmake --build build/gui

cmake_minimum_required(VERSION 3.16)
project(situ_edit_qt LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

# Size, per the workspace's build rule: -Os, and let the linker drop what
# nothing calls. A build type a person chose wins.
if(NOT CMAKE_BUILD_TYPE)
	set(CMAKE_BUILD_TYPE MinSizeRel)
endif()

find_package(Qt6 REQUIRED COMPONENTS Widgets)

add_executable(situ-edit-qt main.cpp session.cpp window.cpp)
target_link_libraries(situ-edit-qt PRIVATE Qt6::Widgets)
target_compile_options(situ-edit-qt PRIVATE -Wall -Wextra
	-ffunction-sections -fdata-sections)
target_link_options(situ-edit-qt PRIVATE -Wl,--gc-sections)

# Where `situ-edit` and `situc` are when this runs from the build tree. An
# installed copy finds them beside itself first, so this is the fallback.
target_compile_definitions(situ-edit-qt PRIVATE
	SITU_TREE_BIN="${CMAKE_CURRENT_SOURCE_DIR}/../bin")

install(TARGETS situ-edit-qt RUNTIME DESTINATION bin)
