# Running mcsm in Docker

The image holds mcsm and Python. mcsm downloads Minecraft, the mod loader, mods and Java
into `/data` when you create a server. Keep `/data` on a volume and your servers survive
image updates.

## Start it

```sh
docker run -d --name mcsm --restart unless-stopped \
  -p 8765:8765 -p 25565:25565 -p 8798:8798 \
  -v mcsm-data:/data \
  -e MCSM_LAN_IP=192.168.1.20 \
  ghcr.io/silverwrx03/mc-server-management:latest
```

Or use [`docker-compose.yml`](../docker-compose.yml): `docker compose up -d`.

| Port | What |
|---|---|
| 8765 | the control panel. Open `http://<this computer>:8765`. **Don't forward this one on your router.** |
| 25565 | Minecraft. Each extra server needs its own port: map a range, e.g. `-p 25565-25570:25565-25570`. |
| 8798 | friends' downloads (the invite links), only needed if you use them |

`MCSM_LAN_IP` is the Docker host's address on your network. Inside a container mcsm can't
see it, and it goes into invite links and the phone-pairing QR code.

## First sign-in

A container has no screen, so the control panel is only reachable from other devices. Those
need a strong password (12+ characters, upper and lower case, and a special character). On the
first start mcsm makes a **one-time password** and prints it:

```sh
docker logs mcsm | grep Password
```

Sign in with it and choose your own. Until you do, it can't be used for anything else. You
can also set a strong password up front with `-e MCSM_INITIAL_PASSWORD='Your-Strong-Pass1'`.
It's only used when `/data` is new.

Forgot the password? Stop the container, delete `/data/.mcsm/web-auth.json` in the volume,
and start it again for a new one-time password.

## Memory

Give the container at least the memory your servers use, plus about 1 GB. With
`--memory`, leave that headroom or the kernel stops the server abruptly.

## Updating

```sh
docker pull ghcr.io/silverwrx03/mc-server-management:latest
docker rm -f mcsm && docker run ...   # same command as before; /data keeps everything
```

mcsm's own "update mcsm" button doesn't apply to containers: update the image instead.

## Stopping

`docker stop mcsm` asks mcsm to stop. Every server saves its world and shuts down cleanly.
Allow it time: `docker stop -t 120 mcsm`, or `stop_grace_period` in compose.

## Building it yourself

```sh
docker build -t mcsm .
```
