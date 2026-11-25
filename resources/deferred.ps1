param([Parameter(Mandatory=$true)][string]$Receipt)
$ErrorActionPreference = 'Stop'

function Write-JsonAtomic($destination, $json) {
    if (-not $destination) { return }
    $dir = [IO.Path]::GetDirectoryName($destination)
    if ($dir -and -not [IO.Directory]::Exists($dir)) {
        [IO.Directory]::CreateDirectory($dir) | Out-Null
    }
    $temporary = $destination + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($temporary, $json, (New-Object Text.UTF8Encoding($false)))
    if ([IO.File]::Exists($destination)) {
        $previous = $temporary + '.previous'
        [IO.File]::Replace($temporary, $destination, $previous)
        Remove-Item -LiteralPath $previous -ErrorAction SilentlyContinue
    } else {
        [IO.File]::Move($temporary, $destination)
    }
}

function Save-Record($value) {
    $json = $value | ConvertTo-Json -Depth 10
    foreach ($destination in @($value.sidecar, $Receipt)) {
        Write-JsonAtomic $destination $json
    }
}

function Hash-File($path) {
    (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Cleanup-Staged($rec) {
    if ($rec.mode -eq 'source') {
        if ($rec.stageDir -and [IO.Directory]::Exists($rec.stageDir)) {
            Remove-Item -LiteralPath $rec.stageDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    } elseif ($rec.pending -and [IO.File]::Exists($rec.pending)) {
        if ((Hash-File $rec.pending) -eq $rec.replacementSha) {
            Remove-Item -LiteralPath $rec.pending -ErrorAction SilentlyContinue
        }
    }
}

function Mod-Active($configuration) {
    if (-not $configuration.enabled -or -not $configuration.modPath -or -not [IO.File]::Exists($configuration.modPath)) {
        return $false
    }
    return $true
}

function Show-Popup($message, $title, $icon = 'Information') {
    try {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show($message, $title, 'OK', $icon) | Out-Null
    } catch {
    }
}

function Show-ReadyNotice($rec) {
    if ($rec.notify -and -not $rec.skipReadyNotice) {
        Show-Popup 'Traffic and AutoDrive fixes are staged and ready. Close BeamNG.Earth completely, wait a few seconds, then launch BeamNG.Earth normally.' 'Traffic and AutoDrive fixes - restart required'
    }
}

try {
    $record = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
    $target = [IO.Path]::GetFullPath($record.target)
    if ($record.mode -ne 'source') {
        $pending = [IO.Path]::GetFullPath($record.pending)
        if ([IO.Path]::GetDirectoryName($target) -ne [IO.Path]::GetDirectoryName($pending) -or
            -not [IO.Path]::GetFileName($pending).StartsWith([IO.Path]::GetFileName($target) + '.earthAutodriveFixes.') -or
            -not $pending.EndsWith('.pending')) {
            throw 'Invalid staged-file path.'
        }
    }

    if ($record.companionPid) {
        $companionEnd = (Get-Date).AddSeconds(30)
        while ($null -ne (Get-Process -Id $record.companionPid -ErrorAction SilentlyContinue)) {
            if ((Get-Date) -gt $companionEnd) {
                throw 'Setup did not finish closing. Please try again.'
            }
            Start-Sleep -Milliseconds 200
        }
    }

    Show-ReadyNotice $record

    $deadline = (Get-Date).AddHours(12)
    $ready = $false
    do {
        $latest = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
        $configuration = Get-Content -LiteralPath $record.config -Raw | ConvertFrom-Json
        if ($latest.token -ne $record.token -or $configuration.requestToken -ne $record.token) {
            Cleanup-Staged $record
            return
        }
        if ($record.mode -ne 'source' -and $target.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) {
            $busy = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'BeamNG.Earth%.exe'" |
                Where-Object { $_.ExecutablePath -and [IO.Path]::GetFullPath($_.ExecutablePath) -eq $target }).Count -gt 0
        } else {
            $busy = $record.serverPid -gt 0 -and $null -ne (Get-Process -Id $record.serverPid -ErrorAction SilentlyContinue)
        }
        if (-not $busy) { $ready = $true; break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    if (-not $ready) {
        throw 'Timed out waiting for BeamNG.Earth to close. Please try again.'
    }

    if ($record.operation -eq 'install' -and -not $record.standalone -and -not (Mod-Active $configuration)) {
        Cleanup-Staged $record
        $record.state = 'cancelled'
        $record.message = 'The add-on was disabled or removed; patch cancelled.'
        Save-Record $record
        return
    }

    if ($record.mode -eq 'source') {
        foreach ($item in $record.items) {
            $dest = Join-Path $target ($item.rel -replace '/', '\')
            if ($item.expectedSha) {
                if (-not [IO.File]::Exists($dest) -or (Hash-File $dest) -ne $item.expectedSha) {
                    throw "File changed before apply: $($item.rel)"
                }
            } elseif ([IO.File]::Exists($dest)) {
                throw "File unexpectedly appeared before apply: $($item.rel)"
            }
        }
        foreach ($item in $record.items) {
            $dest = Join-Path $target ($item.rel -replace '/', '\')
            if ($item.action -eq 'delete') {
                if ([IO.File]::Exists($dest)) { Remove-Item -LiteralPath $dest -Force }
            } else {
                if ((Hash-File $item.staged) -ne $item.replacementSha) {
                    throw "Staged file checksum mismatch: $($item.rel)"
                }
                $destDir = [IO.Path]::GetDirectoryName($dest)
                if ($destDir -and -not [IO.Directory]::Exists($destDir)) {
                    [IO.Directory]::CreateDirectory($destDir) | Out-Null
                }
                Copy-Item -LiteralPath $item.staged -Destination $dest -Force
                if ((Hash-File $dest) -ne $item.replacementSha) {
                    throw "Post-copy checksum mismatch: $($item.rel)"
                }
            }
        }
        Cleanup-Staged $record
    } else {
        if ((Hash-File $target) -ne $record.expectedSha) { throw 'App changed after preparation; an update was preserved.' }
        if ((Hash-File $pending) -ne $record.replacementSha) { throw 'Staged file changed; original app was preserved.' }
        $replaced = $false
        for ($attempt = 0; $attempt -lt 15; $attempt++) {
            $latest = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
            $configuration = Get-Content -LiteralPath $record.config -Raw | ConvertFrom-Json
            if ($latest.token -ne $record.token -or $configuration.requestToken -ne $record.token) {
                Cleanup-Staged $record
                return
            }
            if ((Hash-File $target) -ne $record.expectedSha) {
                throw 'App changed before replacement; original file preserved.'
            }
            try {
                $swapBackup = $target + '.earthAutodriveFixes.swap.' + [guid]::NewGuid().ToString('N')
                [IO.File]::Replace($pending, $target, $swapBackup)
                $replaced = $true
                break
            } catch [IO.IOException] {
                Start-Sleep -Seconds 2
            }
        }
        if (-not $replaced) { throw 'Windows kept the executable locked. Close BeamNG.Earth and try again.' }
        if ((Hash-File $target) -ne $record.replacementSha) { throw 'Replacement verification failed; original backup available.' }
        Remove-Item -LiteralPath $swapBackup -ErrorAction SilentlyContinue
    }

    if ($record.operation -eq 'restore') {
        $record.state = 'restored'
        $record.message = 'Original BeamNG.Earth restored. Start BeamNG.Earth normally.'
    } else {
        $record.state = 'installed'
        $record.message = 'Traffic and AutoDrive fixes installed. Start BeamNG.Earth normally.'
    }
    Save-Record $record

    if ($record.operation -eq 'restore' -and $record.cleanupOnRestore) {
        if ($record.backup -and [IO.File]::Exists($record.backup)) {
            Remove-Item -LiteralPath $record.backup -Force -ErrorAction SilentlyContinue
        }
        if ($record.sidecar -and [IO.File]::Exists($record.sidecar)) {
            Remove-Item -LiteralPath $record.sidecar -Force -ErrorAction SilentlyContinue
        }
        if ($record.installReceipt -and [IO.File]::Exists($record.installReceipt)) {
            Remove-Item -LiteralPath $record.installReceipt -Force -ErrorAction SilentlyContinue
        }
        if ($record.lastTargetFile -and [IO.File]::Exists($record.lastTargetFile)) {
            Remove-Item -LiteralPath $record.lastTargetFile -Force -ErrorAction SilentlyContinue
        }
    }

    if ($record.relaunchAfterSwap -and $target.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase) -and [IO.File]::Exists($target)) {
        Start-Process -FilePath $target -WorkingDirectory ([IO.Path]::GetDirectoryName($target)) | Out-Null
    }

    if ($record.completionNotice) {
        Show-Popup $record.completionNotice 'Traffic and AutoDrive fixes'
    }
} catch {
    if ($record) {
        $record.state = 'error'
        $record.message = $_.Exception.Message
        Save-Record $record
        if ($record.notify -and $record.skipReadyNotice) {
            Show-Popup $_.Exception.Message 'Traffic and AutoDrive fixes - setup error' 'Error'
        }
    }
}
