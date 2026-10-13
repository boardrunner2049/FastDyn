.PHONY: all clean docs setup build_boardrunner

qemu_path    ?= ../qemu
libhw_path   ?= ../libhw
aflnet_path  ?= ../aflnet
LIBGZ        ?= false
LIBHW        ?= false
LIBFUZZ		 ?= false
AFLNET 		 ?= false
PROBE        ?= false
DEV          ?= false
DEBUG_PRINT  ?= false
LIBPY        ?= false
SUNDIALS     ?= false
BOARD_RUNNER ?= false
PHY			?= false
FMU			?= false
TRACE ?= false
FLIGHT_CONTROLLERS ?= false
EFFECTIVE_PHY := $(if $(filter true,$(PHY) $(FMU) $(FLIGHT_CONTROLLERS) $(LIBGZ)),true,false)

# Top-level target: clean, configure with meson, then build with ninja
all: setup
	ninja -C build

# Run meson configuration (after clean), and optionally copy libhw.so
setup: clean fetch
	mkdir -p build
	meson setup build \
	-Dqemu_path=$(abspath $(qemu_path)) \
	-Dlibhw_path=$(abspath $(libhw_path)) \
	-Daflnet_path=$(abspath $(aflnet_path)) \
	-Denable_libhw=$(LIBHW) \
	-Denable_libgz=$(LIBGZ) \
	-Denable_libfuzz=$(LIBFUZZ) \
	-Denable_aflnet=$(AFLNET) \
	-Denable_probe=$(PROBE) \
	-Ddevice_models=$(DEV) \
	-DDEBUG_PRINT=$(DEBUG_PRINT) \
	-Denable_libpy=$(LIBPY) \
	-Denable_sundials=$(SUNDIALS) \
	-Denable_phy=$(EFFECTIVE_PHY) \
	-Denable_fmu=$(FMU) \
	-Denable_flight_controllers=$(FLIGHT_CONTROLLERS) \
	-Denable_trace=$(TRACE)

	@if [ "$(BOARD_RUNNER)" = "true" ]; then \
		$(MAKE) build_boardrunner; \
	fi
	@if [ "$(LIBFUZZ)" = "true" ]; then \
		cd virtuals/fuzzer/fastdyn_fuzz_lib && cargo build --release; \
	fi

# Copy libhw.so into the build directory
build_boardrunner:
	@if [ "$(LIBHW)" = "true" ]; then \
		cp $(libhw_path)/out/libhw.so build/; \
	fi

docs:
	doxygen Doxyfile

clean:
	rm -rf build docs/html

fetch:
	git submodule update --init third_party/common/cmsis-svd-data;
	@# Only fetch submodules that are required for the selected features.
	@if [ "$(DEV)" = "true" ] && [ "$(LIBHW)" = "true" ]; then \
		git submodule update --init device_models/elder/inih third_party/common/cmsis-svd-data; \
	fi
	@if [ "$(LIBGZ)" = "true" ] || [ "$(FLIGHT_CONTROLLERS)" = "true" ]; then \
		git submodule update --init third_party/courbet_deps/mavlink_headers third_party/courbet_deps/SITL_Models; \
	fi

test:
	pytest

test_versbose:
	pytest -o log_cli=true --log-cli-level=INFO
