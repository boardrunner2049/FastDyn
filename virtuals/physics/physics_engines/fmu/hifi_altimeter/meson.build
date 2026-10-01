cc = meson.get_compiler('c')
dl_dep = cc.find_library('dl', required: true)
glib_dep = dependency('glib-2.0', required: true)

# 2. Define the isolated include path
# This -I flag will ONLY be applied to files in this directory
fmi_inc_path = include_directories('./output_folder/249.fmutmp/sources/fmi/')


# 3. Build the FMU logic as a static library
# This is where the 'surgical' application of flags happens.
fmu_static_lib = static_library(
  'fmu_harness_internal',
  'harness/altimeter_virtual.c',
  include_directories: [ fmi_inc_path, include_dirs],
  dependencies: [dl_dep, glib, qemu_dep],
  # We use install: false because this is just an intermediate research component
  install: false 
)

# 4. Create the dependency object for the parent scope
# Note: We do NOT include 'include_directories' here. 
# This ensures that other project files can't see the FMI headers.
fmu_dep = declare_dependency(
  link_with: fmu_static_lib,
  dependencies: [glib_dep, dl_dep]
)

# 5. Append this isolated dependency to the global list
# This variable 'deps' must be initialized in your root meson.build
deps += fmu_dep

