param(
    [string]$InstallDirectory = (Split-Path -Parent $MyInvocation.MyCommand.Path)
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$manifestUrl = $env:QB_AUTOMATION_UPDATE_MANIFEST
$configPath = Join-Path $InstallDirectory 'update_config.json'

if (-not $manifestUrl -and (Test-Path -LiteralPath $configPath)) {
    $settings = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $manifestUrl = [string]$settings.manifest_url
}

if (-not $manifestUrl -or $manifestUrl -match '^REPLACE_WITH_') {
    Write-Host 'Automatic updates are not configured yet; starting the installed version.'
    exit 0
}

$statePath = Join-Path $InstallDirectory '.update-state.json'
$manifestTemp = Join-Path $env:TEMP ('qb-update-' + [guid]::NewGuid().ToString('N') + '.json')
$zipTemp = Join-Path $env:TEMP ('qb-update-' + [guid]::NewGuid().ToString('N') + '.zip')
$stage = Join-Path $env:TEMP ('qb-update-' + [guid]::NewGuid().ToString('N'))

function Get-DownloadUri([string]$Url) {
    $builder = [System.UriBuilder]::new($Url)
    $query = $builder.Query.TrimStart('?')
    if ($query -notmatch '(^|&)download=') {
        $builder.Query = if ($query) { "$query&download=1" } else { 'download=1' }
    }
    return $builder.Uri.AbsoluteUri
}

try {
    Invoke-WebRequest -Uri (Get-DownloadUri $manifestUrl) -OutFile $manifestTemp -UseBasicParsing
    $manifest = Get-Content -LiteralPath $manifestTemp -Raw | ConvertFrom-Json
    if (-not $manifest.version -or -not $manifest.file -or $manifest.file -ne 'latest.zip' -or -not $manifest.package_url) {
        throw 'The update manifest is missing a valid version or package name.'
    }
    if ([string]$manifest.sha256 -notmatch '^[A-Fa-f0-9]{64}$') {
        throw 'The update manifest has an invalid SHA-256 value.'
    }

    $state = if (Test-Path -LiteralPath $statePath) {
        Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    } else { $null }
    if ($state -and $state.version -eq $manifest.version) { exit 0 }

    Invoke-WebRequest -Uri (Get-DownloadUri ([string]$manifest.package_url)) -OutFile $zipTemp -UseBasicParsing
    $actualHash = (Get-FileHash -LiteralPath $zipTemp -Algorithm SHA256).Hash
    if ($actualHash -ine [string]$manifest.sha256) { throw 'The downloaded update failed its SHA-256 check.' }

    Expand-Archive -LiteralPath $zipTemp -DestinationPath $stage -Force
    $source = Join-Path $stage 'qb_agent'
    $web = Join-Path $stage 'web'
    if (-not (Test-Path -LiteralPath $source -PathType Container) -or
        -not (Test-Path -LiteralPath $web -PathType Container)) {
        throw 'The update package does not have the expected app folders.'
    }

    # Copy only application code. Keep company config, credentials, and SQLite history local.
    foreach ($folder in @('qb_agent', 'web')) {
        $destination = Join-Path $InstallDirectory $folder
        if (-not (Test-Path -LiteralPath $destination)) { New-Item -ItemType Directory -Path $destination | Out-Null }
        Copy-Item -Path (Join-Path (Join-Path $stage $folder) '*') -Destination $destination -Recurse -Force
    }
    foreach ($file in @('run_dashboard.py', 'requirements.txt', 'Start Dashboard.cmd', 'Update Dashboard.ps1')) {
        $sourceFile = Join-Path $stage $file
        if (Test-Path -LiteralPath $sourceFile -PathType Leaf) {
            Copy-Item -LiteralPath $sourceFile -Destination (Join-Path $InstallDirectory $file) -Force
        }
    }

    @{ version = [string]$manifest.version } | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    Write-Host "Updated to version $($manifest.version)."
}
catch {
    Write-Warning "Update check failed: $($_.Exception.Message)"
    Write-Warning 'The currently installed version will start.'
}
finally {
    Remove-Item -LiteralPath $manifestTemp, $zipTemp -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}
