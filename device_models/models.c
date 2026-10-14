#include <config.h>
#if ENABLE_LIBHW
	extern DeviceModel passthrough_model_def;
#endif
extern DeviceModel elder_model_def;
extern DeviceModel twintrace_model_def;
extern DeviceModel classic_model_def;
#if ENABLE_LIBPY
extern DeviceModel halucinator_model_def;
#endif

// The global, statically-defined array of device models.
static DeviceModel *all_devices[] = {
	#if ENABLE_LIBHW
		&passthrough_model_def,
	#endif
	&elder_model_def,
	&twintrace_model_def,
	&classic_model_def,
	#if ENABLE_LIBPY
	&halucinator_model_def,
	#endif
};

// Calculate the number of devices at compile time.
static const int num_devices = sizeof(all_devices) / sizeof(all_devices[0]);
