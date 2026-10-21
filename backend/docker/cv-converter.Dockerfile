FROM debian:bookworm-slim

ENV HOME=/tmp/cv-converter-home \
    SAL_USE_VCLPLUGIN=svp \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update \
    && apt-get install -y --no-install-recommends coreutils libreoffice-writer \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin cvconverter \
    && mkdir -p /tmp/cv-converter-home /input /output \
    && chown -R 10001:10001 /tmp/cv-converter-home /input /output

USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["/usr/bin/timeout", "--signal=TERM", "--kill-after=2s", "25s", "soffice"]
