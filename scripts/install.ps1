#Requires -Version 5.1
<#
.SYNOPSIS
    Installs or updates Free Claude Code (PCC) on Windows.
.DESCRIPTION
    Ensures Claude Code and uv are installed, then installs the free-claude-code
    package as a uv tool. Verifies pcc-server and pcc-claude are on PATH.
    Creates desktop and Start Menu shortcuts.
.PARAMETER DryRun
    Print commands without running them.
.PARAMETER VoiceNim
    Install NVIDIA NIM voice transcription support.
.PARAMETER VoiceLocal
    Install local Whisper voice transcription support.
.PARAMETER VoiceAll
    Install all voice transcription backends.
.PARAMETER TorchBackend
    Use a uv PyTorch backend, such as cu130. Requires local voice.
#>
param(
    [switch] $DryRun,
    [switch] $Help,
    [switch] $VoiceNim,
    [switch] $VoiceLocal,
    [switch] $VoiceAll,
    [string] $TorchBackend = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PackageName = "free-claude-code"
$MinUvVersion = "0.11.16"
$PythonVersion = "3.14.0"
$RepoArchiveUrl = "https://github.com/King-Jboy/kingjboy-claude-code/archive/refs/heads/main.zip"
$ClaudeInstallUrl = "https://claude.ai/install.ps1"
$UvInstallUrl = "https://astral.sh/uv/install.ps1"
$FccCommands = @("pcc-server", "pcc-claude", "free-claude-code")
$ShortcutName = "Free Claude Code"
$OwnerMarker = ".free-claude-code-owner"
$OwnerValue = "io.github.king-jboy.kingjboy-claude-code"

function Show-Usage {
    @"
Usage: install.ps1 [options]

Installs or updates Free Claude Code on Windows.

Options:
  -DryRun          Print commands without running them.
  -VoiceNim        Install NVIDIA NIM voice transcription support.
  -VoiceLocal      Install local Whisper voice transcription support.
  -VoiceAll        Install all voice transcription backends.
  -TorchBackend V  Use a uv PyTorch backend, such as cu130.
  -Help            Show this help text.
"@
}

function Write-Step {
    param([string] $Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Get-ApplicationCommand {
    param([string] $Name)
    $commands = @(Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue)
    if ($commands.Count -eq 0) { return $null }
    return $commands[0]
}

function Get-PowerShellExecutable {
    param([string] $PowerShellHome = "")
    if ($PowerShellHome -and (Test-Path (Join-Path $PowerShellHome "pwsh.exe"))) {
        return (Join-Path $PowerShellHome "pwsh.exe")
    }
    $cmd = Get-ApplicationCommand "pwsh"
    if ($cmd) { return $cmd.Source }
    $cmd = Get-ApplicationCommand "powershell"
    if ($cmd) { return $cmd.Source }
    $systemRoot = $env:SystemRoot
    if ($systemRoot) {
        $candidate = Join-Path $systemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        if (Test-Path $candidate) { return $candidate }
    }
    return "powershell.exe"
}

function Test-UvVersionSupported {
    param([string] $Current, [string] $Minimum)
    # Strip prerelease/build metadata for comparison
    $cleanCurrent = $Current -replace '[-+].*$', ''
    $cleanMin = $Minimum -replace '[-+].*$', ''
    # If the current version had prerelease/build metadata, it's not a stable release
    if ($Current -ne $cleanCurrent) { return $false }
    try {
        $c = [version]$cleanCurrent
        $m = [version]$cleanMin
        return $c -ge $m
    } catch { return $false }
}

function Invoke-DownloadAndRun {
    param([string] $Url, [string] $Description)
    $tempFile = Join-Path $env:TEMP "fcc-install-$([guid]::NewGuid().ToString('N').Substring(0,6)).ps1"
    if ($DryRun) {
        Write-Host "+ Invoke-WebRequest $Url -OutFile <temp>"
        Write-Host "+ powershell -File <temp>"
        return
    }
    Invoke-WebRequest -Uri $Url -OutFile $tempFile
    & (Get-PowerShellExecutable) -NoProfile -ExecutionPolicy Bypass -File $tempFile
    Remove-Item $tempFile -Force -ErrorAction SilentlyContinue
}

function Assert-NoFccProcessesRunning {
    foreach ($name in $FccCommands) {
        $procs = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
        if ($procs.Count -gt 0) {
            foreach ($p in $procs) {
                Write-Error "Free Claude Code process '$name' (PID $($p.Id)) is currently running. Stop it before installing."
            }
            exit 1
        }
    }
}

function Ensure-Claude {
    $cmd = Get-ApplicationCommand "claude"
    if ($cmd) {
        Write-Host "Claude Code already found on PATH; verifying it."
        if ($DryRun) {
            Write-Host "+ claude --version"
            return
        }
        $result = & claude --version 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Claude Code verification failed."
            exit 1
        }
        return
    }
    Write-Host "Claude Code not found; installing it."
    Invoke-DownloadAndRun -Url $ClaudeInstallUrl -Description "Claude Code"
    if ($DryRun) {
        Write-Host "+ claude --version"
        return
    }
    # Verify
    $cmd = Get-ApplicationCommand "claude"
    if (-not $cmd) {
        Write-Error "Claude Code installation failed: 'claude' not found on PATH."
        exit 1
    }
    $null = & claude --version 2>&1
    $null = & claude --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Claude Code verification failed after install."
        exit 1
    }
}

function Ensure-Uv {
    $cmd = Get-ApplicationCommand "uv"
    if ($cmd) {
        if ($DryRun) {
            Write-Host "+ uv --version"
            return
        }
        $versionOutput = & uv --version 2>&1
        $version = ($versionOutput -replace 'uv ', '').Trim()
        if (Test-UvVersionSupported $version $MinUvVersion) {
            Write-Host "uv $version already satisfies >=$MinUvVersion; leaving it unchanged."
            return
        }
        Write-Host "uv $version does not satisfy stable >=$MinUvVersion; installing the current standalone uv."
    } else {
        Write-Host "uv is not installed; installing the current standalone uv."
    }
    Invoke-DownloadAndRun -Url $UvInstallUrl -Description "uv"
    if ($DryRun) {
        Write-Host "+ uv --version"
        return
    }
    # Verify
    $cmd = Get-ApplicationCommand "uv"
    if (-not $cmd) {
        Write-Error "uv installation failed: 'uv' not found on PATH."
        exit 1
    }
    $versionOutput = & uv --version 2>&1
    $version = ($versionOutput -replace 'uv ', '').Trim()
    if (-not (Test-UvVersionSupported $version $MinUvVersion)) {
        Write-Error "uv $version still does not satisfy >=$MinUvVersion after install."
        exit 1
    }
    Write-Host "Verified uv $version."
}

function Get-PackageSpec {
    $includeNim = $VoiceNim -or $VoiceAll
    $includeLocal = $VoiceLocal -or $VoiceAll
    if ($includeNim -and $includeLocal) {
        $spec = "free-claude-code[voice,voice_local] @ $RepoArchiveUrl"
    } elseif ($includeNim) {
        $spec = "free-claude-code[voice] @ $RepoArchiveUrl"
    } elseif ($includeLocal) {
        $spec = "free-claude-code[voice_local] @ $RepoArchiveUrl"
    } else {
        $spec = "free-claude-code @ $RepoArchiveUrl"
    }
    return $spec
}

function Install-FreeClaudeCode {
    Write-Step "Installing or updating Free Claude Code"
    $spec = Get-PackageSpec
    $uvArgs = @("tool", "install", "--force", "--refresh-package", $PackageName, "--python", $PythonVersion)
    if ($TorchBackend) {
        $uvArgs += @("--torch-backend", $TorchBackend)
    }
    $uvArgs += $spec
    if ($DryRun) {
        Write-Host "+ uv $($uvArgs -join ' ')"
        return
    }
    & uv @uvArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "uv tool install failed."
        exit 1
    }
}

function Configure-AndVerify {
    Write-Step "Configuring PATH and verifying Free Claude Code"
    if ($DryRun) {
        Write-Host "+ uv tool update-shell"
        Write-Host "+ uv tool dir --bin"
        Write-Host "+ pcc-server --version"
        return
    }
    & uv tool update-shell
    if ($LASTEXITCODE -ne 0) {
        Write-Error "uv tool update-shell failed."
        exit 1
    }
    $toolBin = (& uv tool dir --bin 2>&1).Trim()
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    if ($toolBin) {
        $env:Path = "$toolBin;$env:Path"
    }
    # Re-check for running processes before final verification
    Assert-NoFccProcessesRunning
    $serverCmd = Get-ApplicationCommand "pcc-server"
    if (-not $serverCmd) {
        Write-Error "Verification failed: 'pcc-server' was not found on PATH or in $toolBin"
        exit 1
    }
    & pcc-server --version
    if ($LASTEXITCODE -ne 0) {
        Write-Error "pcc-server --version failed."
        exit 1
    }
}

function New-DesktopShortcut {
    param([string] $TargetPath)
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "$ShortcutName.lnk"

    # Check for existing unowned shortcut
    if (Test-Path $shortcutPath) {
        $shell = New-Object -ComObject WScript.Shell
        $existing = $shell.CreateShortcut($shortcutPath)
        $existingTarget = $existing.TargetPath
        # Check if it's ours by looking for the owner marker
        $ownerFile = Join-Path $env:LOCALAPPDATA "$OwnerMarker"
        if (-not (Test-Path $ownerFile)) {
            Write-Host "Desktop shortcut exists but is not managed by Free Claude Code; leaving it unchanged."
            return
        }
    }

    if ($DryRun) {
        Write-Host "+ Create-Shortcut $shortcutPath -> $TargetPath"
        return
    }

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.WorkingDirectory = [Environment]::GetFolderPath("UserProfile")
    $shortcut.Save()

    # Write owner marker
    $ownerFile = Join-Path $env:LOCALAPPDATA "$OwnerMarker"
    Set-Content -Path $ownerFile -Value $OwnerValue -Encoding UTF8
}

function New-StartMenuShortcut {
    param([string] $TargetPath)
    $programs = [Environment]::GetFolderPath("Programs")
    $shortcutPath = Join-Path $programs "$ShortcutName.lnk"

    if ($DryRun) {
        Write-Host "+ Create-Shortcut $shortcutPath -> $TargetPath"
        return
    }

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.WorkingDirectory = [Environment]::GetFolderPath("UserProfile")
    $shortcut.Save()
}

function Install-Shortcuts {
    $toolBin = ""
    if (-not $DryRun) {
        $toolBin = (& uv tool dir --bin 2>&1).Trim()
    }
    $target = if ($toolBin) { Join-Path $toolBin "pcc-desktop.cmd" } else { "pcc-desktop.cmd" }
    New-DesktopShortcut -TargetPath $target
    New-StartMenuShortcut -TargetPath $target
}

# --- Main ---
if ($Help) {
    Show-Usage
    exit 0
}

if ($TorchBackend -and -not ($VoiceLocal -or $VoiceAll)) {
    Write-Error "--TorchBackend requires --VoiceLocal or --VoiceAll."
    exit 1
}

Write-Step "Checking for running Free Claude Code processes"
Assert-NoFccProcessesRunning

Write-Step "Ensuring Claude Code is installed"
Ensure-Claude

Write-Step "Ensuring uv $MinUvVersion or newer is installed"
Ensure-Uv

Install-FreeClaudeCode

Configure-AndVerify

Write-Step "Creating desktop shortcuts"
Install-Shortcuts

if ($DryRun) {
    Write-Host ""
    Write-Host "Dry run complete. No changes were made."
} else {
    Write-Host ""
    Write-Host "Free Claude Code is installed and verified. Start the proxy with: pcc-server"
}
