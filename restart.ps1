param([ValidateRange(1,65535)][int]$Port = 8000, [string]$ListenAddress = '127.0.0.1', [switch]$NoBrowser)
# Let the web restart response complete before stopping the service.
Start-Sleep -Seconds 2
& (Join-Path $PSScriptRoot 'start.ps1') -Port $Port -ListenAddress $ListenAddress -Restart -NoBrowser:$NoBrowser
