[CmdletBinding()]
param(
    [string]$HostAddress = "127.0.0.1",
    [int]$WebPort = 8001,
    [switch]$SkipMcp
)

$ErrorActionPreference = "Stop"

# Keep normal startup fail-safe defaults unchanged. This launcher explicitly
# enables the governed TaskGraph path for local review/demo. APP_ENV=production
# prevents development dotenv loading from overwriting these process values,
# while still allowing unset model/provider settings to come from .env.
$env:APP_ENV = "production"
$env:S_ABAC_ENABLED = "true"
$env:ORCHESTRATION_SCHEDULER_ENABLED = "true"
$env:AUTO_RECOVERY_ENABLED = "true"
$env:SCHEDULER_AUTO_RECOVERY_MAX_ATTEMPTS = "1"
$env:SCHEDULER_RETRY_BASE_SECONDS = "0.5"
$env:SCHEDULER_RETRY_MAX_SECONDS = "4"
$env:SCHEDULER_RETRY_JITTER_RATIO = "0.2"

$arguments = @{
    HostAddress = $HostAddress
    WebPort = $WebPort
}
if ($SkipMcp) {
    $arguments.SkipMcp = $true
}

Write-Host "[governance] S-ABAC and TaskGraph Scheduler enabled." -ForegroundColor Green
Write-Host "[governance] DAG-aware recovery enabled (safe reads / confirmed pre-side-effect failures only)." -ForegroundColor Green
& (Join-Path $PSScriptRoot "start-superagent.ps1") @arguments
