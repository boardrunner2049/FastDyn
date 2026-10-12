#include <device.h>
#include <utils.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <dlfcn.h>
#include <string.h>

#define FD_LOG_SUBSYSTEM "elder"
#include <fastdyn/log.h>

/* Elder loads one device model library per configured device. The library
 * exports `fastdyn_model`, the interface in <fastdyn/device_model.h>. */

#define MAX_DEVICES 64  /* devices, and address ranges, elder serves */

typedef struct {
    DeviceModels* config;  // Embed original config struct

    void *handle;           // dlopen handle

    void *opaque;           // the model instance init returned

    const FdModel *v2;      // the model's descriptor
    FdDevice info;          // what the model is told about its device
    FdRange *ranges;
    int *irqs;
} device_model_t;

device_model_t devices[MAX_DEVICES];
DeviceModels configs[MAX_DEVICES];

static int device_count = 0;

/* Range ends in the configuration are inclusive. */
static device_model_t *elder_find_device(hwaddr address) {
	for (int i = 0; i < device_count; i++) {
		const DeviceModels *config = devices[i].config;
		for (int j = 0; j < config->range_count; j++) {
			if (address >= config->ranges[j].start && address <= config->ranges[j].end) {
				return &devices[i];
			}
		}
	}
	return NULL;
}

static uint64_t elder_read(void *opaque, hwaddr address, unsigned size, uint64_t pc) {
	device_model_t *d = elder_find_device(address);
	if (d) {
		// Good case, my scroll worked
		return d->v2->read(d->opaque, address, size, pc);
	}
	char err_buf[128];
	snprintf(err_buf, sizeof(err_buf),
             "Unable to map any address to any address of elder read function (address: 0x%lx, pc: 0x%lx)",
             address, pc);
	utils_die(err_buf);
	return 0; // never reached, but silences compiler

}

static void elder_write(void *opaque, hwaddr address, uint64_t value, unsigned size, uint64_t pc) {
	device_model_t *d = elder_find_device(address);
	if (!d) {
		return;
	}
	// Good case, my scroll worked
	d->v2->write(d->opaque, address, value, size, pc);
}

/* Optional descriptor members are only read if the model was built with them. */
#define ELDER_V2_HAS(m, member) \
	((m)->size >= offsetof(FdModel, member) + sizeof((m)->member) && (m)->member)

static int elder_device_has_irq(const device_model_t *d, int number) {
	for (size_t i = 0; i < d->info.irq_count; i++) {
		if (d->irqs[i] == number) {
			return 1;
		}
	}
	return 0;
}

/* A model only hears its own IRQs. */
static int elder_serve(int number) {
	for (int i = 0; i < device_count; i++) {
		device_model_t *d = &devices[i];
		if (ELDER_V2_HAS(d->v2, serve) && elder_device_has_irq(d, number)) {
			d->v2->serve(d->opaque, number);
		}
	}
    return 0;
}

static int elder_interrupt(int number) {
	for (int i = 0; i < device_count; i++) {
		device_model_t *d = &devices[i];
		if (ELDER_V2_HAS(d->v2, interrupt) && elder_device_has_irq(d, number)) {
			d->v2->interrupt(d->opaque, number);
		}
	}
    return 0;
}

static void* elder_init(ConfigSection* model_info);
// The public definition of the elder device model
DeviceModel elder_model_def = {
    .name = "elder",
    .read = elder_read,
    .write = elder_write,
    .init = elder_init,
    .serve = elder_serve,
    .interrupt = elder_interrupt,
};

static void elder_die(const char *format, ...) __attribute__((format(printf, 1, 2), noreturn));

static void elder_die(const char *format, ...) {
	char message[512];
	va_list args;
	va_start(args, format);
	vsnprintf(message, sizeof(message), format, args);
	va_end(args);
	utils_die(message);
	exit(EXIT_FAILURE);  // utils_die does not return
}

/* FdHost.log: model messages go to the FastDyn log as fastdyn.<name>. */
static void elder_host_log(int level, const char *name, const char *format, ...) {
	va_list args;
	va_start(args, format);
	fastdyn_logv(level, name ? name : "model", format, args);
	va_end(args);
}

static const FdHost elder_host = {
	.size = sizeof(FdHost),
	.log = elder_host_log,
};

/* LibVIO's messages (vio_set_logger) go to the same log as fastdyn.vio.<subsystem>. */
typedef void (*elder_vio_log_fn)(int level, const char *subsystem, const char *message, void *user);
typedef void (*elder_vio_set_logger_fn)(elder_vio_log_fn fn, void *user);

static void elder_vio_log(int level, const char *subsystem, const char *message, void *user) {
	(void)user;
	char name[64];
	snprintf(name, sizeof(name), "vio.%s", subsystem ? subsystem : "core");
	fastdyn_log(level, name, "%s", message);
}

static void elder_route_vio_logs(void *handle) {
	elder_vio_set_logger_fn set_logger = (elder_vio_set_logger_fn)dlsym(handle, "vio_set_logger");
	if (set_logger) {
		set_logger(elder_vio_log, NULL);
	}
}

static void elder_load_v2(device_model_t *d) {
	const FdModel *m = d->v2;
	const char *name = d->config->name;
	const char *path = d->config->scroll_path;

	if (m->abi != FASTDYN_MODEL_ABI_VERSION) {
		elder_die("%s: %s was built for model ABI %u, but this FastDyn expects %d",
		          name, path, (unsigned)m->abi, FASTDYN_MODEL_ABI_VERSION);
	}
	if (m->size < offsetof(FdModel, interrupt)) {
		elder_die("%s: %s has an incomplete fastdyn_model (size %u)", name, path, (unsigned)m->size);
	}
	if (!m->init || !m->read || !m->write) {
		elder_die("%s: fastdyn_model.%s is not set (%s)", name,
		          !m->init ? "init" : !m->read ? "read" : "write", path);
	}

	const DeviceModels *config = d->config;
	d->ranges = calloc(config->range_count > 0 ? (size_t)config->range_count : 1, sizeof(FdRange));
	d->irqs = calloc(config->irq_count > 0 ? (size_t)config->irq_count : 1, sizeof(int));
	if (!d->ranges || !d->irqs) {
		elder_die("%s: out of memory", name);
	}
	for (int j = 0; j < config->range_count; j++) {
		d->ranges[j].base = config->ranges[j].start;
		d->ranges[j].size = config->ranges[j].end - config->ranges[j].start + 1;
	}
	for (int j = 0; j < config->irq_count; j++) {
		d->irqs[j] = config->irqs[j];
	}
	d->info = (FdDevice){
		.size = sizeof(FdDevice),
		.name = name,
		.ranges = d->ranges,
		.range_count = (size_t)config->range_count,
		.irqs = d->irqs,
		.irq_count = (size_t)config->irq_count,
	};

	d->opaque = m->init(&d->info, config->config_json ? config->config_json : "{}", &elder_host);
	if (!d->opaque) {
		elder_die("%s: the model refused its configuration (init returned NULL; %s)", name, path);
	}
}

static void* elder_init(ConfigSection* model_info) {
    //Find the overall ranges for all the devices registered as elder
    Range ranges[MAX_DEVICES];
	if (model_info->device_count > MAX_DEVICES || model_info->overall_range_count > MAX_DEVICES) {
		elder_die("elder supports at most %d devices and %d ranges", MAX_DEVICES, MAX_DEVICES);
	}
	device_count = model_info->device_count;
    utils_parse_ranges(model_info->overall_range_count,model_info->overall_ranges, ranges);

	for (int i = 0; i < model_info->overall_range_count; i++) {
        dev_register_device_model(ranges[i].start, ranges[i].end, &elder_model_def);
    }

	for (int i=0; i < model_info->device_count; i++) {
		device_model_t *d = &devices[i];
		d->config = &model_info->devices[i];
		printf("Loading device [%s] from %s\n", d->config->name, d->config->scroll_path);

	    d->handle = dlopen(d->config->scroll_path, RTLD_NOW);
	    if (!d->handle) {
	        fprintf(stderr, "dlopen failed: %s\n", dlerror());
	        utils_die("dlopen error");
	    }
		elder_route_vio_logs(d->handle);

		d->v2 = (const FdModel *)dlsym(d->handle, FASTDYN_MODEL_SYMBOL);
		if (!d->v2) {
			elder_die("%s: %s does not export %s (see <fastdyn/device_model.h>)",
			          d->config->name, d->config->scroll_path, FASTDYN_MODEL_SYMBOL);
		}
		elder_load_v2(d);

        // Register IRQs for this device based on config
        if (d->config->irq_count > 0 && d->config->irqs) {
            for (int j = 0; j < d->config->irq_count; j++) {
                dev_register_interrupt_device_model((int)d->config->irqs[j], &elder_model_def);
            }
        }
	}
	return NULL;  // elder itself has no state to expose as opaque
}
