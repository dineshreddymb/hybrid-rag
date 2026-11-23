# Troubleshooting

Real problems hit while building this project, as **symptom → cause → fix**, followed by a few
generic ones. First moves for almost anything:

```powershell
.\tasks.ps1 status                                         # what is running / healthy
podman logs rag-backend --tail 50                          # what the service said (rag-frontend, rag-mcp, rag-redis, ...)
podman inspect rag-backend --format "{{json .State.Health}}"   # last healthcheck results + their output
podman run --rm docker.io/library/alpine echo ok           # does Podman itself work?
```

---

## 1. Every container fails with a netavark nftables error

**Symptom.** Every container, even a test one, fails to start:

```
netavark (exit code 1): nftables error: "nft" did not return successfully while applying ruleset
```

**Cause.** Podman 6 uses netavark 2, which sets up container networking with **nftables** rules,
including an inet-family `fib` expression. The old WSL2 kernel (6.6.87) lacked the kernel module for
it (`nft_fib_inet`), so the rule set was rejected. netavark 2.x no longer supports the iptables
driver, so there is no setting to fall back to. The Podman machine runs on the WSL kernel, so
the WSL version decides this.

**Fix.** Update WSL, restart it, start the machine:

```powershell
wsl --version              # before: WSL 2.6.3, kernel 6.6.87.2
wsl --update
wsl --shutdown
podman machine start
wsl --version              # after: WSL 3.0.5, kernel 6.18.58.1
podman run --rm docker.io/library/alpine echo ok
```

## 2. podman-compose crashes with `UnicodeDecodeError ... 0x97` reading `.env.langfuse`

**Symptom.** `podman-compose up` stops right away with a `UnicodeDecodeError` mentioning byte `0x97`
while reading `.env.langfuse`.

**Cause.** On Windows, Python's default text encoding is **cp1252**, not UTF-8. The generator wrote a
comment with an em dash (—), which is byte `0x97` in cp1252. podman-compose reads env files as UTF-8, and `0x97` is not valid UTF-8.

**Fix.** Always pass `encoding="utf-8"` when writing files from Python on Windows.
[gen_langfuse_env.py](../../scripts/gen_langfuse_env.py) now does this. To repair an existing file
**without** generating new secrets (don't use `--force` on an existing install, see [02](02-observability-langfuse.md)):

```powershell
$t = Get-Content .env.langfuse -Raw -Encoding Default      # read as ANSI (cp1252)
[IO.File]::WriteAllText("$PWD\.env.langfuse", $t)          # write back as UTF-8 without BOM
```

## 3. Langfuse web stays starting

**Symptom.** `podman ps` shows `rag-langfuse-web` as `(starting)` and later `(unhealthy)`. The logs look
normal and say the server is ready.

**Cause.** Langfuse web is a Next.js server, and Next.js listens on the address in `$HOSTNAME`. Inside a
container `HOSTNAME` is the container's own name/IP, so the server didn't listen on `127.0.0.1`.
The healthcheck runs `wget http://127.0.0.1:3000/api/public/health`, and that connection was refused.

**Fix.** In [compose.observability.yaml](../../compose.observability.yaml):

```yaml
langfuse-web:
  environment:
    HOSTNAME: 0.0.0.0
```

Then recreate it: `.\tasks.ps1 restart langfuse-web`.

## 4. A setting in `.env` is ignored or breaks parsing

**Symptom.** A value doesn't take effect, or the backend fails at start with a pydantic validation error
like `LLM_THINKING_BUDGET: Input should be a valid integer`.

**Cause.** Env files have **no inline comments**. In `KEY=value  # comment`, the comment and the spaces are part of the value.

**Fix.** Put comments on their own line (as [.env.example](../../.env.example) does), then recreate the
container. `podman restart` keeps the old environment:

```powershell
Select-String -Path .env -Pattern '^[A-Z0-9_]+=.*\s#'     # finds offending lines
.\tasks.ps1 restart backend
```

## 5. `HEALTHCHECK` in the Containerfile does nothing

**Symptom.** The image has a `HEALTHCHECK` line, but `podman ps` never shows `(healthy)`. The build prints a
warning that HEALTHCHECK is not supported for the OCI image format.

**Cause.** Podman builds **OCI-format** images by default. The OCI image spec has no healthcheck field,
so the instruction is dropped. Only Docker-format images keep it.

**Fix.** The healthchecks live in compose (`healthcheck:` per service), which works with any image format.
You can see the difference with the MCP image (its Containerfile used to have a `HEALTHCHECK` line, now removed with a comment pointing at compose):

```powershell
podman image inspect localhost/hybrid-rag-mcp:latest --format "{{.Config.Healthcheck}}"   # <nil>
podman inspect rag-mcp --format "{{.Config.Healthcheck}}"                                # from compose.yaml
```

(`podman build --format docker` would also keep it, but then the check is hidden in the image instead of next to the service.)

## 6. Langfuse API: `GET /api/public/traces/{id}` doesn't work

**Symptom.** A script that fetches a trace by id gets an error or nothing back from Langfuse v4.

**Cause.** Langfuse v4 runs in **events-only mode**. Data is stored as observations, and the legacy
trace endpoint `GET /api/public/traces/{id}` is gone.

**Fix.** Ask for the trace's observations instead (basic auth = public key : secret key):

```powershell
$pk = (Select-String .env -Pattern '^LANGFUSE_PUBLIC_KEY=(.*)').Matches.Groups[1].Value
$sk = (Select-String .env -Pattern '^LANGFUSE_SECRET_KEY=(.*)').Matches.Groups[1].Value
$auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${pk}:${sk}"))
$tid = "<trace id from /ask>"
Invoke-RestMethod -Headers @{ Authorization = "Basic $auth" } "http://localhost:3000/api/public/v2/observations?traceId=$tid"
```

## 7. `pytest tests/unit/test_ui_*.py` says the file doesn't exist

**Symptom.** A command with `*` works in bash but fails in PowerShell with "file or directory not found".

**Cause.** PowerShell does **not** expand `*` for native programs (`python.exe`, `podman.exe`). The program
gets the literal string `test_ui_*.py`, and Python on Windows doesn't expand it either.

**Fix.** List the files in PowerShell and pass them as an array, the way `tasks.ps1 test` does:

```powershell
$files = @(Get-ChildItem tests\unit\test_ui_*.py | ForEach-Object { $_.FullName })
frontend\.venv\Scripts\python.exe -m pytest @files -q
```

## 8. YAML error `mapping values are not allowed here` in compose.yaml

**Symptom.** `podman-compose up` stops while reading [compose.yaml](../../compose.yaml) with a YAML error such as
`mapping values are not allowed here`, pointing at the `redis` service's `environment:` block.

**Cause.** The line used compose's "required variable" syntax, `${VAR:?message}`. The message was written as
plain text and contained a colon followed by a space (`...: with colon`). In YAML, an unquoted value that
contains `: ` is read as the start of a new key/value pair, so the parser gives up. The same happens to
unquoted values with ` #`.

**Fix.** Put the whole value in quotes, as the file does now:

```yaml
environment:
  REDIS_PASSWORD: "${REDIS_PASSWORD:?REDIS_PASSWORD is empty. Run tasks.ps1 setup}"
```

If `REDIS_PASSWORD` is empty, compose now stops with that message. Run `.\tasks.ps1 setup`: `scripts/sync_env.py`
generates the password.

## 9. `.env` values with `${...}` are not expanded

**Symptom.** The backend cannot reach Redis (`/health` shows `redis` red, the log says `redis_unavailable`
with an authentication or connection error), although `REDIS_PASSWORD` is set. Or you suspect the literal text
`${REDIS_PASSWORD}` ends up in a connection string.

**Cause.** Neither podman-compose (for `env_file`) nor `podman kube play` expands `${...}` **inside the values of an
env file**. [.env.example](../../.env.example) contains `REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0`, and the
container receives exactly that text. `${...}` is expanded only in `compose.yaml` itself, not in the files it reads.

**Fix (already in the code).** The backend resolves the placeholder itself:
`redis_url_resolved` in [config.py](../../backend/backend_app/config.py) replaces `${REDIS_PASSWORD}` with the
`REDIS_PASSWORD` setting. In compose the backend also gets `REDIS_URL: redis://:${REDIS_PASSWORD:-}@redis:6379/0` in its
`environment:` block, where compose does expand it. To check what the container really sees:

```powershell
podman exec rag-backend python -c "import os; print(os.environ.get('REDIS_URL'))"
```

If you add your own settings that refer to other settings, resolve them in Python (or write the final value in `.env`).

## 10. A file called `NUL` appears in the project folder

**Symptom.** A file named `NUL` shows up in the repo root. Windows Explorer can't open or delete it, and some tools choke on it.

**Cause.** `NUL` is the Windows "discard output" device, but **Git Bash** (POSIX) doesn't know it. A command such as
`some-command > NUL` run in Git Bash creates a real file with that name. In PowerShell `> $null` (or `2>$null`) is the
right form, and in Git Bash it is `> /dev/null`.

**Fix.** Delete it with the extended path syntax, then use the right null device for the shell you are in:

```powershell
Remove-Item -LiteralPath "\\?\$PWD\NUL" -Force
```

---

## Generic problems

**Port already in use.** `up` fails with something like `bind: address already in use` for 8000,
8050, 3000, ... Another program, or the kube pods, hold the port:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object LocalAddress, OwningProcess
Get-Process -Id <OwningProcess>
podman ps -a --format "{{.Names}} {{.Ports}}"     # a forgotten container or pod?
podman pod ps                                     # backend / frontend pods still running? -> .\tasks.ps1 kube-down
```

**podman-compose prints a pod "name is in use" error.** podman-compose tries to create its pod
`pod_hybrid-rag` on every `up` / `run`. When the pod already exists, Podman complains that the name is in use, and
podman-compose carries on with the existing pod. Harmless. If the containers then start, ignore it.

**`podman-compose -f compose.yaml down` warns about `backend-net`.** At the end, `down` tries to remove the
network, but `langfuse-web` (from the other compose file) is still attached, so Podman refuses.
Harmless: the network is meant to stay. `.\tasks.ps1 kube-up` shows this every time.

**Backend is `degraded` / a check is red.** `/health` tells you which dependency:
`mongodb` / `qdrant` red means a core store is down, and `status` becomes `degraded`. `embeddings` red usually means a missing or
rate-limited `HF_TOKEN` (the result is cached 5 minutes). `langfuse` red is fail-open: answers still work,
only tracing is off until Langfuse is reachable. `redis` red is fail-open too: the backend just runs without the cache
(see [12](12-frontend-backend-and-cache.md)) and `status` stays `ok`.

**Changes don't show up.** Code change → `.\tasks.ps1 rebuild <svc>` (`backend`, `frontend`, `mcp-server`, ...). `.env` change → `.\tasks.ps1 restart <svc>`.
See [09](09-compose-everything.md).
