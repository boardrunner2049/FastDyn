#if ENABLE_LIBPY
#include <Python.h>
#include <python.h>
#include <ctype.h>

// Global variables for Python integration
uint8_t py_init = false;
uint8_t peripheral_server_init = true;
PyObject *fastdyn_interceptor = NULL;
PyObject *halucinator_initialize = NULL;
PyObject *dev_notify_irq_callback = NULL;
PyObject *dev_irqret_hook_callback = NULL;
PyObject *device_model_callback = NULL;
uint32_t curr_bkpt_addr = 0;

PyMODINIT_FUNC PyInit_emb(void);

void initialize_halucinator_python_vm(void) {
	//Initialize the Python Interpreter
	Py_Initialize();

    PyRun_SimpleString("import sys, os\n"
                       "sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)\n"
                       "sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)\n"
                       "sys.path.append('.')\n");

	//Load the module for the C APIs <-> Python Interaction.
	PyObject *hal_reg_mem = PyUnicode_FromString("src.halucinator.hal_reg_mem");
	PyObject *hal_reg_mem_module = PyImport_Import(hal_reg_mem);
	Py_DECREF(hal_reg_mem);

	if (!hal_reg_mem_module) {
		PyErr_Print();
		fprintf(stderr, "Failed to load Python script.\n");
	}

	Py_XDECREF(hal_reg_mem_module);

	//Qemu -> Halucinator (Single Process)
	//Let's initialize the Halucinator Hal_initialzer -> For now, the configurations are defined inside the file, will update later, once stable.
	PyObject *Halucinator = PyUnicode_FromString("src.halucinator.main");
	PyObject *Halucinator_module = PyImport_Import(Halucinator);

	//Intercept Function to be called once the qemu starts halucinator. (initialize once)
	PyObject *Intercepts_file = PyUnicode_FromString("src.halucinator.bp_handlers.intercepts");
	PyObject *Intercepts_module = PyImport_Import(Intercepts_file);

	Py_DECREF(Halucinator);
	Py_DECREF(Intercepts_file);

	if (Halucinator_module != NULL && Intercepts_module != NULL) {
		//Get the Halucinator Initializer function
		halucinator_initialize = PyObject_GetAttrString(Halucinator_module, "halucinator_initialize");

		//Get the intecptor function from the intercepts module.
		fastdyn_interceptor = PyObject_GetAttrString(Intercepts_module, "intercept_fastdyn_callback");

		//Get the interrupt handler function from the intercept module.
		dev_notify_irq_callback = PyObject_GetAttrString(Intercepts_module, "notify_irq_callback");
		dev_irqret_hook_callback = PyObject_GetAttrString(Intercepts_module, "irqret_hook_callback");

		//Get the device model / peripheral handler callback from the intercept module.
		device_model_callback = PyObject_GetAttrString(Intercepts_module, "periph_access_callback");

		//verify the existance of the halucinator_initialize
		if (halucinator_initialize && PyCallable_Check(halucinator_initialize)) {
			//TODO: Improve this logic :>
			//Empyty block, don't do anything, just want to catch errors here
		} else {
			//Add garbage handling mabye when we go out of scope
			Py_DECREF(Halucinator_module);
			PyErr_Print();
			exit(1);
		}

		if (fastdyn_interceptor && PyCallable_Check(fastdyn_interceptor)) {
			py_init = true;
		} else {
			//Add garbage handling mabye when we go out of scope
			Py_DECREF(Intercepts_module);
			PyErr_Print();
			exit(1);
		}
		Py_DECREF(Halucinator_module);
		Py_DECREF(Intercepts_module);
	} else {
		PyErr_Print();
		exit(1);
	}

	//Let's initialize Halucinator only once!
	//No arguments, handled by halucinator itself
	//TODO: In future, find a way to pass arguments from here?
	PyObject *halucinator_initialize_args = PyTuple_Pack(0);

	// Call the Halucinator Initialize Function
	PyObject *Halucinator_return_val = PyObject_CallObject(halucinator_initialize, halucinator_initialize_args);

	Py_DECREF(halucinator_initialize_args);
	//Verify the halucinator was initialized successfully!
	if (Halucinator_return_val != NULL && PyTuple_Check(Halucinator_return_val)){
		PyObject *hal_return_val = PyTuple_GetItem(Halucinator_return_val, 0);  // True/False -> show whether halucinator was initialized successfully or not!

		int arg1 = PyObject_IsTrue(hal_return_val);    // Converts True/False to 1/0
		if (arg1){
			printf("Successfuly initialized Halucinator...\n");
			// 7. Release the GIL so Python threads can run later
		    PyEval_SaveThread();
		} else {
			Py_DECREF(Halucinator_return_val);
			printf("Error Initializing the Halucinator! Exiting...");
			exit(1);
		}
		Py_DECREF(Halucinator_return_val);
	} else {
		PyErr_Print();
		exit(1);
	}
}

void fastdyn_callback(unsigned int cpu_index, void *udata) {
    const char *input = (const char *) udata;
	if (input[0] == '*') {
	    curr_bkpt_addr = (uint32_t) strtoul(input + 1, NULL, 16);
	} else {
	    curr_bkpt_addr = (uint32_t) strtoul(input, NULL, 16);
	}

	if (!py_init) {
		initialize_halucinator_python_vm();
    }

	PyGILState_STATE gstate = PyGILState_Ensure();

	DEBUG_LOG("fastdyn api called!");
	DEBUG_LOG("input pc: %s",input);

	//Build the arguments. -> PC Value passed by the user when registering the callback!
	PyObject *fastdyn_callback_args = PyTuple_Pack(2, PyUnicode_FromString(input), PyLong_FromLong(peripheral_server_init));
	peripheral_server_init = false;

	// Call the Initialize function
	PyObject *fastdyn_callback_return_val = PyObject_CallObject(fastdyn_interceptor, fastdyn_callback_args);

	Py_DECREF(fastdyn_callback_args);
	//Verify the halucinator was initialized successfully!
	if (fastdyn_callback_return_val != NULL && PyTuple_Check(fastdyn_callback_return_val)){
		Py_DECREF(fastdyn_callback_return_val);
	} else {
		PyErr_Print();
		exit(1);
	}
	PyGILState_Release(gstate);

}

int python_vm_setup(void) {
	// Shutdown the Python Interpreter if already started.
	Py_Finalize();

    //Register QEMU-API Module
    PyImport_AppendInittab("qemuapi", PyInit_emb);

	//Python Interpreter can be used anywhere now!
	return 0;
}

//Expose read/write registers/memory API from here...
uint32_t read_reg(int reg);
uint32_t read_reg(int reg){
	if (reg == 15){
		return curr_bkpt_addr;
	} else {
		uint32_t reg_val = qemu_get_register(reg);
		return reg_val;
	}
}
static PyObject *read_reg_callback(PyObject *self, PyObject *args) {
	return PyCapsule_New((void *)read_reg, "read_reg_func", NULL);
}

uint32_t read_floating_reg(int reg);
uint32_t read_floating_reg(int reg){
	FloatConverter fc;
	fc.i = qemu_get_register(reg);
	DEBUG_LOG("Read_REG value %f", fc.f);
	return fc.f;
}
static PyObject *read_floating_reg_callback(PyObject *self, PyObject *args) {
	return PyCapsule_New((void *)read_floating_reg, "read_floating_reg_func", NULL);
}

void write_reg(int reg, uint32_t val);
void write_reg(int reg, uint32_t val){
	qemu_set_register(val, reg);
	if (reg == 15) {
		curr_bkpt_addr = val; //update the current value of PC
	}
}
static PyObject *write_reg_callback(PyObject *self, PyObject *args) {
	return PyCapsule_New((void *)write_reg, "write_reg_func", NULL);
}

void write_floating_reg(int reg, float val);
void write_floating_reg(int reg, float val){
	FloatConverter fc;
	fc.f = val;
	DEBUG_LOG("the value from c code is %f", fc.f);
	qemu_set_register(fc.i, reg);
}
static PyObject *write_floating_reg_callback(PyObject *self, PyObject *args) {
	return PyCapsule_New((void *)write_floating_reg, "write_floating_reg_func", NULL);
}

int read_memory(unsigned long long addr, uint8_t *mem_buf, int len);
int read_memory(unsigned long long addr, uint8_t *mem_buf, int len){
	return qemu_plugin_read_memory(addr, mem_buf, len);
}
static PyObject *read_mem_callback(PyObject *self, PyObject *args) {
	return PyCapsule_New((void *)read_memory, "read_mem_func", NULL);
}

int write_memory(unsigned long long addr, uint8_t *mem_buf, int len);
int write_memory(unsigned long long addr, uint8_t *mem_buf, int len){
	return qemu_plugin_write_memory(addr, mem_buf, len);
}
static PyObject *write_mem_callback(PyObject *self, PyObject *args) {
	return PyCapsule_New((void *)write_memory, "write_mem_func", NULL);
}

unsigned long long virtual_clock(void);
unsigned long long virtual_clock(void){
	return (unsigned long long)qemu_plugin_get_virtual_timer();
}

static PyObject *virtual_clock_callback(PyObject *self, PyObject *args) {
	return PyCapsule_New((void *)virtual_clock, "virtual_clock_func", NULL);
}

void python_pulseirq(uint64_t irq_num) {
    qemu_plugin_pulse_irq(irq_num);
}

static PyObject *pulse_irq_callback(PyObject *self, PyObject *args) {
	return PyCapsule_New((void *)python_pulseirq, "pulse_irq_func", NULL);
}


void python_raiseirq(uint64_t irq_num) {
	qemu_plugin_raise_irq(irq_num);
}

static PyObject *raise_irq_callback(PyObject *self, PyObject *args) {
	return PyCapsule_New((void *)python_raiseirq, "raise_irq_func", NULL);
}

// Python method definitions for both read and write
static PyMethodDef EmbMethods[] = {
	{"read_reg_callback", read_reg_callback, METH_NOARGS, "Returns a pointer to the C read_reg callback function"},
	{"write_reg_callback", write_reg_callback, METH_NOARGS, "Returns a pointer to the C write_reg callback function"},
	{"read_mem_callback", read_mem_callback, METH_NOARGS, "Returns a pointer to the C write_reg callback function"},
	{"write_mem_callback", write_mem_callback, METH_NOARGS, "Returns a pointer to the C write_reg callback function"},
	{"virtual_clock_callback", virtual_clock_callback, METH_NOARGS, "Returns a pointer to the C write_reg callback function"},
	{"write_floating_reg_callback", write_floating_reg_callback, METH_NOARGS, "Returns a pointer to the C write_reg callback function"},
	{"read_floating_reg_callback", read_floating_reg_callback, METH_NOARGS, "Returns a pointer to the C write_reg callback function"},
	{"raise_irq_callback", raise_irq_callback, METH_NOARGS, "Raises an interrupt with a given interrupt number"},
	{"pulse_irq_callback", pulse_irq_callback, METH_NOARGS, "Pulses an interrupt with a given interrupt number"},
	{NULL, NULL, 0, NULL}
};

static struct PyModuleDef qemuapi = {
	PyModuleDef_HEAD_INIT, "qemuapi", NULL, -1, EmbMethods
};

PyMODINIT_FUNC PyInit_emb(void) {
	return PyModule_Create(&qemuapi);
}

#endif