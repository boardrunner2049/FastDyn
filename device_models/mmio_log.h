#pragma once

void dev_log_start(void);
void dev_log_stop(void);
void dev_log_printf(const char *format, ...) __attribute__((format(printf, 1, 2)));
