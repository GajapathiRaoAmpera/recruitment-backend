# Judge0 (code runs)

The API runs candidate code on a self-hosted [Judge0](https://github.com/judge0/judge0) (FR-15).
You don't install Judge0: this folder holds a Docker setup, started with one command.

## Why our own image

Stock Judge0 1.13.1 (the newest release) ships the sandbox `isolate` 1.8.1, which only supports
cgroup v1. On any current host (cgroup v2: recent Ubuntu, Debian, Fedora, Docker Desktop) every
run fails with `Internal Error` / `Failed to create control group`. Our image
(`infra/judge0/Dockerfile`) is Judge0 1.13.1 plus `isolate` 2.7 (cgroup v2), two small patches,
and an entrypoint that prepares the container's cgroup. Details are in the Dockerfile.

## Run it

Needs Docker with Compose v2. Tested on Linux with cgroup v2 (`stat -fc %T /sys/fs/cgroup`
prints `cgroup2fs`).

```bash
cp .env.example .env            # skip if you already have one
# set JUDGE0_AUTH_TOKEN in .env to a random string:  openssl rand -hex 24
make judge0-up                  # first time builds the image (a few minutes), then starts 4 containers
```

`JUDGE0_URL` (default `http://localhost:2358`) and `JUDGE0_AUTH_TOKEN` in `.env` are what the API
uses. Stop with `make judge0-down`.

Check it works:

```bash
curl -s -H "X-Auth-Token: $JUDGE0_AUTH_TOKEN" localhost:2358/languages | head -c 200
```

## What runs

| Container | What | Notes |
| --- | --- | --- |
| `server` | Judge0 web API | published on `127.0.0.1:2358` only; every request needs `X-Auth-Token` |
| `workers` | runs the code in the sandbox | `privileged` (isolate needs namespaces and cgroups); `JUDGE0_WORKERS` (default 4) runs in parallel |
| `db`, `redis` | Judge0's own Postgres and Redis | internal network only, not the app's database |

Behaviour worth knowing:

- Languages and ids live in `app/execution/languages.py` (Python, JavaScript, C, C++, Java, Go,
  SQL on SQLite). Ids come from `GET /languages` on this image; re-check them if the image changes.
- `?wait=true` is switched off: that would run code inside the (unprivileged) web container.
  The client in `app/execution/judge0.py` submits a batch and polls for it, with a 15 s limit.
- Always ask for results with `base64_encoded=true`; Judge0 answers 400 for plain-text output that
  isn't valid UTF-8 (compiler messages often are).
- Every run carries cpu / wall / memory limits from the language map.

## Troubleshooting

- `required variable JUDGE0_AUTH_TOKEN is missing`: set it in `.env`.
- `Internal Error` on every run, `Failed to create control group ... Read-only file system` in
  the logs: the container isn't privileged or the host isn't cgroup v2.
- Port 2358 busy: stop whatever uses it; the port is fixed in `compose.yml`.
- Start from zero (drops Judge0's database): `docker compose -f infra/judge0/compose.yml down -v`.
