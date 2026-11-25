param([Parameter(Mandatory=$true)][string]$Receipt)
$ErrorActionPreference = 'Stop'
$Receipt = [IO.Path]::GetFullPath($Receipt)
try { Set-Location -LiteralPath ([IO.Path]::GetTempPath()) } catch {}
$staleVars = @(Get-ChildItem Env: | Where-Object {
    $_.Name -like '_PYI_*' -or $_.Name -like '_MEIPASS*' -or
    $_.Name -in @('PYTHONHOME', 'PYTHONPATH', 'TCL_LIBRARY', 'TK_LIBRARY', 'BNGEARTH_PARENT')
})
foreach ($sv in $staleVars) {
    Remove-Item -LiteralPath "Env:\$($sv.Name)" -ErrorAction SilentlyContinue
}

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
    if ($value.sidecar) {
        Write-JsonAtomic $value.sidecar $json
    }
    if ($Receipt) {
        $rDir = [IO.Path]::GetDirectoryName($Receipt)
        if (-not $rDir -or [IO.Directory]::Exists($rDir)) {
            Write-JsonAtomic $Receipt $json
        }
    }
}

function Hash-File($path) {
    (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-TargetHash12($fullPath) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($fullPath)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hex = ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join ''
        return $hex.Substring(0, 12)
    } finally {
        $sha.Dispose()
    }
}

function Acquire-SwapLock($lockPath, $timeoutSec = 90) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $fs = [IO.File]::Open($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try {
                $epoch = [double]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) / 1000.0
                $payload = (@{ pid = $PID; ts = $epoch } | ConvertTo-Json -Compress)
                $buf = [Text.Encoding]::UTF8.GetBytes($payload)
                $fs.Write($buf, 0, $buf.Length)
            } finally {
                $fs.Dispose()
            }
            return $true
        } catch [IO.IOException] {
            try {
                if ([IO.File]::Exists($lockPath)) {
                    $raw = [IO.File]::ReadAllText($lockPath)
                    $doc = $raw | ConvertFrom-Json
                    $nowEpoch = [double]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) / 1000.0
                    $staleAge = ($doc.ts -and (($nowEpoch - [double]$doc.ts) -gt 120.0))
                    $deadOwner = ($doc.pid -and ($null -eq (Get-Process -Id ([int]$doc.pid) -ErrorAction SilentlyContinue)))
                    if ($staleAge -or $deadOwner) {
                        Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
                        continue
                    }
                }
            } catch {
            }
            Start-Sleep -Milliseconds 150
        }
    }
    throw 'Timed out waiting for sibling add-on swap lock.'
}

function Release-SwapLock($lockPath) {
    if ($lockPath -and [IO.File]::Exists($lockPath)) {
        Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-RestageOnCurrent($targetPath, $receiptPath) {
    $restagePy = Join-Path $PSScriptRoot 'restage_on_current.py'
    if (-not [IO.File]::Exists($restagePy)) {
        return $false
    }
    $prevHold = $env:BNE_COMPANION_NO_HOLD
    $prevDontWrite = $env:PYTHONDONTWRITEBYTECODE
    $prevParent = $env:BNGEARTH_PARENT
    try {
        $env:BNE_COMPANION_NO_HOLD = '1'
        $env:PYTHONDONTWRITEBYTECODE = '1'
        $env:BNGEARTH_PARENT = '0'
        if ($env:BNE_RESTAGE_PYTHON -and [IO.File]::Exists($env:BNE_RESTAGE_PYTHON)) {
            $p = Start-Process -FilePath $env:BNE_RESTAGE_PYTHON -ArgumentList @('-B', $restagePy, '--receipt', $receiptPath) -WindowStyle Hidden -Wait -PassThru
            if ($p.ExitCode -eq 0) { return $true }
        }
        if ($targetPath.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase) -and [IO.File]::Exists($targetPath)) {
            for ($tryIdx = 0; $tryIdx -lt 15; $tryIdx++) {
                try {
                    $p = Start-Process -FilePath $targetPath -ArgumentList @('--run-addon', $restagePy, '--receipt', $receiptPath) -WindowStyle Hidden -Wait -PassThru
                    if ($p.ExitCode -eq 0) { return $true }
                } catch {
                }
                Start-Sleep -Seconds 1
            }
        }
        $pyCmd = Get-Command py -ErrorAction SilentlyContinue
        if ($pyCmd) {
            foreach ($verArg in @('-3.12', '-3')) {
                try {
                    $p = Start-Process -FilePath $pyCmd.Source -ArgumentList @($verArg, '-B', $restagePy, '--receipt', $receiptPath) -WindowStyle Hidden -Wait -PassThru
                    if ($p.ExitCode -eq 0) { return $true }
                } catch {
                }
            }
        }
        foreach ($pyCand in @('python', 'python3')) {
            $cmd = Get-Command $pyCand -ErrorAction SilentlyContinue
            if ($cmd) {
                $p = Start-Process -FilePath $cmd.Source -ArgumentList @('-B', $restagePy, '--receipt', $receiptPath) -WindowStyle Hidden -Wait -PassThru
                if ($p.ExitCode -eq 0) { return $true }
            }
        }
    } finally {
        if ($null -eq $prevHold) { Remove-Item Env:\BNE_COMPANION_NO_HOLD -ErrorAction SilentlyContinue } else { $env:BNE_COMPANION_NO_HOLD = $prevHold }
        if ($null -eq $prevDontWrite) { Remove-Item Env:\PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue } else { $env:PYTHONDONTWRITEBYTECODE = $prevDontWrite }
        if ($null -eq $prevParent) { Remove-Item Env:\BNGEARTH_PARENT -ErrorAction SilentlyContinue } else { $env:BNGEARTH_PARENT = $prevParent }
    }
    return $false
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

function Assert-ValidPendingPath($targetPath, $pendingPath) {
    if ([IO.Path]::GetDirectoryName($targetPath) -ne [IO.Path]::GetDirectoryName($pendingPath) -or
        -not [IO.Path]::GetFileName($pendingPath).StartsWith([IO.Path]::GetFileName($targetPath) + '.earthAutodriveFixes.') -or
        -not $pendingPath.EndsWith('.pending')) {
        throw 'Invalid staged-file path.'
    }
}

try {
    $record = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
    $target = [IO.Path]::GetFullPath($record.target)
    $pending = $null
    if ($record.mode -ne 'source') {
        $pending = [IO.Path]::GetFullPath($record.pending)
        Assert-ValidPendingPath $target $pending
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
        if (-not [IO.File]::Exists($Receipt) -or -not [IO.File]::Exists($record.config)) {
            Cleanup-Staged $record
            return
        }
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

    $targetHash = Get-TargetHash12 $target
    $swapLock = [IO.Path]::Combine([IO.Path]::GetTempPath(), "bne_companion_swap_$targetHash.lock")
    Acquire-SwapLock $swapLock
    try {
        if (-not [IO.File]::Exists($Receipt) -or -not [IO.File]::Exists($record.config)) {
            Cleanup-Staged $record
            return
        }
        $latest = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
        $configuration = Get-Content -LiteralPath $record.config -Raw | ConvertFrom-Json
        if ($latest.token -ne $record.token -or $configuration.requestToken -ne $record.token) {
            Cleanup-Staged $record
            return
        }

        if ($record.operation -eq 'install' -and -not $record.standalone -and -not (Mod-Active $configuration)) {
            Cleanup-Staged $record
            $record.state = 'cancelled'
            $record.message = 'The add-on was disabled or removed; patch cancelled.'
            Save-Record $record
            return
        }

        if ($record.mode -eq 'source') {
            $sourceDrifted = $false
            foreach ($item in $record.items) {
                $dest = Join-Path $target ($item.rel -replace '/', '\')
                if ($item.expectedSha) {
                    if (-not [IO.File]::Exists($dest) -or (Hash-File $dest) -ne $item.expectedSha) {
                        $sourceDrifted = $true
                        break
                    }
                } elseif ([IO.File]::Exists($dest)) {
                    $sourceDrifted = $true
                    break
                }
            }
            if ($sourceDrifted) {
                Cleanup-Staged $record
                if (-not (Invoke-RestageOnCurrent $target $Receipt)) {
                    throw 'Source files changed before apply and could not be restaged.'
                }
                $record = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
                if ($record.state -eq 'installed' -or $record.state -eq 'restored') {
                    Save-Record $record
                    return
                }
            }
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
            if ((Hash-File $target) -ne $record.expectedSha) {
                Cleanup-Staged $record
                if (-not (Invoke-RestageOnCurrent $target $Receipt)) {
                    throw 'App changed after preparation; an update was preserved.'
                }
                $record = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
                if ($record.state -eq 'installed' -or $record.state -eq 'restored') {
                    Save-Record $record
                    return
                }
                $pending = [IO.Path]::GetFullPath($record.pending)
                Assert-ValidPendingPath $target $pending
            }
            if ((Hash-File $target) -ne $record.expectedSha) { throw 'App changed after preparation; an update was preserved.' }
            if ((Hash-File $pending) -ne $record.replacementSha) { throw 'Staged file changed; original app was preserved.' }
            $replaced = $false
            for ($attempt = 0; $attempt -lt 15; $attempt++) {
                if (-not [IO.File]::Exists($Receipt) -or -not [IO.File]::Exists($record.config)) {
                    Cleanup-Staged $record
                    return
                }
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
    } finally {
        Release-SwapLock $swapLock
    }

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
