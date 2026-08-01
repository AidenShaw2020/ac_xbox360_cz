[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$XboxDirectory,

    [Parameter(Mandatory = $true)]
    [string]$PcDirectory,

    [Parameter(Mandatory = $false)]
    [string]$Ffmpeg = "ffmpeg.exe",

    [Parameter(Mandatory = $false)]
    [string]$Xma2Encode = "xma2encode.exe"
)

$ErrorActionPreference = "Stop"
$checks = @()
$checks += [pscustomobject]@{ Item = "Xbox vstup"; OK = Test-Path -LiteralPath $XboxDirectory -PathType Container }
$checks += [pscustomobject]@{ Item = "PC vstup"; OK = Test-Path -LiteralPath $PcDirectory -PathType Container }
$checks += [pscustomobject]@{ Item = "Python"; OK = [bool](Get-Command python -ErrorAction SilentlyContinue) }
$checks += [pscustomobject]@{ Item = "FFmpeg"; OK = [bool](Get-Command $Ffmpeg -ErrorAction SilentlyContinue) }
$checks += [pscustomobject]@{ Item = "xma2encode"; OK = Test-Path -LiteralPath $Xma2Encode -PathType Leaf }

foreach ($required in @("Data360.forge", "Data360_Present_Room.forge", "Data360_StreamedSoundsEng.forge")) {
    $checks += [pscustomobject]@{
        Item = "Xbox/$required"
        OK = Test-Path -LiteralPath (Join-Path $XboxDirectory $required) -PathType Leaf
    }
}

$checks | Format-Table -AutoSize
if ($checks.OK -contains $false) {
    throw "Některé požadavky chybějí."
}

Write-Host "Základní požadavky jsou dostupné."
