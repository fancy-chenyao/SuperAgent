[CmdletBinding()]
param(
    [string]$HostAddress = "127.0.0.1",
    [int]$WebPort = 8001,
    [switch]$SkipMcp
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$outputDir = Join-Path $projectRoot "output"
$ports = @(8001, 8010, 8011, 8012)
$services = @(
    @{ Name = "remote-registry"; Script = "mock_remote_registry.py"; WorkDir = $projectRoot; Port = 8012; Log = "remote-registry" },
    @{ Name = "remote-tool"; Script = "mock_remote_tool_skill.py"; WorkDir = $projectRoot; Port = 8011; Log = "remote-tool" },
    @{ Name = "remote-agent"; Script = "mock_remote_agent.py"; WorkDir = $projectRoot; Port = 8010; Log = "remote-agent" }
)

if (-not $SkipMcp) {
    $ports = @(8000) + $ports
    $services = @(
        @{ Name = "excel-mcp"; Script = "__main__.py"; WorkDir = (Join-Path $projectRoot "src\tools\excel"); Port = 8000; Log = "excel-mcp" }
    ) + $services
}

$startedProcesses = @()

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python. Run 'uv sync' first."
}

$occupiedPorts = @(
    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in $ports } |
        Select-Object -ExpandProperty LocalPort -Unique
)

if ($occupiedPorts.Count -gt 0) {
    $portText = ($occupiedPorts | Sort-Object) -join ", "
    throw "Port(s) already in use: $portText. Run '.\stop-superagent.ps1' first."
}

New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

try {
    foreach ($service in $services) {
        $stdout = Join-Path $outputDir "$($service.Log)-stdout.log"
        $stderr = Join-Path $outputDir "$($service.Log)-stderr.log"
        $process = Start-Process `
            -FilePath $python `
            -ArgumentList $service.Script `
            -WorkingDirectory $service.WorkDir `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr `
            -PassThru
        $startedProcesses += $process
        Write-Host "[start] $($service.Name) on port $($service.Port)" -ForegroundColor Cyan
    }

    Start-Sleep -Seconds 3

    foreach ($process in $startedProcesses) {
        if ($process.HasExited) {
            throw "A background service failed to start. Check logs in the output directory."
        }
    }

    Write-Host "[start] web: http://${HostAddress}:$WebPort" -ForegroundColor Green
    Write-Host "Press Ctrl+C to stop web. Background services will be stopped automatically." -ForegroundColor Yellow
    & $python "cli.py" "web" "--host" $HostAddress "--port" $WebPort
}
finally {
    foreach ($process in $startedProcesses) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "Background services stopped." -ForegroundColor Yellow
}
