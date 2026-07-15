$ErrorActionPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Main = Join-Path $Root 'main.py'
$Config = Join-Path $Root 'config.toml'
$OutLog = Join-Path $Root 'data\actuator.out.log'
$ErrLog = Join-Path $Root 'data\actuator.err.log'
$WatchLog = Join-Path $Root 'data\actuator.watch.log'
$ActuatorApi = 'http://127.0.0.1:8912/api/ui-automation/actuators/list_actuators/'

function Write-WatchLog {
  param([string]$Message)
  $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  Add-Content -Path $WatchLog -Value "$timestamp $Message"
}

function Get-ActuatorProcesses {
  Get-CimInstance Win32_Process |
    Where-Object {
      $_.CommandLine -like '*WHartTest_Actuator*main.py*' -or
      $_.CommandLine -like '*main.py --no-gui --config config.toml*'
    }
}

function Get-OnlineActuatorCount {
  try {
    $response = Invoke-RestMethod -Uri $ActuatorApi -TimeoutSec 8
    if ($null -ne $response.data -and $null -ne $response.data.data -and $null -ne $response.data.data.count) {
      return [int]$response.data.data.count
    }
    return 0
  } catch {
    Write-WatchLog "check failed: $($_.Exception.Message)"
    return -1
  }
}

function Stop-ActuatorProcesses {
  Get-ActuatorProcesses | ForEach-Object {
    Write-WatchLog "stopping stale process pid=$($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force
  }
}

function Start-Actuator {
  Write-WatchLog 'starting actuator'
  $arguments = @($Main, '--no-gui', '--config', $Config)
  Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
}

Write-WatchLog 'watcher started'

while ($true) {
  $processes = @(Get-ActuatorProcesses)
  $onlineCount = Get-OnlineActuatorCount

  if ($processes.Count -eq 0 -or $onlineCount -eq 0) {
    Write-WatchLog "restart required: process_count=$($processes.Count), online_count=$onlineCount"
    Stop-ActuatorProcesses
    Start-Sleep -Seconds 2
    Start-Actuator
    Start-Sleep -Seconds 10
  }

  Start-Sleep -Seconds 30
}
