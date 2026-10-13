[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][switch]$ConfirmRemoval
)

$ErrorActionPreference = 'Stop'
$ServiceName = 'GPU Server Manager Print Agent'
$AppDirectory = Join-Path $env:ProgramData 'GPU Server Manager\Print Agent'

if (-not $ConfirmRemoval) {
    throw 'Pass -ConfirmRemoval to remove the Print Agent service and local data.'
}

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    if ($service.Status -ne 'Stopped') {
        Stop-Service -Name $ServiceName -Force
    }
    sc.exe delete $ServiceName | Out-Null
}
if (Test-Path -LiteralPath $AppDirectory) {
    Remove-Item -LiteralPath $AppDirectory -Recurse -Force
}
Write-Host "Removed $ServiceName."
