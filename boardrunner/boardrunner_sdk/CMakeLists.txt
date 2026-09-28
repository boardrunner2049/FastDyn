# cmake_minimum_required(VERSION 3.16)
# project(boardrunner_sdk C)

# set(CMAKE_C_STANDARD 11)
# set(CMAKE_C_STANDARD_REQUIRED ON)

# # Put outputs in the chosen build directory
# set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})

# # External include dirs
# set(FASTDYN_INCLUDE_DIR "" CACHE PATH "Path to FastDyn include dir (must contain device.h)")
# set(QEMU_INCLUDE_DIR "" CACHE PATH "Path to QEMU include dir")

# if(NOT FASTDYN_INCLUDE_DIR)
#   message(FATAL_ERROR "FASTDYN_INCLUDE_DIR is required (must contain device.h)")
# endif()

# if(NOT QEMU_INCLUDE_DIR)
#   message(FATAL_ERROR "QEMU_INCLUDE_DIR is required")
# endif()

# # glib
# find_package(PkgConfig REQUIRED)
# pkg_check_modules(GLIB REQUIRED glib-2.0)

# # ---------------------------
# # 1) Build the shared VIO library once
# # ---------------------------
# file(GLOB_RECURSE VIO_SOURCES CONFIGURE_DEPENDS
#   "${CMAKE_CURRENT_LIST_DIR}/boardrunner_vio/src/*.c"
# )

# add_library(boardrunner_vio SHARED ${VIO_SOURCES})
# set_target_properties(boardrunner_vio PROPERTIES
#   OUTPUT_NAME "boardrunner_vio"     # libboardrunner_vio.so
# )

# target_compile_options(boardrunner_vio PRIVATE -g -fPIC)

# target_include_directories(boardrunner_vio PUBLIC
#   "${CMAKE_CURRENT_LIST_DIR}/include"
#   ${GLIB_INCLUDE_DIRS}
#   ${FASTDYN_INCLUDE_DIR}
#   "${QEMU_INCLUDE_DIR}"
# )

# # Derive FastDyn root from FASTDYN_INCLUDE_DIR (expected: <FastDynRoot>/include)
# get_filename_component(FASTDYN_ROOT "${FASTDYN_INCLUDE_DIR}" DIRECTORY)
# set(FASTDYN_UTILS_C "${FASTDYN_ROOT}/utils/utils.c")

# if(NOT EXISTS "${FASTDYN_UTILS_C}")
#   message(FATAL_ERROR
#     "Expected FastDyn utils at: ${FASTDYN_UTILS_C}\n"
#     "Make sure FASTDYN_INCLUDE_DIR points to: <FastDynRoot>/include"
#   )
# endif()

# target_sources(boardrunner_vio PRIVATE "${FASTDYN_UTILS_C}")

# target_link_libraries(boardrunner_vio PRIVATE ${GLIB_LIBRARIES})
# target_link_directories(boardrunner_vio PRIVATE ${GLIB_LIBRARY_DIRS})

# # Fail fast if any symbol is unresolved (catch dlopen failures at build time)
# target_link_options(boardrunner_vio PRIVATE -Wl,--no-undefined)

# # ---------------------------
# # 2) Build ONE plugin: model.so
# # ---------------------------
# add_library(model MODULE "${CMAKE_CURRENT_LIST_DIR}/model/model.c")
# set_target_properties(model PROPERTIES
#   PREFIX ""                  # model.so not libmodel.so
#   OUTPUT_NAME "model"
#   BUILD_RPATH "\$ORIGIN"     # find libboardrunner_vio.so next to model.so
#   INSTALL_RPATH "\$ORIGIN"
# )

# target_compile_options(model PRIVATE -g -fPIC -rdynamic)

# target_include_directories(model PRIVATE
#   "${CMAKE_CURRENT_LIST_DIR}/include"
#   ${GLIB_INCLUDE_DIRS}
#   ${FASTDYN_INCLUDE_DIR}
#   "${QEMU_INCLUDE_DIR}"
# )

# target_link_libraries(model PRIVATE boardrunner_vio ${GLIB_LIBRARIES})

# # Usage:
# #   cmake -S . -B build \
# #     -DFASTDYN_INCLUDE_DIR=/path/to/FastDyn/include \
# #     -DQEMU_INCLUDE_DIR=/path/to/qemu/include
# #   cmake --build build -j

cmake_minimum_required(VERSION 3.16)
project(boardrunner_sdk C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
option(BOARDRUNNER_BUILD_RUNTIME "Build the shared VIO library and model plugins" ON)
option(BOARDRUNNER_BUILD_API_TESTS "Build standalone BoardRunner API tests" OFF)

# Put outputs in the chosen build directory
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})

# External include dirs
set(FASTDYN_INCLUDE_DIR "" CACHE PATH "Path to FastDyn include dir (must contain device.h)")
set(QEMU_INCLUDE_DIR "" CACHE PATH "Path to QEMU include dir")

if(BOARDRUNNER_BUILD_API_TESTS)
  enable_testing()

  add_executable(test_signals
    "${CMAKE_CURRENT_LIST_DIR}/tests/test_signals.c"
    "${CMAKE_CURRENT_LIST_DIR}/boardrunner_vio/src/bus/signals.c"
  )

  target_compile_options(test_signals PRIVATE -g)
  target_include_directories(test_signals PRIVATE
    "${CMAKE_CURRENT_LIST_DIR}/include"
  )

  add_test(NAME boardrunner_test_signals COMMAND test_signals)
endif()

if(BOARDRUNNER_BUILD_RUNTIME)
  if(NOT FASTDYN_INCLUDE_DIR)
    message(FATAL_ERROR "FASTDYN_INCLUDE_DIR is required (must contain device.h)")
  endif()

  if(NOT QEMU_INCLUDE_DIR)
    message(FATAL_ERROR "QEMU_INCLUDE_DIR is required")
  endif()

  # glib
  find_package(PkgConfig REQUIRED)
  pkg_check_modules(GLIB REQUIRED glib-2.0)

  # Derive FastDyn root from FASTDYN_INCLUDE_DIR
  get_filename_component(FASTDYN_ROOT "${FASTDYN_INCLUDE_DIR}" DIRECTORY)

  # ==========================================
  #  FIX: LOCATE LIBFASTDYN.SO
  # ==========================================
  # We look for the library in the 'build' folder relative to the root
  find_library(FASTDYN_LIB fastdyn
    PATHS "${FASTDYN_ROOT}/build"
    NO_DEFAULT_PATH
  )

  if(NOT FASTDYN_LIB)
      message(FATAL_ERROR "Could not find libfastdyn.so in ${FASTDYN_ROOT}/build. Please build FastDyn first.")
  endif()
  message(STATUS "Found FastDyn Library: ${FASTDYN_LIB}")

  # ---------------------------
  # 1) Build the shared VIO library
  # ---------------------------
  file(GLOB_RECURSE VIO_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_LIST_DIR}/boardrunner_vio/src/*.c"
  )

  # NOTE: You were manually adding utils.c here. If utils.c is already inside
  # libfastdyn.so, you might not need to compile it again here.
  # For now, I will leave it to avoid breaking other dependencies,
  # but ideally, you should link against FASTDYN_LIB instead of recompiling source.
  set(FASTDYN_UTILS_C "${FASTDYN_ROOT}/utils/utils.c")
  if(NOT EXISTS "${FASTDYN_UTILS_C}")
    message(FATAL_ERROR "Expected FastDyn utils at: ${FASTDYN_UTILS_C}")
  endif()

  add_library(boardrunner_vio SHARED ${VIO_SOURCES})
  target_sources(boardrunner_vio PRIVATE "${FASTDYN_UTILS_C}")

  set_target_properties(boardrunner_vio PROPERTIES OUTPUT_NAME "boardrunner_vio")
  target_compile_options(boardrunner_vio PRIVATE -g -fPIC)

  target_include_directories(boardrunner_vio PUBLIC
    "${CMAKE_CURRENT_LIST_DIR}/include"
    ${GLIB_INCLUDE_DIRS}
    ${FASTDYN_INCLUDE_DIR}
    "${QEMU_INCLUDE_DIR}"
  )

  # rt: shm_open / clock_gettime (glibc split before 2.34; harmless after)
  # m:  lroundf and friends from invensense_packer.c
  target_link_libraries(boardrunner_vio PRIVATE ${GLIB_LIBRARIES} rt m)
  target_link_options(boardrunner_vio PRIVATE -Wl,--no-undefined)

  # ---------------------------
  # 2) Build ALL plugins in model/
  # ---------------------------
  # Each .c file in model/ becomes its own .so (e.g., model.c -> model.so).
  file(GLOB MODEL_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_LIST_DIR}/model/*.c"
  )

  # ==========================================
  #  FIX: RPATH SETTINGS
  # ==========================================
  # get the directory where libfastdyn.so lives so we can add it to RPATH
  get_filename_component(FASTDYN_LIB_DIR "${FASTDYN_LIB}" DIRECTORY)

  if(NOT MODEL_SOURCES)
    message(WARNING "No model sources found in ${CMAKE_CURRENT_LIST_DIR}/model")
  endif()

  foreach(MODEL_SRC ${MODEL_SOURCES})
    get_filename_component(MODEL_NAME "${MODEL_SRC}" NAME_WE)
    # Sanitize for CMake target name
    string(REGEX REPLACE "[^A-Za-z0-9_]" "_" MODEL_TARGET "${MODEL_NAME}")

    add_library(${MODEL_TARGET} MODULE "${MODEL_SRC}")

    set_target_properties(${MODEL_TARGET} PROPERTIES
      PREFIX ""
      OUTPUT_NAME "${MODEL_NAME}"
      # Add the location of libfastdyn.so to the Runtime Search Path
      BUILD_RPATH "${FASTDYN_LIB_DIR}:\$ORIGIN"
      INSTALL_RPATH "${FASTDYN_LIB_DIR}:\$ORIGIN"
    )

    target_compile_options(${MODEL_TARGET} PRIVATE -g -fPIC)

    target_include_directories(${MODEL_TARGET} PRIVATE
      "${CMAKE_CURRENT_LIST_DIR}/include"
      ${GLIB_INCLUDE_DIRS}
      ${FASTDYN_INCLUDE_DIR}
      "${QEMU_INCLUDE_DIR}"
    )

    # This allows each plugin to "see" symbols (like test_info) inside libfastdyn.so
    target_link_libraries(${MODEL_TARGET} PRIVATE boardrunner_vio ${GLIB_LIBRARIES} ${FASTDYN_LIB})
  endforeach()
endif()
