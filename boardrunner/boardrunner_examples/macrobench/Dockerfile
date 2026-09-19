# syntax=docker/dockerfile:1
#
# Figure 9 macrobench images. Built twice from the two pinned base images:
#   BASE=pseclab/halucinator@sha256:832ab994...    BACKEND=halucinator     -> halucinator-macro
#   BASE=pseclab/halucinator-fd@sha256:2ce475b5... BACKEND=halucinator-fd  -> halucinator-fd-macro
# Adds the in-container runner, UART peer, firmware specs, shipped results
# and the summary/figure scripts. Build (context = this directory):
#   docker build --build-arg BASE=... --build-arg BACKEND=... -t <name> .
ARG BASE
FROM ${BASE}
ARG BACKEND
ENV BACKEND=${BACKEND}
RUN pip3 install --no-cache-dir tomli matplotlib pyyaml >/dev/null
# Pre-compile Python bytecode so the first run in a fresh container does not
# pay for it (the image is otherwise used exactly as the paper's containers).
RUN python3 -m compileall -q /root/halucinator/src /usr/local/lib/python3*/dist-packages \
        /usr/lib/python3/dist-packages >/dev/null 2>&1 || true
COPY macro_run.py summarize.py make_macro_figure.py firmwares.toml run.sh /opt/macrobench/
COPY peers /opt/macrobench/peers
COPY stats /opt/macrobench/stats
COPY results/paper /opt/macrobench/results/paper
RUN chmod +x /opt/macrobench/run.sh && mkdir -p /results
WORKDIR /root/halucinator
ENTRYPOINT ["/opt/macrobench/run.sh"]
CMD ["all", "10"]
