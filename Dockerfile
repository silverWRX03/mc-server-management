# mcsm in a container: the control panel and your Minecraft servers.
#
#   docker run -d --name mcsm -p 8765:8765 -p 25565:25565 -p 8798:8798 \
#     -v mcsm-data:/data -e MCSM_LAN_IP=192.168.1.20 ghcr.io/silverwrx03/mc-server-management
#
# Everything (servers, worlds, backups, the Java that mcsm downloads) lives in /data, so keep
# that volume. See docs/docker.md.
FROM python:3.12-slim

# mcsm needs nothing but Python: it downloads Java (Eclipse Temurin) for each Minecraft version itself.
RUN useradd --create-home --uid 1000 mcsm && mkdir /data && chown mcsm:mcsm /data

COPY --chown=mcsm:mcsm . /tmp/mcsm-src
RUN pip install --no-cache-dir /tmp/mcsm-src && rm -rf /tmp/mcsm-src

USER mcsm
ENV MCSM_HOME=/data \
    MCSM_CONTAINER=1 \
    PYTHONUNBUFFERED=1
VOLUME /data
WORKDIR /data
# 8765: the control panel; 25565: Minecraft (add more for more servers); 8798: friends' downloads
EXPOSE 8765 25565 8798
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8765/api/auth', timeout=4)" || exit 1
# mcsm stops every server cleanly (saving worlds) on SIGTERM, i.e. `docker stop`.
STOPSIGNAL SIGTERM
CMD ["mcsm", "start", "--no-browser", "--web-host", "0.0.0.0"]
