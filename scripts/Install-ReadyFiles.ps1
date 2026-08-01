[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDirectory,

    [Parameter(Mandatory = $true)]
    [string]$GameDirectory,

    [Parameter(Mandatory = $false)]
    [string]$BackupDirectory
)

$ErrorActionPreference = "Stop"
$source = (Resolve-Path -LiteralPath $SourceDirectory).Path
$game = (Resolve-Path -LiteralPath $GameDirectory).Path
$manifest = Join-Path $PSScriptRoot "..\config\final_files.json"
& (Join-Path $PSScriptRoot "Verify-FinalFiles.ps1") -Directory $source -Manifest $manifest

if (-not $BackupDirectory) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $BackupDirectory = Join-Path (Split-Path -Parent $game) "AC1_X360_Backup_$stamp"
}

if ($PSCmdlet.ShouldProcess($game, "zálohovat původní archivy a nainstalovat českou sadu")) {
    New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
    $items = (Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json).files
    foreach ($item in $items) {
        $destination = Join-Path $game $item.name
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            Copy-Item -LiteralPath $destination -Destination (Join-Path $BackupDirectory $item.name)
        }
        Copy-Item -LiteralPath (Join-Path $source $item.name) -Destination $destination
    }
    Write-Host "Instalace dokončena. Záloha: $BackupDirectory"
}
