# syntax=docker/dockerfile:1
#
# HALucinator baseline for Figure 8a/8b: unmodified upstream HALucinator
# (pseclab/halucinator:5403321 = halucinator/halucinator @ 5403321, built
# with its own Dockerfile) plus the slice firmware, its configs and the
# PcUpdate / CounterVirt handler.
#
# BUILD (context = firmware_slicing/, the parent of this directory):
#     cd boardrunner/boardrunner_examples/microbenchmarks/firmware_slicing
#     docker build -f baseline/Dockerfile -t halucinator-slice:latest .
# RUN:
#     docker run --rm -v "$PWD/results:/results" halucinator-slice:latest pcupdate 10
#     docker run --rm -v "$PWD/results:/results" halucinator-slice:latest counter 10
FROM pseclab/halucinator@sha256:832ab994de7a90a79ce10a4cb9cfa24087a885fbb3e5a7c79bd4549b59e065c1

# Breakpoint handler that measures the elapsed time in-process.
COPY baseline/halucinator_configs/sentinel_log.py /root/halucinator/src/halucinator/bp_handlers/sentinel_log.py

# Firmware sources + HALucinator configs; build at N_INNER=1000 (paper).
WORKDIR /root/microbench
COPY firmware/microbench.c firmware/startup.s firmware/stm32_flash.ld ./
COPY baseline/halucinator_configs/*.yaml ./
ARG N_INNER=1000
RUN mkdir -p build && arm-none-eabi-gcc -mcpu=cortex-m4 -mthumb -mfloat-abi=soft \
        -Wall -O0 -g -ffreestanding -nostdlib -fno-common -fno-builtin \
        -DBENCH_SLICE -DN_INNER=${N_INNER} -DN_OUTER=1 \
        -T stm32_flash.ld -nostdlib -Wl,--gc-sections \
        startup.s microbench.c -o build/microbench_slice.axf \
    && arm-none-eabi-objcopy -O binary build/microbench_slice.axf build/microbench_slice.bin

COPY baseline/run.sh /opt/bin/run.sh
RUN chmod +x /opt/bin/run.sh && mkdir -p /results
ENTRYPOINT ["/opt/bin/run.sh"]
CMD ["pcupdate", "10"]
