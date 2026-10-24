# Statistics of the sensor timestamps in a scan manifest (manifest.json / rig__manifest.json).
#
#   .\manifest_sync_stats.ps1 -Manifest "$env:USERPROFILE\Downloads\rig__manifest.json"
#
# 1. Offset between the two cameras for every photo pair (same level and shot): |ts(cam1) - ts(cam0)| in ms.
#    A value near 0 means the pair was captured in the same sensor frame (rpi.sync).
# 2. Time between two consecutive photos of the same camera inside a ring (seconds): the rotation + settle time
#    of one turntable step as seen by the sensor.
param(
    [Parameter(Mandatory = $true)][string]$Manifest
)

function Show-Stats([string]$Title, [double[]]$Values, [string]$Unit, [string]$Format = "N3") {
    if (-not $Values -or $Values.Count -eq 0) {
        Write-Host "$Title : no data"
        return
    }
    $sorted = $Values | Sort-Object
    $n = $sorted.Count
    $median = if ($n % 2 -eq 1) { $sorted[($n - 1) / 2] } else { ($sorted[$n / 2 - 1] + $sorted[$n / 2]) / 2 }
    $mean = ($Values | Measure-Object -Average).Average
    Write-Host ("{0}" -f $Title)
    Write-Host ("  samples : {0}" -f $n)
    Write-Host ("  mean    : {0} {1}" -f $mean.ToString($Format), $Unit)
    Write-Host ("  median  : {0} {1}" -f $median.ToString($Format), $Unit)
    Write-Host ("  min/max : {0} / {1} {2}" -f $sorted[0].ToString($Format), $sorted[$n - 1].ToString($Format), $Unit)
}

$m = Get-Content -Raw -Path $Manifest | ConvertFrom-Json
$photos = @($m.photos | Where-Object { $null -ne $_.sensor_timestamp })
Write-Host ("Scan {0}: {1} photos, {2} with a sensor timestamp" -f $m.scan_id, @($m.photos).Count, $photos.Count)

# 1. offset between the cameras, per pair
$offsets = @()
foreach ($pair in ($photos | Group-Object { "{0}/{1}" -f $_.level, $_.shot })) {
    $c0 = @($pair.Group | Where-Object { $_.camera -eq 0 })
    $c1 = @($pair.Group | Where-Object { $_.camera -eq 1 })
    if ($c0.Count -eq 1 -and $c1.Count -eq 1) {
        $offsets += [math]::Abs([double]$c1[0].sensor_timestamp - [double]$c0[0].sensor_timestamp) / 1e6
    }
}
Show-Stats "Offset between cam0 and cam1 per pair" $offsets "ms" "N3"
Write-Host ""

# 2. time between consecutive shots of the same camera, inside one ring
# (cam0 only: both cameras are in the same frame, see the offset above). A ring of N shots has N - 1 gaps, and the climb
# between two rings is not counted, so 2 rings of 20 shots give 2 x 19 = 38 samples.
$steps = @()
foreach ($ring in ($photos | Where-Object { $_.camera -eq 0 } | Group-Object level)) {
    $ordered = @($ring.Group | Sort-Object { [int]$_.shot })
    Write-Host ("  ring {0}: {1} shots -> {2} gaps" -f $ring.Name, $ordered.Count, [math]::Max(0, $ordered.Count - 1))
    for ($i = 1; $i -lt $ordered.Count; $i++) {
        $steps += ([double]$ordered[$i].sensor_timestamp - [double]$ordered[$i - 1].sensor_timestamp) / 1e9
    }
}
Show-Stats "Time between consecutive shots of cam0 inside a ring (gaps = shots - 1 per ring)" $steps "s" "N2"
