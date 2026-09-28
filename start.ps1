param([ValidateRange(1,65535)][int]$Port = 8000, [string]$ListenAddress = '127.0.0.1', [switch]$Restart, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $env:PYTHONUTF8 = '1'
    $pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Python environment missing. Run uv sync --locked in the project folder.' }
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0 -and $Restart) {
        $targets = @()
        foreach ($servicePid in ($listeners.OwningProcess | Select-Object -Unique)) {
            $service = Get-CimInstance Win32_Process -Filter "ProcessId = $servicePid"
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($service.ParentProcessId)"
            $owned = ($service.ExecutablePath -eq $pythonPath) -or ($parent.ExecutablePath -eq $pythonPath)
            if (-not $owned -or $service.CommandLine -notmatch '\s-m\s+contractdb\s+serve\b') { throw "Port $Port belongs to another application. Nothing was stopped." }
            $targets += $servicePid
        }
        foreach ($servicePid in $targets) { Stop-Process -Id $servicePid -ErrorAction Stop }
        $deadline = (Get-Date).AddSeconds(15)
        while (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
            if ((Get-Date) -gt $deadline) { throw 'The old service did not release its port.' }
            Start-Sleep -Milliseconds 250
        }
        $listeners = @()
    }
    if ($listeners.Count -eq 0) {
        $logDir = Join-Path $PSScriptRoot '.qa'
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        $server = Start-Process -FilePath $pythonPath -ArgumentList '-m','contractdb','serve','--host',$ListenAddress,'--port',$Port -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir 'server.stdout.log') -RedirectStandardError (Join-Path $logDir 'server.stderr.log')
    }
    $deadline = (Get-Date).AddSeconds(30)
    do {
        try { $health = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2 } catch { $health = $null }
        if ($health.status -eq 'ok') { break }
        if ($server -and $server.HasExited) { throw 'Service exited. See .qa/server.stderr.log for details.' }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if ($health.status -ne 'ok') { throw 'Service is not ready. Check the port and .qa/server.stderr.log.' }
    Write-Host "Ready: http://127.0.0.1:$Port"
    if ($ListenAddress -eq '0.0.0.0') {
        $lanAddresses = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
            Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and -not [System.Net.IPAddress]::IsLoopback($_) }
        foreach ($address in $lanAddresses) { Write-Host "LAN: http://${address}:$Port" }
    }
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port" }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally { Pop-Location }
