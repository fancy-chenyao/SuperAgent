[CmdletBinding()]
param()

$projectRoot = $PSScriptRoot
$ports = @(8000, 8001, 8010, 8011, 8012)
$listeners = @(
    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in $ports }
)

if ($listeners.Count -eq 0) {
    Write-Host "No running SuperAgent services found." -ForegroundColor Yellow
    exit 0
}

$stopped = 0
$knownServiceScripts = @(
    "cli.py web",
    "mock_remote_registry.py",
    "mock_remote_tool_skill.py",
    "mock_remote_agent.py",
    "src\tools\excel",
    "tools\excel",
    "__main__.py"
)

foreach ($processId in ($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($null -eq $processInfo) {
        continue
    }

    $commandLine = [string]$processInfo.CommandLine
    $executablePath = [string]$processInfo.ExecutablePath
    $isProjectVenv = $executablePath -like "$projectRoot*"
    $isKnownService = $false
    foreach ($scriptName in $knownServiceScripts) {
        if ($commandLine -like "*$scriptName*") {
            $isKnownService = $true
            break
        }
    }

    if ($isProjectVenv -or $isKnownService) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        Write-Host "[stop] PID $processId" -ForegroundColor Cyan
        $stopped++
    }
}

if ($stopped -eq 0) {
    Write-Warning "Ports are occupied, but no SuperAgent process under this project was stopped."
} else {
    Write-Host "SuperAgent services stopped." -ForegroundColor Green
}
