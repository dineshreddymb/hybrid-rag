# 00 — Setup: Podman on Windows, images and containers

## Why

Containers need a Linux kernel. Windows doesn't have one, so Podman runs a small Linux
virtual machine inside **WSL2** called the *Podman machine*. The `podman` command you type in
PowerShell is only a client. It sends every command to that VM, and the containers really run there.

This explains a few things you will see later:

- Paths inside containers are Linux paths. Your `C:` drive appears in the VM as `/mnt/c`.
- Named volumes live on the VM's own disk, not in a Windows folder (see [01](01-data-services.md)).
- If WSL breaks or is out of date, *every* container fails (see [troubleshooting](troubleshooting.md#1-every-container-fails-with-a-netavark-nftables-error)).

## Images vs containers

| | Image | Container |
|---|---|---|
| What | A read-only template: OS files + Python + your code | A running (or stopped) copy of an image, with its own writable layer |
| Made by | `podman build` or `podman pull` | `podman run` / `podman-compose up` |
| Lifetime | Stays until you remove it | Disposable: remove and recreate it any time |
| Analogy | A class | An object (an instance of the class) |

One image can back many containers. Anything a container writes outside a volume is lost
when the container is removed. That is a feature: a fresh container is always a clean state.

## How: the Podman machine

```powershell
podman machine ls                 # is the VM running? CPUs, memory, disk
podman machine start              # start it (after a Windows reboot, for example)
podman machine stop
podman info --format "{{.Host.Kernel}}"   # kernel version of the VM
podman machine ssh                # a shell inside the VM (type exit to leave)
```

`podman machine ls` shows `MEMORY 2GiB` here. On WSL that number is not a hard limit: WSL2
VMs share memory under the limit set in `%UserProfile%\.wslconfig` (by default half your RAM).

## How: the basic commands

Podman wants **fully qualified image names**: `docker.io/library/alpine`, not just `alpine`.
That is why every image in [compose.yaml](../../compose.yaml) starts with `docker.io/...`.

```powershell
podman pull docker.io/library/alpine            # download an image
podman image ls                                 # list images
podman run --rm docker.io/library/alpine echo ok    # run once, remove when it exits
podman run -d --name sleeper docker.io/library/alpine sleep 600   # -d = in the background
podman ps                                       # running containers
podman ps -a                                    # also stopped ones
podman logs sleeper                             # what it printed (stdout/stderr)
podman logs -f rag-backend                          # follow logs live (Ctrl+C to stop)
podman exec -it sleeper sh                      # open a shell inside a running container
podman stop sleeper                             # stop it (it still exists)
podman rm sleeper                               # remove it
podman rm -f sleeper                            # stop + remove in one go
podman image prune                              # delete dangling <none> images left by rebuilds
```

`podman run --rm docker.io/library/alpine echo ok` is also the fastest way to check that
Podman itself works. If it prints `ok`, the VM, networking and image storage are fine.

## How: the project venv and podman-compose

`podman-compose` is a Python program that reads `compose.yaml` and turns it into `podman`
commands. It is installed in the project venv, not globally:

```powershell
.\tasks.ps1 setup
```

What [tasks.ps1](../../tasks.ps1) `setup` does:

1. Creates `.venv` with `py -3.12 -m venv .venv` (the venv has Python 3.12).
2. Installs `podman-compose` and the backend package with its dev extras (`pytest`) into it.
3. Runs [scripts/sync_env.py](../../scripts/sync_env.py): creates `.env` from
   [.env.example](../../.env.example) if it doesn't exist, adds keys that were added to the example
   later (your values are never changed) and generates `REDIS_PASSWORD`.
4. Runs [scripts/gen_langfuse_env.py](../../scripts/gen_langfuse_env.py) to create `.env.langfuse`
   and put the Langfuse SDK keys into `.env` (see [02](02-observability-langfuse.md)).

After that, every compose task in `tasks.ps1` calls `.venv\Scripts\podman-compose.exe`. You can
call it directly too:

```powershell
.\.venv\Scripts\podman-compose.exe version
.\.venv\Scripts\podman-compose.exe -f compose.yaml ps
```

If PowerShell refuses to run `tasks.ps1` ("running scripts is disabled"), allow local scripts
once for your user: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

The frontend, MCP and eval folders have their own test venvs (`frontend\.venv`, `mcp-server\.venv`, `eval\.venv`).
`.\tasks.ps1 test` uses them when they exist and skips those tests otherwise.

## Try it

1. `podman machine ls`: the machine should say *Currently running*.
2. `podman run --rm docker.io/library/alpine cat /etc/os-release`: an Alpine Linux container
   prints its OS. You are on Windows, but the process ran in Linux.
3. Start a background container, look at it, then remove it:
   ```powershell
   podman run -d --name sleeper docker.io/library/alpine sleep 600
   podman ps
   podman exec sleeper ps            # only "sleep" runs in there: a container is one process tree
   podman rm -f sleeper
   podman ps -a                      # gone
   podman image ls docker.io/library/alpine   # the image is still there
   ```
4. Write a file inside a container, remove the container, start a new one: the file is gone.
   ```powershell
   podman run --name t1 docker.io/library/alpine sh -c "echo hi > /tmp/x; cat /tmp/x"
   podman rm t1
   podman run --rm docker.io/library/alpine cat /tmp/x     # No such file
   ```

## What to check

- [ ] `podman run --rm docker.io/library/alpine echo ok` prints `ok`.
- [ ] `.\.venv\Scripts\podman-compose.exe version` prints a version (1.6.0 here).
- [ ] `.env` and `.env.langfuse` exist and are listed in [.gitignore](../../.gitignore).
- [ ] You can explain in one sentence why removing a container doesn't lose project data
      (answer in [01](01-data-services.md)).
