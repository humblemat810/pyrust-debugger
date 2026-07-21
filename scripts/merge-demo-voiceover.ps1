[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Video,

    [Parameter(Mandatory = $true)]
    [string]$Cues,

    [Parameter(Mandatory = $true)]
    [string]$Output,

    [string]$Ffmpeg = "ffmpeg",

    [string]$Ffprobe = "ffprobe",

    [ValidateRange(64, 320)]
    [int]$AudioBitrateKbps = 160
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-ExistingFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description was not found: $Path"
    }

    return (Resolve-Path -LiteralPath $Path).Path
}

try {
    $null = Get-Command $Ffmpeg -ErrorAction Stop
    $null = Get-Command $Ffprobe -ErrorAction Stop
}
catch {
    throw "ffmpeg and ffprobe must be installed and available on PATH. $_"
}

$videoPath = Resolve-ExistingFile -Path $Video -Description "Silent screen recording"
$cuesPath = Resolve-ExistingFile -Path $Cues -Description "Cue manifest"
$cueDirectory = Split-Path -Parent $cuesPath
$outputPath = [System.IO.Path]::GetFullPath($Output)
$outputDirectory = Split-Path -Parent $outputPath

if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

$manifest = Get-Content -LiteralPath $cuesPath -Raw | ConvertFrom-Json
$cues = @($manifest.cues | Sort-Object { [double]$_.startSeconds })

if ($cues.Count -eq 0) {
    throw "Cue manifest must contain at least one item in its 'cues' array."
}

$durationText = (& $Ffprobe -v error -show_entries format=duration `
    -of default=nokey=1:noprint_wrappers=1 -- $videoPath | Select-Object -First 1).Trim()
$duration = 0.0

if (-not [double]::TryParse(
    $durationText,
    [Globalization.NumberStyles]::Float,
    [Globalization.CultureInfo]::InvariantCulture,
    [ref]$duration
) -or $duration -le 0) {
    throw "Could not determine a positive duration for: $videoPath"
}

$durationFilter = $duration.ToString("0.###", [Globalization.CultureInfo]::InvariantCulture)
$inputArguments = @("-hide_banner", "-y", "-i", $videoPath)
$filterLines = [System.Collections.Generic.List[string]]::new()
$audioLabels = [System.Collections.Generic.List[string]]::new()

for ($index = 0; $index -lt $cues.Count; $index++) {
    $cue = $cues[$index]

    if ($null -eq $cue.file -or [string]::IsNullOrWhiteSpace([string]$cue.file)) {
        throw "Cue $index has no 'file' value."
    }

    $startSeconds = [double]$cue.startSeconds
    if ($startSeconds -lt 0 -or $startSeconds -ge $duration) {
        throw "Cue $index has startSeconds=$startSeconds, outside the video duration of $durationFilter seconds."
    }

    $clipPath = [string]$cue.file
    if (-not [System.IO.Path]::IsPathRooted($clipPath)) {
        $clipPath = Join-Path $cueDirectory $clipPath
    }
    $clipPath = Resolve-ExistingFile -Path $clipPath -Description "Narration clip for cue $index"

    $delayMilliseconds = [math]::Round($startSeconds * 1000)
    $inputIndex = $index + 1
    $label = "cue$index"
    $inputArguments += @("-i", $clipPath)
    $filterLines.Add(
        "[$inputIndex:a]adelay=${delayMilliseconds}:all=1,apad=whole_dur=$durationFilter,atrim=duration=$durationFilter[$label]"
    )
    $audioLabels.Add("[$label]")
}

$filterLines.Add(
    "$($audioLabels -join '')amix=inputs=$($audioLabels.Count):duration=longest:normalize=0,atrim=duration=$durationFilter[voiceover]"
)

$temporaryFilter = Join-Path ([System.IO.Path]::GetTempPath()) ("pyrust-voiceover-{0}.ffscript" -f [guid]::NewGuid())

try {
    [System.IO.File]::WriteAllLines($temporaryFilter, $filterLines)

    & $Ffmpeg @inputArguments `
        -filter_complex_script $temporaryFilter `
        -map 0:v:0 `
        -map "[voiceover]" `
        -c:v copy `
        -c:a aac `
        -b:a "${AudioBitrateKbps}k" `
        -movflags +faststart `
        -shortest `
        $outputPath

    if ($LASTEXITCODE -ne 0) {
        throw "ffmpeg failed with exit code $LASTEXITCODE."
    }
}
finally {
    Remove-Item -LiteralPath $temporaryFilter -Force -ErrorAction SilentlyContinue
}

Write-Host "Created narrated demo: $outputPath"
