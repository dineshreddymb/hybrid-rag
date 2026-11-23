# 01 — Data services: MongoDB, Qdrant, persistence and networks

Files: [compose.yaml](../../compose.yaml) (services `mongodb`, `qdrant`, `mongo-express`; `redis` is in [12](12-frontend-backend-and-cache.md)),
[qdrant_store.py](../../backend/backend_app/vectorstore/qdrant_store.py), [mongo.py](../../backend/backend_app/db/mongo.py).

| Service | Image | Port on your PC | Data lives in |
|---|---|---|---|
| `mongodb` | `docker.io/library/mongo:8` | `127.0.0.1:27017` | named volume `mongo_data` |
| `qdrant` | `docker.io/qdrant/qdrant:v1.20.2` | `127.0.0.1:6333` (REST + dashboard), `127.0.0.1:6334` (gRPC) | named volume `qdrant_storage`, snapshots in `data\qdrant_snapshots` |
| `mongo-express` (profile `dev`) | `docker.io/library/mongo-express:1` | `127.0.0.1:8081` | nothing (it is a viewer) |

## Ports, and why they bind to 127.0.0.1

`ports: "127.0.0.1:27017:27017"` means *host address : host port : container port*.

- Without the `127.0.0.1:` prefix, Podman listens on **all** network interfaces. Anyone on your
  Wi-Fi could then reach MongoDB, which has no password here.
- With the prefix, only programs on your own PC can connect. That's enough for a browser, PowerShell or Compass.

Containers don't use published ports to talk to each other. They use the network (below).

## Named volumes vs bind mounts

| | Named volume | Bind mount |
|---|---|---|
| Compose syntax | `mongo_data:/data/db` | `./data/docs:/data/docs` |
| Where the files are | On the Podman VM's Linux disk (`/var/lib/containers/storage/volumes/...`) | In a Windows folder, shared into the VM via `/mnt/c/...` |
| Can you see them in Explorer? | No | Yes |
| Speed / safety for databases | Fast, real Linux file system | Slower; file locking and `fsync` go through the WSL file share |
| Good for | Database files | Files *you* edit or want to see: docs, snapshots, reports |

Compose prefixes volume names with the project name (`name: hybrid-rag`), so `podman volume ls`
shows `hybrid-rag_mongo_data` and `hybrid-rag_qdrant_storage`. You will also see volumes
with long hex names. Those are anonymous volumes that images declare themselves
(for example Mongo's `/data/configdb`).

### Why Qdrant uses a named volume

Qdrant's docs warn that putting its storage on a Windows folder mounted through WSL can
**corrupt the collection**, because that file share doesn't behave like a Linux disk for
memory-mapped files and locks. So:

- live storage: `qdrant_storage:/qdrant/storage` (named volume, safe), and
- backups: `./data/qdrant_snapshots:/qdrant/snapshots` (bind mount, visible in Explorer).

A snapshot is one file that holds a full copy of a collection. Qdrant writes it once, so the
bind mount's weaknesses don't matter.

## Networks and DNS by service name

The compose files define three networks (the full picture is in [12](12-frontend-backend-and-cache.md)).
The data services here sit on **`backend-net`**, together with the backend:

```yaml
mongodb:
  networks: [backend-net]
```

Podman runs a DNS server for each network. A container can reach another by its **service
name** (`mongodb`, `qdrant`, `redis`, `langfuse-web`), but only if they share a network. That's
why the backend is configured with `MONGO_URI: mongodb://mongodb:27017` and
`QDRANT_URL: http://qdrant:6333`, and why the frontend container cannot even resolve `mongodb`.
Inside a container, `localhost` means *that container itself*, not your PC.

## Healthchecks

A container can be *running* and still not ready (Mongo takes a few seconds to open its files).
A healthcheck is a command Podman runs inside the container on a timer:

```yaml
healthcheck:
  test: ["CMD", "mongosh", "--quiet", "--eval", "db.adminCommand('ping').ok"]
  interval: 10s      # run every 10 s
  timeout: 5s        # a run that takes longer counts as failed
  retries: 10        # unhealthy after 10 failures in a row
  start_period: 20s  # failures in the first 20 s don't count
```

The Qdrant image has no `curl`, so its check uses bash to open a TCP connection:
`bash -c "exec 3<>/dev/tcp/127.0.0.1/6333"`. `podman ps` shows the result as `(healthy)`.
The `backend` waits for both (and for `redis`) with `depends_on: condition: service_healthy`.

## Try it

**1. Data survives deleting the containers**

```powershell
Invoke-RestMethod http://localhost:8000/stats      # note documents / chunks / qdrant_points
.\tasks.ps1 down                                   # removes ALL containers (volumes are kept)
podman ps -a                                       # no rag-* containers
podman volume ls                                   # hybrid-rag_mongo_data etc. still there
.\tasks.ps1 up
.\tasks.ps1 status                                 # wait until everything is (healthy)
Invoke-RestMethod http://localhost:8000/stats      # same numbers as before
```

`podman-compose down` keeps volumes. `down -v` would delete them, so don't use `-v` unless you
really want to start from zero.

**2. Look inside a volume and the network**

```powershell
podman volume inspect hybrid-rag_qdrant_storage     # "Mountpoint" is a path inside the VM
podman network inspect backend-net                  # subnet + which containers are attached
podman exec rag-backend python -c "import socket; print(socket.gethostbyname('mongodb'))"
```

The last command prints the IP that Podman's DNS gives the name `mongodb` on backend-net.

**3. Take a Qdrant snapshot**

```powershell
.\tasks.ps1 qdrant-backup
Get-ChildItem data\qdrant_snapshots\chunks_bge_small_en_v1_5
```

This calls `POST /admin/qdrant/snapshot` on the backend. The `.snapshot` file (plus a `.checksum`)
appears in a sub-folder named after the collection. You can also open
http://localhost:6333/dashboard, pick the collection and look at its *Snapshots* tab.

*Restore:* `.\tasks.ps1 qdrant-restore` with no argument lists the snapshots; with a file name it
asks Qdrant to recover the collection from that snapshot file. This **replaces** the live
collection, so only try it on data you can re-ingest:

```powershell
.\tasks.ps1 qdrant-restore                          # lists the snapshots of the collection
.\tasks.ps1 qdrant-restore <snapshot-file-name>     # PUT .../snapshots/recover
```

If the snapshot is older than MongoDB, run `.\tasks.ps1 ingest force` afterwards.

**4. Browse MongoDB**

```powershell
.\tasks.ps1 dev-tools          # starts mongo-express (profile "dev")
```

Open http://localhost:8081 → database `hybrid_rag`. Look at `documents`, `chunks`,
`messages`, `app_logs`. Or from the command line:

```powershell
podman exec rag-mongodb mongosh hybrid_rag --quiet --eval "db.chunks.countDocuments()"
```

## What to check

- [ ] After `down` + `up`, `/stats` shows the same `documents`, `chunks` and `qdrant_points`.
- [ ] `podman ps` shows `127.0.0.1:` in front of every published port.
- [ ] A `.snapshot` file exists under `data\qdrant_snapshots\`.
- [ ] You can explain why Mongo and Qdrant use named volumes but `data\docs` is a bind mount.
