[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^https?://')][string]$CentralUrl,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')][string]$AgentId,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$AgentToken,
    [Parameter(Mandatory = $true)][ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })][string]$RendererPath,
    [string]$PythonExe = 'python.exe',
    [string]$CaBundle = '',
    [string]$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
)

$ErrorActionPreference = 'Stop'
$ServiceName = 'GPU Server Manager Print Agent'
$AccountName = "NT SERVICE\$ServiceName"
$AppDirectory = Join-Path $env:ProgramData 'GPU Server Manager\Print Agent'
$ConfigPath = Join-Path $AppDirectory 'config.json'

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this installer from an elevated PowerShell session.'
}
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    throw "Service already exists: $ServiceName"
}

New-Item -ItemType Directory -Path $AppDirectory -Force | Out-Null
icacls $AppDirectory /inheritance:r | Out-Null
icacls $AppDirectory /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null

& $PythonExe -m pip install (Join-Path $SourceRoot 'services\print-agent')
if ($LASTEXITCODE -ne 0) { throw 'Print Agent package installation failed.' }

$configuration = @{
    central_url = $CentralUrl
    agent_id = $AgentId
    agent_token = $AgentToken
    renderer_path = (Resolve-Path -LiteralPath $RendererPath).Path
    data_directory = $AppDirectory
}
if (-not $CaBundle -and (Test-Path -LiteralPath (Join-Path $SourceRoot 'certificates\gsm-root.crt') -PathType Leaf)) {
    $CaBundle = Join-Path $SourceRoot 'certificates\gsm-root.crt'
}
if ($CaBundle) {
    $installedCa = Join-Path $AppDirectory 'gsm-root.crt'
    Copy-Item -LiteralPath (Resolve-Path -LiteralPath $CaBundle).Path -Destination $installedCa
    $configuration.ca_bundle = $installedCa
    $env:GSM_PRINT_CA_BUNDLE = $installedCa
}
$configuration | ConvertTo-Json -Compress | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
icacls $ConfigPath /inheritance:r /grant:r "SYSTEM:F" "Administrators:F" | Out-Null

# pywin32 installs a real Service Control Manager entry with Automatic startup.
& $PythonExe -m gsm_print_agent.windows_service `
    --startup auto `
    install
if ($LASTEXITCODE -ne 0) { throw 'Print Agent Windows service registration failed.' }
sc.exe config $ServiceName obj= "$AccountName" password= "" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Print Agent virtual service account configuration failed.' }
icacls $AppDirectory /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "${AccountName}:(OI)(CI)M" | Out-Null
icacls $ConfigPath /grant:r "SYSTEM:F" "Administrators:F" "${AccountName}:R" | Out-Null

$env:GSM_PRINT_CENTRAL_URL = $CentralUrl
$env:GSM_PRINT_AGENT_ID = $AgentId
$env:GSM_PRINT_AGENT_TOKEN = $AgentToken
$env:GSM_PRINT_RENDERER_PATH = (Resolve-Path -LiteralPath $RendererPath).Path
$env:GSM_PRINT_AGENT_DATA_DIR = $AppDirectory
$env:GSM_PRINT_AGENT_MODE = 'windows'
& $PythonExe -m gsm_print_agent.main discover | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Print Agent printer discovery failed.' }
& $PythonExe -m gsm_print_agent.main health | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Print Agent Central API health check failed.' }
$smokePdf = Join-Path $AppDirectory 'installer-smoke.pdf'
[IO.File]::WriteAllText($smokePdf, "%PDF-1.4`n% dry-run only`n")
& $PythonExe -m gsm_print_agent.main dry-run $smokePdf --output-directory (Join-Path $AppDirectory 'dry-run') | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Print Agent local dry-run failed.' }
Remove-Item -LiteralPath $smokePdf -Force

Start-Service -Name $ServiceName
$installedService = Get-Service -Name $ServiceName
$installedService.WaitForStatus(
    [ServiceProcess.ServiceControllerStatus]::Running,
    [TimeSpan]::FromSeconds(30)
)
Start-Sleep -Seconds 2
$installedService.Refresh()
if ($installedService.Status -ne [ServiceProcess.ServiceControllerStatus]::Running) {
    throw 'Print Agent Windows service did not remain running.'
}
Write-Host "Installed $ServiceName. Outbound Central API connectivity and dry-run passed."
