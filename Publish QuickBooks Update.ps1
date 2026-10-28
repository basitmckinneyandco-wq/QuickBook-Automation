param(
    [string]$ReleaseDirectory,
    [string]$PackageUrl = $env:QB_AUTOMATION_PACKAGE_URL
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ReleaseDirectory) {
    $oneDriveRoot = if ($env:OneDriveCommercial) { $env:OneDriveCommercial } else { $env:OneDrive }
    if (-not $oneDriveRoot) { throw 'OneDrive is not signed in on this computer.' }
    # Match the exact OneDrive folder name that is shared for updates.
    $ReleaseDirectory = Join-Path $oneDriveRoot 'Quickbook App Updates'
}
if (-not (Test-Path -LiteralPath $ReleaseDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $ReleaseDirectory -Force | Out-Null
    Write-Host "Created the local OneDrive sync folder: $ReleaseDirectory"
}
if (-not $PackageUrl) {
    Write-Warning 'QB_AUTOMATION_PACKAGE_URL is not set yet. The first package can still be uploaded, but the manifest will not be usable until you add the stable latest.zip link and publish again.'
}
$branch = git -C $root branch --show-current
if ($branch -ne 'main') { throw "Publish from main, not '$branch'." }
if (git -C $root status --porcelain) { throw 'Commit project changes before publishing.' }
git -C $root fetch origin main
if ($LASTEXITCODE -ne 0) { throw 'Could not fetch the latest shared project from GitHub.' }
git -C $root merge --ff-only origin/main
if ($LASTEXITCODE -ne 0) { throw 'Local main cannot fast-forward. Resolve the Git changes before publishing.' }
$revision = (git -C $root rev-parse --short=12 HEAD).Trim()
$zipName = 'latest.zip'
$zipPath = Join-Path $ReleaseDirectory $zipName
$tempZip = Join-Path $ReleaseDirectory "$zipName.tmp"
git -C $root archive --format=zip --output=$tempZip HEAD
if ($LASTEXITCODE -ne 0) { throw 'Could not export the committed project files.' }
$hash = (Get-FileHash -LiteralPath $tempZip -Algorithm SHA256).Hash.ToUpperInvariant()
Move-Item -LiteralPath $tempZip -Destination $zipPath -Force
$manifest = @{ version = $revision; file = $zipName; package_url = $PackageUrl; sha256 = $hash } | ConvertTo-Json
$tempManifest = Join-Path $ReleaseDirectory 'release.json.tmp'
[System.IO.File]::WriteAllText($tempManifest, $manifest, [System.Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $tempManifest -Destination (Join-Path $ReleaseDirectory 'release.json') -Force
Write-Host "Published revision $revision to $ReleaseDirectory"
