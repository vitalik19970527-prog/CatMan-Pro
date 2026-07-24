param(
    [switch]$NoYesterdaySlot
)

$ErrorActionPreference = "Stop"

$root = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { "D:\CM_WorkSpace" }
Set-Location -LiteralPath $root

if (-not (Test-Path "backups")) {
    New-Item -ItemType Directory -Path "backups" | Out-Null
}

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$name = "CM_WorkSpace_backup_{0}.zip" -f $ts
$finalDest = Join-Path (Get-Location) ("backups\$name")
$tempDest = Join-Path $env:TEMP $name

if (Test-Path $tempDest) { Remove-Item -Force $tempDest }
if (Test-Path $finalDest) { Remove-Item -Force $finalDest }

# All project files except EDB PostgreSQL install folder; exclude backups/ to avoid nesting old zips.
$tarArgs = @(
    "-a", "-c", "-f", $tempDest,
    "--exclude=EDB PostgreSQL",
    "--exclude=backups",
    "."
)
& tar @tarArgs

Move-Item -Force $tempDest $finalDest

if (-not $NoYesterdaySlot) {
    $yesterday = (Get-Date).AddDays(-1).ToString("yyyyMMdd")
    $yesterdayDest = Join-Path (Get-Location) "backups\CM_WorkSpace_backup_$yesterday.zip"
    Copy-Item -LiteralPath $finalDest -Destination $yesterdayDest -Force
    Write-Output "ARCHIVE_STAMPED=$finalDest"
    Write-Output "ARCHIVE_YESTERDAY_SLOT=$yesterdayDest"
} else {
    Write-Output "ARCHIVE_STAMPED=$finalDest"
}
