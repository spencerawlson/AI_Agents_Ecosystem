# Running Ventoryn in Docker (FedServ)

`Dockerfile` + `deploy/docker/ventoryn.sh`. Replaces the manual
`python launch.py all` in a terminal with a container that restarts on
crash and on reboot.

## How it is wired (and why)

| Concern | Choice |
|---|---|
| Cloudflare tunnel | `ventoryn.app → http://localhost:8000` (token-file tunnel). Container uses **host networking** and binds **127.0.0.1:8000**, so the tunnel target is unchanged and the LAN can no longer reach the app around Cloudflare. |
| Real visitor IP | Host networking keeps the tunnel's connection on loopback, which is the only peer allowed to set `CF-Connecting-IP` (rate limits, analytics). With bridge networking every visitor would look like one IP. |
| State | `data/` (incl. `ecosystem_state.db`) and `reports/` are bind-mounted from the repo; never in the image. `ECOSYSTEM_STATE_DB` is set to the mounted path. |
| Secrets | `.env.ventoryn` is mounted read-only and read by the app's own loader (`ECOSYSTEM_ENV_FILE`), which strips quotes like the shell did — Docker's `--env-file` would keep them. `.dockerignore` keeps every `.env*` out of the image. |
| SELinux (enforcing) | Mounts use `:z`. |
| File ownership | Container runs as the host user's uid/gid (1000). |
| Logs | `docker logs`, rotated at 20 MB × 5. |
| Video jobs | `ffmpeg` is in the image. |
| Screenshots | A second container, `ventoryn-shot` (same image, `python -m ventoryn.shot_service`, 127.0.0.1:8210). Chrome needs a relaxed seccomp filter for its sandbox, so the browser runs here — not in the app: no data, no `.env.ventoryn`, read-only root, all capabilities dropped, 1 GB / 1 CPU / 512 processes. The app reaches it with a shared token (`~/.config/ventoryn/shot.token`, created on first use). |

Docker Compose is not installed on the server (Fedora's `docker` package
does not ship it), so the script uses plain `docker run`.

## First cut-over

```bash
cd ~/Projects/AI_Agents_Ecosystem
git pull --ff-only
deploy/docker/ventoryn.sh build      # the running app is not touched
deploy/docker/ventoryn.sh cutover    # backup -> stop host process -> start container
deploy/docker/ventoryn.sh status
```

`cutover` takes a consistent backup into `~/ventoryn-backups/<time>/`
(SQLite online backup, JSON state, reports, env copy — folder mode 700),
stops the host process gracefully, starts the container and waits up to
two minutes for it to answer. If it does not, it prints the container
logs and **rolls back automatically**.

## Day-to-day

```bash
git pull --ff-only                   # first, so the newest script runs
deploy/docker/ventoryn.sh deploy     # git pull + rebuild + replace; previous image restored if unhealthy
deploy/docker/ventoryn.sh logs
deploy/docker/ventoryn.sh status
deploy/docker/ventoryn.sh backup
deploy/docker/ventoryn.sh shooter     # (re)start only the screenshot container
```

## Rollback to the old way

```bash
deploy/docker/ventoryn.sh rollback
```

Stops and removes the container and starts `.venv/bin/python launch.py all`
on the host (bound to 127.0.0.1, env from `.env.ventoryn`, log in
`~/ventoryn-host.log`). The `.venv` is kept for exactly this.

## Separate finding (not changed by this setup)

The cloudflared process for `api.road2cissp.com` (IT_Fund) is started
with `--token <value>` on its command line, readable by any local user via
`ps`. Rotate that tunnel token in the Cloudflare dashboard and run it with
`--token-file` like the `ventoryn.app` tunnel.
