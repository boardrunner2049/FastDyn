project(
  'fastdyn',
  'c',
  version: '0.1.0',
  meson_version: '>=0.61.2'
)
cc = meson.get_compiler('c')
# cpp = meson.get_compiler('cpp')

# suppress MAVLink packed warnings
add_project_arguments('-Wno-address-of-packed-member', language : 'c')


QEMU_DIR = get_option('qemu_path')
qemu_include = include_directories(
  join_paths(QEMU_DIR, 'include')
)
# --- Project include dir
include_dirs = [include_directories('include'), qemu_include, include_directories('.')]

# --- Project include dir
local_include = include_directories('include')

#glib support
glib = dependency('glib-2.0', version: '>=2.56.0', required: true)
cjson_dep = dependency('libcjson', required: true)
deps = [glib, cjson_dep]
srcs = []

#debugging FLAGS
DEBUG_PRINT = get_option('DEBUG_PRINT')

#enable SUNDIALS
ENABLE_SUNDIALS = get_option('enable_sundials')

if ENABLE_SUNDIALS
  # 1. Try the automatic way again (just in case)
  sundials_dep = dependency('sundials', required: false)

  if not sundials_dep.found()
    # 2. Manual Fallback: Link the specific SUNDIALS components
    # This assumes the files are in /usr/local/lib and /usr/local/include
    cpp = meson.get_compiler('c')

    # Manually find the libraries
    cvode_lib = cpp.find_library('sundials_cvode', required: true)
    nvec_lib  = cpp.find_library('sundials_nvecserial', required: true)
    m_lib     = cpp.find_library('m', required: true) # Math library

    sundials_dep = declare_dependency(
      dependencies: [cvode_lib, nvec_lib, m_lib]
    )
  endif

  deps += sundials_dep
  add_project_arguments('-DENABLE_SUNDIALS', language: 'c')
endif

#PYTHON Support
ENABLE_LIBPY = get_option('enable_libpy')
if ENABLE_LIBPY
  python3 = import('python').find_installation('python3')
  python_dep = python3.dependency()

  python_versions = ['python3.10', 'python3.11', 'python3.12', 'python3.13', 'python3.14', 'python3.15']
  libpython = dependency('', required: false)

  srcs += files('device_models/halucinator/halucinator.c')

  foreach v : python_versions
    libpython = cc.find_library(v, required: false)
    if libpython.found()
      break
    endif
  endforeach
  if not libpython.found()
    error('Required Python library not found (3.10 - 3.15)')
  endif

  deps+=[libpython, python_dep]
endif

# USER FLAGS. USER CAN ADD CUSTOM LIBS
ENABLE_LIBGZ = get_option('enable_libgz')
ENABLE_FLIGHT_CONTROLLERS = get_option('enable_flight_controllers')

#fuzzer support
ENABLE_LIBFUZZ = get_option('enable_libfuzz')

ENABLE_AFLNET = get_option('enable_aflnet')
if ENABLE_AFLNET
    deps += [
    cc.find_library('gvc',    required: true),
    cc.find_library('cgraph', required: true),
    cc.find_library('m',      required: true),
    cc.find_library('cap',    required: true),
    cc.find_library('dl',     required: true),
  ]
endif

ENABLE_PROBE = get_option('enable_probe')

ENABLE_PHY = get_option('enable_phy')

subdir('virtuals')

#subdir('virtuals')
ENABLE_LIBHW = get_option('enable_libhw')
LIBHW_DIR = get_option('libhw_path')

if ENABLE_LIBHW
  libhw_inc = include_directories(join_paths(LIBHW_DIR, 'include'))

  libhw_dep = declare_dependency(
    include_directories: libhw_inc,
    link_args: [
      '-L' + join_paths(LIBHW_DIR, 'out'),
      '-Wl,-rpath,' + join_paths(LIBHW_DIR, 'out'),
      '-lhw',
      '-lstlink',
    ],
  )

  deps += libhw_dep
endif


#Based on the configurations, generate config.h
ENABLE_LIBDEV = get_option('device_models')
configure_file(
  input : 'config.h.in',
  output : 'config.h',
  configuration : {
    'ENABLE_LIBGZ' : ENABLE_LIBGZ ? 1 : 0,
    'ENABLE_LIBHW' : ENABLE_LIBHW ? 1 : 0,
    'ENABLE_LIBFUZZ' : ENABLE_LIBFUZZ ? 1 : 0,
    'ENABLE_AFLNET' : ENABLE_AFLNET ? 1 : 0,
    'ENABLE_PROBE' : ENABLE_PROBE ? 1 : 0,
    'ENABLE_PHY' : ENABLE_PHY ? 1 : 0,
    'ENABLE_LIBPY' : ENABLE_LIBPY ? 1 : 0,
    'DEBUG_PRINT' : DEBUG_PRINT ? 1 : 0,
    'ENABLE_LIBDEV' : ENABLE_LIBDEV ? 1:0,
	'ENABLE_FMU' : get_option('enable_fmu') ? 1 : 0,
	'ENABLE_FLIGHT_CONTROLLERS' : get_option('enable_flight_controllers') ? 1 : 0,
  }
)
#config_inc = include_directories(meson.current_build_dir())


QEMU_DIR = get_option('qemu_path')
qemu_include = include_directories(
  join_paths(QEMU_DIR, 'include')
)

# Find BFD and libiberty
# bfd_dep = cc.find_library('bfd', required: true)

# deps += bfd_dep
# deps += iberty_dep

# --- Collect all source files
srcs += files(
  'core/core.c',
  'utils/utils.c',
  'virtuals/virtuals.c',
)

if ENABLE_PROBE
srcs += files(
  'virtuals/probe/probe.c',
)
endif

if ENABLE_LIBPY
srcs += files(
  'virtuals/python/python.c',
)
endif

if ENABLE_LIBGZ or ENABLE_FLIGHT_CONTROLLERS
srcs += files(
  'libs/ring_buffer.c',
)
endif

if ENABLE_LIBDEV
  libdl_dep = cc.find_library('dl', required: true)
  deps += libdl_dep
  srcs += files(
  'device_models/dev.c',
  'device_models/classic/classic.c',
  )

if ENABLE_LIBHW
  srcs += files(
  'device_models/common/hw_session.c',
  'device_models/passthrough/passthrough.c',
  'device_models/elder/elder.c',
  'device_models/elder/device_config.c',
  'device_models/twintrace/twintrace.c',
  'device_models/elder/inih/ini.c'
  )
endif

  device_models_inc = include_directories('device_models/elder/inih/')
  local_include = [local_include, device_models_inc]
endif

if ENABLE_LIBGZ or ENABLE_FLIGHT_CONTROLLERS
mavlink_include = include_directories('third_party/courbet_deps/mavlink_headers/ardupilotmega')

local_include = [local_include, mavlink_include]
endif

fastdyn_target = shared_module(
  'fastdyn',
  srcs,
  include_directories: [qemu_include, local_include],
  dependencies: deps,
  install: false
)
