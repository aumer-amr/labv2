<#
.SYNOPSIS
Streams a Voicemeeter output bus to Snapserver.

.EXAMPLE
./scripts/start-snapcast-source.ps1 -Action InstallFfmpeg

.EXAMPLE
./scripts/start-snapcast-source.ps1 -Action ListDevices

.EXAMPLE
./scripts/start-snapcast-source.ps1 -InputDevice "VoiceMeeter VAIO3 Output (VB-Audio VoiceMeeter VAIO3)"
#>

[CmdletBinding()]
param(
    [ValidateSet("InstallFfmpeg", "ListDevices", "Stream")]
    [string]$Action = "Stream",

    [string]$InputDevice = "VoiceMeeter VAIO3 Output (VB-Audio VoiceMeeter VAIO3)",

    [string]$Snapserver = "10.10.1.114",

    [ValidateRange(1, 65535)]
    [int]$Port = 4953,

    [ValidateRange(10, 1000)]
    [int]$CaptureBufferMilliseconds = 100
)

$ErrorActionPreference = "Stop"

if ($Action -eq "InstallFfmpeg") {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "winget.exe is required to install FFmpeg."
    }

    & winget.exe install --exact --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed with exit code $LASTEXITCODE."
    }

    Write-Host "FFmpeg installed. Open a new terminal before running this script again."
    exit 0
}

$ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    throw "ffmpeg.exe not found. Run this script with -Action InstallFfmpeg first."
}

if ($Action -eq "ListDevices") {
    & $ffmpeg.Source -hide_banner -list_devices true -f dshow -i dummy
    exit 0
}

$target = "tcp://${Snapserver}:$Port"
Write-Host "Streaming '$InputDevice' to $target. Press Ctrl+C to stop."

& $ffmpeg.Source `
    -hide_banner `
    -loglevel warning `
    -f dshow `
    -audio_buffer_size $CaptureBufferMilliseconds `
    -i "audio=$InputDevice" `
    -vn `
    -ac 2 `
    -ar 48000 `
    -c:a pcm_s16le `
    -f s16le `
    $target

if ($LASTEXITCODE -ne 0) {
    throw "FFmpeg stopped with exit code $LASTEXITCODE."
}
