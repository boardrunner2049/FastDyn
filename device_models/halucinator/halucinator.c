#include <device.h>
#include <utils.h>
#include <pthread.h>
#include <unistd.h>
#include <python.h>
#include <Python.h>

#define FD_LOG_SUBSYSTEM "halucinator"
#include <fastdyn/log.h>

// Global current device model
static struct timespec start_ts;


typedef struct HalucinatorState {
    hwaddr start;
    hwaddr end;
} HalucinatorState;


static uint64_t halucinator_read(void *opaque, hwaddr req_address, unsigned req_size) {
	//In case, it matches the peripherals address range, just forward to the python peripheral handler.
	//Checks will be applied by the python handler for the exact region and permissions e.t.c
	uint32_t access_req = 0; //access_req: if 1 -> write_req else read_req
	uint64_t req_write_val = 0; //we don't care, we will ignore it in read access.
	uint64_t ret_val = 0;
	uint32_t curr_pc = qemu_get_register(15);
    HalucinatorState *state = (HalucinatorState *)opaque;
    hwaddr base_addr = state->start;

	PyGILState_STATE gstate = PyGILState_Ensure();

	//Build the arguments. -> access_req, req_address, req_size, req_write_val, actual_region_base_addr, actual_region_size
	PyObject *device_model_args = PyTuple_Pack(6, PyLong_FromLong(curr_pc), PyLong_FromLong(access_req), PyLong_FromLong(req_address),
	PyLong_FromLong(req_write_val), PyLong_FromLong(req_size), PyLong_FromLong(base_addr));

	// Call the Initialize function
	PyObject *device_model_return_val = PyObject_CallObject(device_model_callback, device_model_args);

	if (device_model_return_val != NULL) {
		// Check if it is an integer
		if (PyLong_Check(device_model_return_val)) {
			ret_val = PyLong_AsUnsignedLongLong(device_model_return_val);
		} else {
			// Unexpected type
			FD_LOG_ERROR("the Python model returned a non-integer; expected uint64_t");
			PyErr_Print();
			Py_DECREF(device_model_return_val);
			exit(1);
		}

		Py_DECREF(device_model_return_val);  // safe to DECREF after extracting value
	} else {
		// NULL return value (Python error)
		FD_LOG_ERROR("the Python model returned no value; expected uint64_t");
		PyErr_Print();
		exit(1);
	}
	PyGILState_Release(gstate);

	return ret_val;

}

static void halucinator_write(void *opaque, hwaddr req_address, uint64_t req_write_val, unsigned req_size) {
	//In case, it matches the peripherals address range, just forward to the python peripheral handler.
	//Checks will be applied by the python handler for the exact region and permissions e.t.c
	uint32_t access_req = 1; //access_req: if 1 -> write_req else read_req
	uint32_t curr_pc = qemu_get_register(15);
    HalucinatorState *state = (HalucinatorState *)opaque;
    hwaddr base_addr = state->start;

	PyGILState_STATE gstate = PyGILState_Ensure();

	//Build the arguments. -> access_req, req_address, req_size, req_write_val, actual_region_base_addr
	PyObject *device_model_args = PyTuple_Pack(6, PyLong_FromLong(curr_pc), PyLong_FromLong(access_req), PyLong_FromLong(req_address),
	PyLong_FromLong(req_write_val), PyLong_FromLong(req_size), PyLong_FromLong(base_addr));

	// Call the Initialize function
	PyObject_CallObject(device_model_callback, device_model_args);

	PyGILState_Release(gstate);
}

static inline void dev_get_timestamp(time_t *sec, long *usec) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	// Calculate offset from start
	*sec = ts.tv_sec - start_ts.tv_sec;
	*usec = (ts.tv_nsec - start_ts.tv_nsec) / 1000;
	if (*usec < 0) {
		*sec -= 1;
		*usec += 1000000;
	}
}

static int halucinator_interrupt(int number) {
	time_t sec;
    long usec;
    dev_get_timestamp(&sec, &usec);
	DEBUG_LOG("[%5ld.%06ld] Interrupt Taken: \t Vector = 0x%08X\n",
			sec, usec, number);

	if (!py_init) {
		initialize_halucinator_python_vm();
	}
	// Always acquire GIL before calling Python APIs
	PyGILState_STATE gstate = PyGILState_Ensure();

	//Build the arguments. -> interrupt number passed
	PyObject *dev_notify_irq_args = PyTuple_Pack(1, PyLong_FromLong(number));

	// Call the Initialize function
	PyObject *dev_notify_irq_return_val = PyObject_CallObject(dev_notify_irq_callback, dev_notify_irq_args);

	Py_DECREF(dev_notify_irq_args);
	//Verify the halucinator was initialized successfully!
	if (dev_notify_irq_return_val != NULL && PyTuple_Check(dev_notify_irq_return_val)){
		Py_DECREF(dev_notify_irq_return_val);
	} else {
		PyErr_Print();
		exit(1);
	}


	// Release GIL
	PyGILState_Release(gstate);
	return 0;
}

static int halucinator_serve(int number) {
	time_t sec;
    long usec;
	dev_get_timestamp(&sec, &usec);
	DEBUG_LOG("[%5ld.%06ld] Interrupt Served: \t Vector = 0x%08X\n",
			sec, usec, number);

	if (!py_init) {
		initialize_halucinator_python_vm();
	}
	PyGILState_STATE gstate = PyGILState_Ensure();
	//Build the arguments. -> interrupt number passed
	PyObject *dev_irqret_hook_args = PyTuple_Pack(1, PyLong_FromLong(number));

	// Call the Initialize function
	PyObject *dev_irqret_hook_return_val = PyObject_CallObject(dev_irqret_hook_callback, dev_irqret_hook_args);

	Py_DECREF(dev_irqret_hook_args);
	//Verify the halucinator was initialized successfully!
	if (dev_irqret_hook_return_val != NULL && PyTuple_Check(dev_irqret_hook_return_val)){
		Py_DECREF(dev_irqret_hook_return_val);
	} else {
		PyErr_Print();
		exit(1);
	}
    PyGILState_Release(gstate);
	return 0;
}
int halucinator_init(char *argument);


// int halucinator_init(char *argument) {
// 	Range ranges[10];
// 	int n = utils_parse_ranges(argument, ranges, 10);
// 	for (int i = 0; i < n; i++) {
//         dev_register_device_model(ranges[i].start, ranges[i].end, &halucinator_model_def);
//     }
//     return 0;
// }

// The public definition of the halucinator device model
DeviceModel halucinator_model_def = {
    .name = "halucinator",
    .read = halucinator_read,
    .write = halucinator_write,
	.init = halucinator_init,
	.serve = halucinator_serve,
	.interrupt = halucinator_interrupt,
};

int halucinator_init(char *argument) {
    Range ranges[10];
    int n = utils_parse_ranges(argument, ranges, 10);
	initialize_halucinator_python_vm();
    for (int i = 0; i < n; i++) {
        HalucinatorState *state = malloc(sizeof(HalucinatorState));

		if (!state) { FD_LOG_ERROR("out of memory"); exit(1); }

        state->start = ranges[i].start;
        state->end   = ranges[i].end;

        DeviceModel *dev_instance = malloc(sizeof(DeviceModel));
        *dev_instance = halucinator_model_def;
        dev_instance->opaque = state;

        // Register this instance with its memory range
        dev_register_device_model(ranges[i].start, ranges[i].end, dev_instance);
    }

	return 0;
}
