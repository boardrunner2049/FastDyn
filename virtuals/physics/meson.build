# virtuals/physics/meson.build

phy_inc = include_directories('.')
local_include = [local_include, phy_inc]

subdir('physics_engines')

if get_option('enable_flight_controllers')
	subdir('flight_controllers')
endif

srcs += files(
  './phy.c',
  './fmu_trace/trace_csv.c',
)
