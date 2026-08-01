[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Directory = ".",

    [Parameter(Mandatory = $false)]
    [string]$Manifest
)

$ErrorActionPreference = "Stop"
if (-not $Manifest) {
    $Manifest = Join-Path $PSScriptRoot "..\config\final_files.json"
}
$root = (Resolve-Path -LiteralPath $Directory).Path
$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$expected = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$results = @()

foreach ($item in $expected.files) {
    $path = Join-Path $root $item.name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        $results += [pscustomobject]@{
            File = $item.name; Size = "missing"; SHA256 = "missing"; Status = "CHYBÍ"
        }
        continue
    }

    $file = Get-Item -LiteralPath $path
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    $sizeOk = [int64]$file.Length -eq [int64]$item.size
    $hashOk = $hash -eq [string]$item.sha256
    $results += [pscustomobject]@{
        File = $item.name
        Size = $file.Length
        SHA256 = $hash
        Status = if ($sizeOk -and $hashOk) { "OK" } else { "NESOUHLASÍ" }
    }
}

$results | Format-Table -AutoSize
if ($results.Status -contains "CHYBÍ" -or $results.Status -contains "NESOUHLASÍ") {
    throw "Finální sada neodpovídá manifestu."
}

Write-Host "Ověřeno: $($results.Count) souborů odpovídá finální testované sadě."
