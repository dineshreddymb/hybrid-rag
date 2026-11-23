<#
Shortcuts for the hybrid RAG stack.  Usage:  .\tasks.ps1 <task> [args]

  setup               create .venv, install podman-compose + dev deps, create/update .env (incl. Redis
                      password) and the Langfuse secrets
  up                  start everything (app + Langfuse)
  up-app              start only the app (frontend, mcp-server, backend, mongodb, qdrant, redis)
  up-obs              start only Langfuse
  down                stop and remove all containers (volumes / data are kept)
  status              containers + health
  logs <service>      follow logs of one service, e.g.  .\tasks.ps1 logs backend
  rebuild <service>   rebuild one image and recreate its container, e.g.  .\tasks.ps1 rebuild backend
  restart <service>   recreate one container (e.g. after editing .env)
  ingest [force]      re-scan data/docs   (force = re-embed every chunk)
  test                unit tests (no containers needed)
  itest               integration tests against the running stack
  eval [args]         run the RAGAS job, e.g.  .\tasks.ps1 eval --mode all --no-ragas
  qdrant-backup       write a Qdrant snapshot to data\qdrant_snapshots
  qdrant-restore [snapshot-file-name]   restore a snapshot (no name = list them)
  cache-stats         Redis cache hits / misses / keys / memory
  cache-clear [kind]  empty the cache: answer | search | embedding | health | all (default all)
  dev-tools           start mongo-express on http://localhost:8081
  kube-up             run the app as two Podman pods (backend, frontend); stops the compose app first
  kube-down           stop both pods and start the compose app again
#>
param(
    [Parameter(Position = 0)] [string]$Task = "help",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)] [string[]]$Rest
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Compose = Join-Path $PSScriptRoot ".venv\Scripts\podman-compose.exe"
$AllFiles = @("-f", "compose.yaml", "-f", "compose.observability.yaml")
$Api = "http://localhost:8000"

function Need-Venv {
    if (-not (Test-Path $Compose)) { throw "Run '.\tasks.ps1 setup' first (no .venv / podman-compose)." }
}

# podman-compose prints harmless warnings on stderr (e.g. "pod still has containers" when the
# Langfuse stack shares the project); PowerShell would treat them as fatal errors.
function Invoke-Compose([string[]]$ComposeArgs) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Compose @ComposeArgs 2>&1 | ForEach-Object { "$_" }
    $ErrorActionPreference = $old
}

function Get-EnvValue([string]$Name, [string]$Default = "") {
    if (Test-Path ".env") {
        $line = Select-String -Path ".env" -Pattern "^$Name=(.*)$" | Select-Object -First 1
        if ($line) { return $line.Matches[0].Groups[1].Value.Trim() }
    }
    return $Default
}

switch ($Task) {
    "setup" {
        if (-not (Test-Path $Py)) { py -3.12 -m venv .venv }
        & $Py -m pip install --quiet --upgrade pip
        & $Py -m pip install --quiet podman-compose -e "backend[dev]"
        & $Py scripts/sync_env.py          # .env from .env.example, new keys, REDIS_PASSWORD
        & $Py scripts/gen_langfuse_env.py  # Langfuse secrets + SDK keys in .env
        Write-Host "Add HF_TOKEN and GOOGLE_API_KEY to .env, then:  .\tasks.ps1 up"
    }
    "up"        { Need-Venv; & $Compose @AllFiles up -d }
    "up-app"    { Need-Venv; & $Compose -f compose.yaml up -d }
    "up-obs"    { Need-Venv; & $Compose -f compose.observability.yaml up -d }
    "down"      { Need-Venv; Invoke-Compose ($AllFiles + "down") }
    "status"    {
        podman ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
        try { (Invoke-RestMethod "$Api/health").checks | Format-List } catch { Write-Host "Backend not reachable at $Api" }
    }
    "logs"      { Need-Venv; & $Compose @AllFiles logs -f $Rest }
    "rebuild"   {
        Need-Venv
        if (-not $Rest) { throw "usage: .\tasks.ps1 rebuild <service>" }
        & $Compose @AllFiles build $Rest
        & $Compose @AllFiles up -d --force-recreate --no-deps $Rest
    }
    "restart"   {
        Need-Venv
        if (-not $Rest) { throw "usage: .\tasks.ps1 restart <service>" }
        & $Compose @AllFiles up -d --force-recreate --no-deps $Rest
    }
    "ingest"    {
        $force = if ($Rest -contains "force") { "?force=true" } else { "" }
        Invoke-RestMethod -Method Post "$Api/ingest/scan$force" | ConvertTo-Json -Depth 5
    }
    "test"      {
        & $Py -m pytest tests/unit -q -p no:cacheprovider --ignore-glob="tests/unit/test_ui_*" --ignore-glob="tests/unit/test_mcp_*" --ignore-glob="tests/unit/test_eval_*"
        # each service has its own venv; test file prefix -> folder
        $suites = [ordered]@{ ui = "frontend"; mcp = "mcp-server"; eval = "eval" }
        foreach ($prefix in $suites.Keys) {
            $dir = $suites[$prefix]
            $venvPy = "$dir\.venv\Scripts\python.exe"
            # PowerShell does not expand wildcards for native programs, so list the files ourselves
            $files = @(Get-ChildItem "tests\unit\test_${prefix}_*.py" | ForEach-Object { $_.FullName })
            if ((Test-Path $venvPy) -and $files.Count) { & $venvPy -m pytest @files -q -p no:cacheprovider }
            else { Write-Host "skip $dir tests (no $dir\.venv - run: py -3.12 -m venv $dir\.venv; $dir\.venv\Scripts\python -m pip install -e `"$dir[dev]`")" }
        }
    }
    "itest"     {
        $mcpPy = "mcp-server\.venv\Scripts\python.exe"
        if (Test-Path $mcpPy) { & $mcpPy -m pytest tests/integration -q -p no:cacheprovider }
        else { & $Py -m pytest tests/integration -q -p no:cacheprovider }
    }
    "eval"      { Need-Venv; & $Compose --profile eval run --rm eval @Rest }
    "qdrant-backup" { Invoke-RestMethod -Method Post "$Api/admin/qdrant/snapshot"; Get-ChildItem data\qdrant_snapshots -Recurse -File | Select-Object FullName, Length }
    "qdrant-restore" {
        $collection = Get-EnvValue "QDRANT_COLLECTION" "chunks_bge_small_en_v1_5"
        if (-not $Rest) {
            Write-Host "Snapshots of '$collection':"
            (Invoke-RestMethod "http://localhost:6333/collections/$collection/snapshots").result | Select-Object name, size
            Write-Host "Restore one with:  .\tasks.ps1 qdrant-restore <name>"
        } else {
            $body = @{ location = "file:///qdrant/snapshots/$collection/$($Rest[0])" } | ConvertTo-Json
            Invoke-RestMethod -Method Put "http://localhost:6333/collections/$collection/snapshots/recover?wait=true" -ContentType "application/json" -Body $body | ConvertTo-Json
            Write-Host "Restored. If the snapshot is older than MongoDB, run: .\tasks.ps1 ingest force"
        }
    }
    "cache-stats" { Invoke-RestMethod "$Api/admin/cache/stats" | ConvertTo-Json -Depth 5 }
    "cache-clear" {
        $kind = if ($Rest) { $Rest[0] } else { "all" }
        Invoke-RestMethod -Method Post "$Api/admin/cache/clear?kind=$kind" | ConvertTo-Json -Depth 5
    }
    "dev-tools" { Need-Venv; & $Compose --profile dev up -d mongo-express; Write-Host "mongo-express: http://localhost:8081" }
    "kube-up"   {
        Need-Venv
        Invoke-Compose @("-f", "compose.yaml", "down")
        foreach ($net in @("frontend-net", "backend-net")) {
            podman network exists $net
            if ($LASTEXITCODE -ne 0) { podman network create $net | Out-Null }
        }
        & $Py deploy/kube/make_secret.py
        podman kube play --replace deploy/kube/secret.yaml
        podman kube play --replace --network frontend-net:alias=backend --network backend-net deploy/kube/backend.yaml
        podman kube play --replace --network frontend-net deploy/kube/frontend.yaml
    }
    "kube-down" {
        Need-Venv
        podman kube down deploy/kube/frontend.yaml
        podman kube down deploy/kube/backend.yaml
        podman kube down deploy/kube/secret.yaml
        Invoke-Compose @("-f", "compose.yaml", "up", "-d")
    }
    default     { Get-Content $PSCommandPath -TotalCount 31 | Select-Object -Skip 1 }
}
