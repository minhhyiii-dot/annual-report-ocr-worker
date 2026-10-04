[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("doctor", "setup", "import", "status", "run", "pack")]
    [string]$Command,

    [ValidateSet("auto", "gpu", "cpu")]
    [string]$Profile = "auto",

    [switch]$ApprovedByUser,

    [string]$JobZip,

    [string]$JobId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$WorkerRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$HomeRoot = Split-Path -Parent $WorkerRoot
$RuntimeRoot = Join-Path $HomeRoot ".runtime"
$RuntimeLockPath = Join-Path $WorkerRoot "runtime-lock.json"
$HomeChecksumPath = Join-Path $HomeRoot "worker_home_checksums.sha256"
$RuntimeLock = Get-Content -LiteralPath $RuntimeLockPath -Raw -Encoding UTF8 | ConvertFrom-Json
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }

    $builder = [Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) { [void]$builder.Append(('\' * ($backslashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [int]$TimeoutSeconds = 0
    )
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (@($Arguments | ForEach-Object { ConvertTo-NativeArgument ([string]$_) }) -join " ")
    $startInfo.WorkingDirectory = $HomeRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw "Native process did not start: $FilePath" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if ($TimeoutSeconds -gt 0) {
            if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
                try { $process.Kill() } catch { }
                throw "Native process timed out after $TimeoutSeconds seconds: $FilePath"
            }
        }
        else {
            $process.WaitForExit()
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        return [pscustomobject]@{
            exit_code = [int]$process.ExitCode
            stdout = [string]$stdout
            stderr = [string]$stderr
        }
    }
    catch {
        throw "Could not execute native process '$FilePath': $($_.Exception.Message)"
    }
    finally {
        $process.Dispose()
    }
}

function Get-NativeDiagnostic {
    param([Parameter(Mandatory = $true)][object]$Result)
    $parts = @()
    if (-not [string]::IsNullOrWhiteSpace([string]$Result.stderr)) { $parts += ([string]$Result.stderr).Trim() }
    if (-not [string]::IsNullOrWhiteSpace([string]$Result.stdout)) { $parts += ([string]$Result.stdout).Trim() }
    $message = ($parts -join " | ")
    if ($message.Length -gt 1000) { return $message.Substring(0, 1000) }
    return $message
}

function ConvertFrom-SingleJsonOutput {
    param(
        [Parameter(Mandatory = $true)][object]$Result,
        [Parameter(Mandatory = $true)][string]$Context
    )
    if ([string]::IsNullOrWhiteSpace([string]$Result.stdout)) {
        $diagnostic = Get-NativeDiagnostic $Result
        throw "$Context returned no JSON (exit $($Result.exit_code)): $diagnostic"
    }
    try {
        return ([string]$Result.stdout | ConvertFrom-Json)
    }
    catch {
        throw "$Context returned invalid or multiple JSON documents (exit $($Result.exit_code)): $($_.Exception.Message)"
    }
}

function Test-SafeRelativePath {
    param([Parameter(Mandatory = $true)][string]$Relative)
    if ([string]::IsNullOrWhiteSpace($Relative) -or $Relative.Contains("\") -or $Relative.Contains(":")) { return $false }
    if ([IO.Path]::IsPathRooted($Relative)) { return $false }
    $parts = $Relative.Split("/")
    if ($parts -contains "" -or $parts -contains "." -or $parts -contains "..") { return $false }
    return $true
}

function Assert-SafeHomePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $root = [IO.Path]::GetFullPath($HomeRoot).TrimEnd("\")
    $candidate = [IO.Path]::GetFullPath($Path)
    if ($candidate -ne $root -and -not $candidate.StartsWith($root + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes Worker Home: $candidate"
    }
}

function Get-ExplicitLinkTarget {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$CanonicalParent
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ([string]$item.LinkType -notin @("Junction", "SymbolicLink")) { return $null }
    $targets = @($item.Target | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
    if ($targets.Count -ne 1) { throw "Ambiguous filesystem link target: $Path" }
    $target = [string]$targets[0]
    if (-not [IO.Path]::IsPathRooted($target)) { $target = Join-Path $CanonicalParent $target }
    return [IO.Path]::GetFullPath($target)
}

function Resolve-SafeHomePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-SafeHomePath $Path
    $lexicalRoot = [IO.Path]::GetFullPath($HomeRoot).TrimEnd("\")
    $physicalRoot = $lexicalRoot
    for ($rootDepth = 0; $rootDepth -lt 32; $rootDepth += 1) {
        $rootTarget = Get-ExplicitLinkTarget -Path $physicalRoot -CanonicalParent (Split-Path -Parent $physicalRoot)
        if ($null -eq $rootTarget) { break }
        $physicalRoot = $rootTarget.TrimEnd("\")
    }
    if ($rootDepth -ge 32) { throw "Worker Home filesystem link chain is too deep" }
    $allowedRoots = @($lexicalRoot, $physicalRoot) | Select-Object -Unique

    function Test-InAllowedRoot([string]$Candidate) {
        foreach ($root in $allowedRoots) {
            if ($Candidate -eq $root -or $Candidate.StartsWith($root + "\", [StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
        return $false
    }

    function Resolve-Candidate([string]$Candidate, [int]$Depth) {
        if ($Depth -ge 32) { throw "Filesystem link chain is too deep: $Candidate" }
        $full = [IO.Path]::GetFullPath($Candidate).TrimEnd("\")
        $matchedRoot = $null
        foreach ($root in @($allowedRoots | Sort-Object Length -Descending)) {
            if ($full -eq $root -or $full.StartsWith($root + "\", [StringComparison]::OrdinalIgnoreCase)) {
                $matchedRoot = $root
                break
            }
        }
        if ($null -eq $matchedRoot) { throw "Resolved path escapes Worker Home: $full" }
        $relative = if ($full -eq $matchedRoot) { "" } else { $full.Substring($matchedRoot.Length + 1) }
        $inspectCurrent = $matchedRoot
        $canonicalCurrent = if ($matchedRoot -eq $lexicalRoot) { $physicalRoot } else { $matchedRoot }
        foreach ($part in @($relative.Split("\") | Where-Object { $_ -ne "" })) {
            $inspectNext = Join-Path $inspectCurrent $part
            $projected = [IO.Path]::GetFullPath((Join-Path $canonicalCurrent $part))
            $target = Get-ExplicitLinkTarget -Path $inspectNext -CanonicalParent $canonicalCurrent
            if ($null -ne $target) {
                $canonicalCurrent = Resolve-Candidate $target ($Depth + 1)
            }
            else {
                $canonicalCurrent = $projected
                if (-not (Test-InAllowedRoot $canonicalCurrent)) {
                    throw "Resolved path escapes Worker Home: $canonicalCurrent"
                }
            }
            $inspectCurrent = $inspectNext
        }
        return $canonicalCurrent
    }

    return Resolve-Candidate ([IO.Path]::GetFullPath($Path)) 0
}

function Assert-SafeMutationPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    [void](Resolve-SafeHomePath $Path)
}

function Get-HomeRelativePath {
    param([Parameter(Mandatory = $true)][string]$FullPath)
    $root = [IO.Path]::GetFullPath($HomeRoot).TrimEnd("\") + "\"
    $candidate = [IO.Path]::GetFullPath($FullPath)
    if (-not $candidate.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes Worker Home: $candidate"
    }
    return $candidate.Substring($root.Length).Replace("\", "/")
}

function Get-StaticFiles {
    $mutable = @(".runtime", "inbox", "jobs", "outbox", "logs")
    $files = [Collections.Generic.List[IO.FileInfo]]::new()
    foreach ($item in Get-ChildItem -LiteralPath $HomeRoot -Force) {
        if ($mutable -contains $item.Name) { continue }
        if (-not $item.PSIsContainer) {
            if ($item.Name -eq "worker_home_checksums.sha256") { continue }
            $files.Add($item)
            continue
        }
        foreach ($file in Get-ChildItem -LiteralPath $item.FullName -File -Recurse -Force) {
            $files.Add($file)
        }
    }
    return $files
}

function Test-HomeChecksums {
    if (-not (Test-Path -LiteralPath $HomeChecksumPath -PathType Leaf)) {
        throw "Missing worker_home_checksums.sha256"
    }
    $expected = [Collections.Generic.Dictionary[string, string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($line in Get-Content -LiteralPath $HomeChecksumPath -Encoding UTF8) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch '^([0-9a-fA-F]{64})  (.+)$') { throw "Malformed Worker Home checksum line" }
        $relative = $Matches[2]
        if (-not (Test-SafeRelativePath $relative)) { throw "Unsafe checksum path: $relative" }
        if ($expected.ContainsKey($relative)) { throw "Duplicate checksum path: $relative" }
        $expected.Add($relative, $Matches[1].ToLowerInvariant())
    }
    $actual = [Collections.Generic.Dictionary[string, IO.FileInfo]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($file in Get-StaticFiles) {
        $relative = Get-HomeRelativePath $file.FullName
        if ($actual.ContainsKey($relative)) { throw "Duplicate static path: $relative" }
        $actual.Add($relative, $file)
    }
    foreach ($relative in $expected.Keys) {
        if (-not $actual.ContainsKey($relative)) { throw "Checksum entry missing on disk: $relative" }
        $digest = (Get-FileHash -LiteralPath $actual[$relative].FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($digest -ne $expected[$relative]) { throw "Checksum mismatch: $relative" }
    }
    foreach ($relative in $actual.Keys) {
        if (-not $expected.ContainsKey($relative)) { throw "Unexpected static file: $relative" }
    }
    return $expected.Count
}

function Test-HttpEndpoint {
    param([Parameter(Mandatory = $true)][string]$Url)
    $response = $null
    try {
        $request = [Net.HttpWebRequest]::Create([Uri]$Url)
        $request.Method = "HEAD"
        $request.AllowAutoRedirect = $false
        $request.Timeout = 5000
        $request.ReadWriteTimeout = 5000
        $response = [Net.HttpWebResponse]$request.GetResponse()
        return [pscustomobject]@{
            url = $Url
            method = "HEAD"
            reachable = $true
            status_code = [int]$response.StatusCode
            status = [string]$response.StatusDescription
            error = $null
        }
    }
    catch [Net.WebException] {
        if ($null -ne $_.Exception.Response) {
            $response = [Net.HttpWebResponse]$_.Exception.Response
            return [pscustomobject]@{
                url = $Url
                method = "HEAD"
                reachable = $true
                status_code = [int]$response.StatusCode
                status = [string]$response.StatusDescription
                error = $null
            }
        }
        return [pscustomobject]@{
            url = $Url
            method = "HEAD"
            reachable = $false
            status_code = $null
            status = $null
            error = $_.Exception.Message
        }
    }
    catch {
        return [pscustomobject]@{
            url = $Url
            method = "HEAD"
            reachable = $false
            status_code = $null
            status = $null
            error = $_.Exception.Message
        }
    }
    finally { if ($null -ne $response) { $response.Close() } }
}

function Get-NvidiaReport {
    $report = [ordered]@{ detected = $false; cuda_supported = $null; gpus = @(); advisory = $true }
    $nvidia = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($null -eq $nvidia) { return [pscustomobject]$report }
    try {
        $rows = & $nvidia.Source --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader,nounits 2>$null
        $banner = & $nvidia.Source 2>$null | Out-String
        if ($banner -match 'CUDA Version:\s*([0-9.]+)') { $report.cuda_supported = $Matches[1] }
        $gpus = @()
        foreach ($row in @($rows)) {
            $parts = $row.Split(",") | ForEach-Object { $_.Trim() }
            if ($parts.Count -lt 4) { continue }
            $gpus += [pscustomobject]@{
                name = $parts[0]
                memory_mib = [int]$parts[1]
                driver = $parts[2]
                compute_capability = $parts[3]
            }
        }
        $report.detected = $gpus.Count -gt 0
        $report.gpus = $gpus
    }
    catch { $report.error = $_.Exception.Message }
    return [pscustomobject]$report
}

function Get-SelectedProfile {
    param([Parameter(Mandatory = $true)][string]$Requested, [Parameter(Mandatory = $true)][object]$GpuReport)
    if ($Requested -ne "auto") { return $Requested }
    if ($GpuReport.detected) { return "gpu" }
    return "cpu"
}

function Get-ProfileRuntimeCode {
    param([Parameter(Mandatory = $true)][ValidateSet("gpu", "cpu")][string]$SelectedProfile)
    if ($SelectedProfile -eq "gpu") { return "g" }
    return "c"
}

function Get-SetupState {
    $path = Join-Path $RuntimeRoot "setup_state.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try {
        $payload = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$payload.schema_version -ne "2.1") { return $null }
        return $payload
    }
    catch { return $null }
}

function Get-SetupStateIssue {
    $path = Join-Path $RuntimeRoot "setup_state.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try { $payload = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { return "Existing setup state is invalid; use a clean Worker Home extraction." }
    if ([string]$payload.schema_version -ne "2.1") {
        return "Legacy setup state is not supported; use a clean Worker Home extraction."
    }
    return $null
}

function Get-OptionalProperty {
    param([object]$Object, [Parameter(Mandatory = $true)][string]$Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Resolve-HomeRelativePath {
    param([Parameter(Mandatory = $true)][string]$Relative)
    if (-not (Test-SafeRelativePath $Relative)) { throw "Unsafe Worker Home relative path: $Relative" }
    $resolved = Join-Path $HomeRoot $Relative.Replace("/", "\")
    [void](Resolve-SafeHomePath $resolved)
    return [IO.Path]::GetFullPath($resolved)
}

function Get-ProfilePythonPath {
    param(
        [Parameter(Mandatory = $true)][object]$State,
        [Parameter(Mandatory = $true)][string]$SelectedProfile
    )
    $profiles = Get-OptionalProperty -Object $State -Name "profiles"
    $profileState = Get-OptionalProperty -Object $profiles -Name $SelectedProfile
    $relative = [string](Get-OptionalProperty -Object $profileState -Name "python")
    if (-not [string]::IsNullOrWhiteSpace($relative)) {
        return Resolve-HomeRelativePath $relative
    }
    throw "Runtime state 2.1 does not record Python for profile '$SelectedProfile'"
}

function Enter-SetupLock {
    Assert-SafeMutationPath $RuntimeRoot
    New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
    Assert-SafeMutationPath $RuntimeRoot
    $lockPath = Join-Path $RuntimeRoot "setup.lock"
    Assert-SafeMutationPath $lockPath
    try {
        return [IO.File]::Open(
            $lockPath,
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    }
    catch [IO.IOException] {
        throw "Another Worker Home setup is already running. Wait for it to finish, then run doctor again."
    }
}

function Test-ValidLocalUv {
    param([Parameter(Mandatory = $true)][string]$UvPath)
    Assert-SafeMutationPath $UvPath
    if (-not (Test-Path -LiteralPath $UvPath -PathType Leaf)) { return $false }
    try {
        $result = Invoke-NativeCapture -FilePath $UvPath -Arguments @("--version") -TimeoutSeconds 30
        if ($result.exit_code -ne 0) { return $false }
        $versionPattern = '^uv\s+' + [regex]::Escape([string]$RuntimeLock.uv.version) + '(?:\s|$)'
        return ([string]$result.stdout).Trim() -match $versionPattern
    }
    catch { return $false }
}

function Get-LockedUv {
    $uv = Join-Path $RuntimeRoot "tools\uv.exe"
    Assert-SafeHomePath $uv
    if (Test-ValidLocalUv $uv) { return $uv }

    $expectedSha = [string](Get-OptionalProperty -Object $RuntimeLock.uv -Name "installer_sha256")
    if ($expectedSha -notmatch '^[0-9a-fA-F]{64}$') {
        throw "runtime-lock.json is missing a valid pinned uv.installer_sha256"
    }
    $expectedSha = $expectedSha.ToLowerInvariant()
    $installer = Join-Path $RuntimeRoot "downloads\uv-install-$($RuntimeLock.uv.version).ps1"
    $partial = "$installer.part"
    Assert-SafeMutationPath $installer
    Assert-SafeMutationPath $partial

    $installerReady = $false
    if (Test-Path -LiteralPath $installer -PathType Leaf) {
        $installerReady = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant() -eq $expectedSha
        if (-not $installerReady) { Remove-Item -LiteralPath $installer -Force }
    }
    if (-not $installerReady) {
        if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
        Assert-SafeMutationPath $partial
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $RuntimeLock.uv.installer_url -OutFile $partial | Out-Null
            $actualSha = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actualSha -ne $expectedSha) {
                throw "uv installer checksum mismatch: expected $expectedSha, observed $actualSha"
            }
            Move-Item -LiteralPath $partial -Destination $installer -Force
        }
        finally {
            if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
        }
    }

    [Environment]::SetEnvironmentVariable("UV_VERSION", [string]$RuntimeLock.uv.version, "Process")
    try {
        $installResult = Invoke-NativeCapture -FilePath "powershell.exe" -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $installer
        )
    }
    finally {
        [Environment]::SetEnvironmentVariable("UV_VERSION", $null, "Process")
    }
    if ($installResult.exit_code -ne 0) {
        throw "uv installer failed with exit code $($installResult.exit_code): $(Get-NativeDiagnostic $installResult)"
    }
    if (-not (Test-ValidLocalUv $uv)) {
        throw "Pinned uv $($RuntimeLock.uv.version) was not installed inside Worker Home"
    }
    return $uv
}

function Install-AndFindManagedPython {
    param([Parameter(Mandatory = $true)][string]$UvPath)
    $installResult = Invoke-NativeCapture -FilePath $UvPath -Arguments @(
        "python", "install", "--no-bin", "--no-registry", [string]$RuntimeLock.python.version
    )
    if ($installResult.exit_code -ne 0) {
        throw "uv could not install managed Python $($RuntimeLock.python.version) (exit $($installResult.exit_code)): $(Get-NativeDiagnostic $installResult)"
    }

    $lookupResult = Invoke-NativeCapture -FilePath $UvPath -Arguments @(
        "python", "find", [string]$RuntimeLock.python.version
    )
    if ($lookupResult.exit_code -ne 0) {
        throw "uv-managed Python lookup failed with exit code $($lookupResult.exit_code): $(Get-NativeDiagnostic $lookupResult)"
    }
    $candidates = @(([string]$lookupResult.stdout -split "`r?`n") | Where-Object {
        -not [string]::IsNullOrWhiteSpace([string]$_)
    })
    if ($candidates.Count -eq 0) { throw "uv-managed Python lookup returned no path" }
    $managedPython = ([string]$candidates[-1]).Trim()
    Assert-SafeMutationPath $managedPython
    if (-not (Test-Path -LiteralPath $managedPython -PathType Leaf)) {
        throw "uv-managed Python could not be located inside Worker Home"
    }
    return [IO.Path]::GetFullPath($managedPython)
}

function Invoke-Doctor {
    $checks = [Collections.Generic.List[object]]::new()
    function Add-Check([string]$Name, [bool]$Passed, [object]$Details, [bool]$Blocking = $false) {
        $checks.Add([pscustomobject]@{ name = $Name; passed = $Passed; blocking = $Blocking; details = $Details })
    }

    try {
        $count = Test-HomeChecksums
        Add-Check "worker_home_checksums" $true "$count static files" $true
    }
    catch { Add-Check "worker_home_checksums" $false $_.Exception.Message $true }

    try {
        $os = Get-CimInstance Win32_OperatingSystem
        Add-Check "windows_x64" ([Environment]::Is64BitOperatingSystem -and [Environment]::OSVersion.Version.Major -ge 10) `
            ([pscustomobject]@{ caption = $os.Caption; build = $os.BuildNumber; advisory = $true })
    }
    catch { Add-Check "windows_x64" $false ([pscustomobject]@{ error = $_.Exception.Message; advisory = $true }) }

    try {
        $ramGiB = [Math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
        Add-Check "ram" $true ([pscustomobject]@{ observed_gib = $ramGiB; advisory = $true })
    }
    catch { Add-Check "ram" $false ([pscustomobject]@{ error = $_.Exception.Message; advisory = $true }) }

    try {
        $driveRoot = [IO.Path]::GetPathRoot($HomeRoot)
        $freeGiB = [Math]::Round(([IO.DriveInfo]::new($driveRoot)).AvailableFreeSpace / 1GB, 1)
        Add-Check "free_disk" $true ([pscustomobject]@{ observed_gib = $freeGiB; advisory = $true })
    }
    catch { Add-Check "free_disk" $false ([pscustomobject]@{ error = $_.Exception.Message; advisory = $true }) }

    try {
        $longPathsValue = (Get-ItemProperty -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -ErrorAction Stop).LongPathsEnabled
        $longPathsEnabled = [int]$longPathsValue -eq 1
        Add-Check "windows_long_paths" $longPathsEnabled ([pscustomobject]@{
            enabled = $longPathsEnabled
            home_path_characters = $HomeRoot.Length
            advisory = $true
            remediation = "If setup reports a path-too-long error, ask the user to re-extract Worker Home to a shorter path; do not move it automatically."
        })
    }
    catch { Add-Check "windows_long_paths" $false ([pscustomobject]@{ error = $_.Exception.Message; advisory = $true }) }

    $gpu = Get-NvidiaReport
    Add-Check "nvidia_gpu" ([bool]$gpu.detected) $gpu
    $selected = Get-SelectedProfile -Requested $Profile -GpuReport $gpu

    $network = @()
    $allNetwork = $true
    foreach ($url in $RuntimeLock.downloads.network_probes) {
        $endpoint = Test-HttpEndpoint $url
        $network += $endpoint
        if (-not $endpoint.reachable) { $allNetwork = $false }
    }
    Add-Check "network" $allNetwork ([pscustomobject]@{ method = "HEAD"; endpoints = $network })

    $setupStateIssue = Get-SetupStateIssue
    $state = Get-SetupState
    $installedProfiles = @()
    $stateProfiles = Get-OptionalProperty -Object $state -Name "profiles"
    if ($null -ne $stateProfiles) {
        $installedProfiles = @($stateProfiles.PSObject.Properties.Name)
    }
    $activeProfile = Get-OptionalProperty -Object $state -Name "active_profile"
    $selectedInstalled = $installedProfiles -contains $selected
    $selectedProfileRecord = Get-OptionalProperty -Object $stateProfiles -Name $selected
    $recordedDeviceSmoke = Get-OptionalProperty -Object $selectedProfileRecord -Name "device_smoke"
    $recordedCompletedAt = Get-OptionalProperty -Object $selectedProfileRecord -Name "completed_at"
    $runtimeReady = $false
    $runtimeVerification = $null
    $runtimeError = $setupStateIssue
    if ($selectedInstalled) {
        try {
            $profilePython = Get-ProfilePythonPath -State $state -SelectedProfile $selected
            Assert-SafeMutationPath $profilePython
            if (-not (Test-Path -LiteralPath $profilePython -PathType Leaf)) {
                throw "profile Python is missing"
            }
            $runtimeProbe = Join-Path $WorkerRoot "runtime_probe.py"
            Assert-SafeHomePath $runtimeProbe
            if (-not (Test-Path -LiteralPath $runtimeProbe -PathType Leaf)) {
                throw "worker/runtime_probe.py is missing"
            }
            $probeResult = Invoke-NativeCapture -FilePath $profilePython -Arguments @(
                "-B", "-I", $runtimeProbe,
                "--home-root", $HomeRoot,
                "--profile", $selected,
                "--runtime-lock", $RuntimeLockPath
            ) -TimeoutSeconds 120
            if ($probeResult.exit_code -notin @(0, 2, 4)) {
                throw "runtime probe failed with exit code $($probeResult.exit_code): $(Get-NativeDiagnostic $probeResult)"
            }
            $runtimeVerification = ConvertFrom-SingleJsonOutput -Result $probeResult -Context "runtime probe"
            $probeStatus = [string](Get-OptionalProperty -Object $runtimeVerification -Name "status")
            $runtimeReady = $probeResult.exit_code -eq 0 -and $probeStatus -eq "READY"
            if (-not $runtimeReady) {
                $reportedError = [string](Get-OptionalProperty -Object $runtimeVerification -Name "error")
                $runtimeError = if ([string]::IsNullOrWhiteSpace($reportedError)) {
                    "runtime probe reported $probeStatus"
                }
                else { $reportedError }
            }
        }
        catch { $runtimeError = $_.Exception.Message }
    }
    Add-Check "runtime" $runtimeReady ([pscustomobject]@{
        requested_profile = $Profile
        recommended_profile = $(if ($gpu.detected) { "gpu" } else { "cpu" })
        selected_profile = $selected
        installed_profiles = $installedProfiles
        active_profile = $activeProfile
        verification = $runtimeVerification
        recorded_device_smoke = $recordedDeviceSmoke
        recorded_setup_completed_at = $recordedCompletedAt
        setup_state_issue = $setupStateIssue
        error = $runtimeError
        advisory = $true
    })

    $blockingFailures = @($checks | Where-Object { $_.blocking -and -not $_.passed })
    $profileLock = $RuntimeLock.profiles.$selected
    $profileRuntimeCode = Get-ProfileRuntimeCode -SelectedProfile $selected
    $modelLock = $RuntimeLock.model_cache
    return [pscustomobject]@{
        status = if ($blockingFailures.Count -eq 0) { "READY" } else { "BLOCKED" }
        preflight_mode = "advisory_only"
        checks = $checks
        recommended_profile = $(if ($gpu.detected) { "gpu" } else { "cpu" })
        selected_profile = $selected
        setup_required = (-not $runtimeReady) -or ($activeProfile -ne $selected)
        setup_requires_approval = $true
        planned_changes = [pscustomobject]@{
            runtime_download_gib_min = $profileLock.estimated_runtime_download_gib_min
            runtime_download_gib_max = $profileLock.estimated_runtime_download_gib_max
            runtime_installed_gib = $profileLock.estimated_runtime_installed_gib
            estimate_note = $profileLock.estimate_note
            model_download_gib_min = $modelLock.estimated_download_gib_min
            model_download_gib_max = $modelLock.estimated_download_gib_max
            model_installed_gib = $modelLock.estimated_installed_gib
            model_estimate_note = $modelLock.estimate_note
            first_use_total_download_gib_min = $profileLock.estimated_runtime_download_gib_min + $modelLock.estimated_download_gib_min
            first_use_total_download_gib_max = $profileLock.estimated_runtime_download_gib_max + $modelLock.estimated_download_gib_max
            first_use_total_installed_gib = $profileLock.estimated_runtime_installed_gib + $modelLock.estimated_installed_gib
            runtime_directory = ".runtime"
            profile_directory = ".runtime/v/$profileRuntimeCode"
            shared_model_cache = ".runtime/paddlex_cache"
            reuses_existing_profile = $selectedInstalled
            admin = $false
            docker = $false
            driver_changes = $false
            global_path_changes = $false
        }
    }
}

function Set-LocalRuntimeEnvironment {
    # uv reads every UV_* variable from the parent process. Remove inherited
    # policy/configuration first so a machine-level setting cannot redirect
    # package sources, Python locations, or offline behavior.
    $safeUvTls = [ordered]@{}
    foreach ($name in @("UV_NATIVE_TLS", "UV_SYSTEM_CERTS")) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if (-not [string]::IsNullOrWhiteSpace($value)) { $safeUvTls[$name] = $value }
    }
    foreach ($entry in @([Environment]::GetEnvironmentVariables("Process").Keys)) {
        $name = [string]$entry
        if ($name.StartsWith("UV_", [StringComparison]::OrdinalIgnoreCase)) {
            [Environment]::SetEnvironmentVariable($name, $null, "Process")
        }
    }
    foreach ($name in $safeUvTls.Keys) {
        [Environment]::SetEnvironmentVariable($name, [string]$safeUvTls[$name], "Process")
    }
    foreach ($name in @(
        "INSTALLER_DOWNLOAD_URL",
        "CARGO_DIST_FORCE_INSTALL_DIR",
        "INSTALLER_NO_MODIFY_PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE"
    )) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    $localUserHome = Join-Path $RuntimeRoot "home"
    foreach ($directory in @(
        $RuntimeRoot,
        $localUserHome,
        (Join-Path $localUserHome "AppData\Local"),
        (Join-Path $localUserHome "AppData\Roaming"),
        (Join-Path $localUserHome ".cache"),
        (Join-Path $RuntimeRoot "tools"),
        (Join-Path $RuntimeRoot "python"),
        (Join-Path $RuntimeRoot "python-bin"),
        (Join-Path $RuntimeRoot "cache\uv"),
        (Join-Path $RuntimeRoot "cache\pip"),
        (Join-Path $RuntimeRoot "cache\huggingface\hub"),
        (Join-Path $RuntimeRoot "cache\modelscope"),
        (Join-Path $RuntimeRoot "cache\torch"),
        (Join-Path $RuntimeRoot "cache\paddle"),
        (Join-Path $RuntimeRoot "cache\cuda"),
        (Join-Path $RuntimeRoot "cache\xdg"),
        (Join-Path $RuntimeRoot "downloads"),
        (Join-Path $RuntimeRoot "tmp"),
        (Join-Path $RuntimeRoot "paddlex_cache"),
        (Join-Path $RuntimeRoot "v")
    )) {
        Assert-SafeMutationPath $directory
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        Assert-SafeMutationPath $directory
    }
    $env:UV_UNMANAGED_INSTALL = Join-Path $RuntimeRoot "tools"
    $env:UV_NO_MODIFY_PATH = "1"
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeRoot "python"
    $env:UV_PYTHON_BIN_DIR = Join-Path $RuntimeRoot "python-bin"
    $env:UV_PYTHON_INSTALL_BIN = "0"
    $env:UV_PYTHON_INSTALL_REGISTRY = "0"
    $env:UV_PYTHON_NO_REGISTRY = "1"
    $env:UV_CACHE_DIR = Join-Path $RuntimeRoot "cache\uv"
    $env:UV_PYTHON_PREFERENCE = "only-managed"
    $env:UV_NO_CONFIG = "1"
    $env:INSTALLER_NO_MODIFY_PATH = "1"
    $env:PYTHONNOUSERSITE = "1"
    # Some imported libraries ignore their own cache variables and resolve
    # paths from the process user profile (for example paddle.dataset.common).
    # These overrides are process-local and keep those implicit writes inside
    # Worker Home without modifying the real Windows profile or registry.
    $env:HOME = $localUserHome
    $env:USERPROFILE = $localUserHome
    $env:LOCALAPPDATA = Join-Path $localUserHome "AppData\Local"
    $env:APPDATA = Join-Path $localUserHome "AppData\Roaming"
    $env:PIP_CACHE_DIR = Join-Path $RuntimeRoot "cache\pip"
    $env:HF_HOME = Join-Path $RuntimeRoot "cache\huggingface"
    $env:HUGGINGFACE_HUB_CACHE = Join-Path $RuntimeRoot "cache\huggingface\hub"
    $env:MODELSCOPE_CACHE = Join-Path $RuntimeRoot "cache\modelscope"
    $env:TORCH_HOME = Join-Path $RuntimeRoot "cache\torch"
    $env:PADDLE_HOME = Join-Path $RuntimeRoot "cache\paddle"
    $env:CUDA_CACHE_PATH = Join-Path $RuntimeRoot "cache\cuda"
    $env:XDG_CACHE_HOME = Join-Path $RuntimeRoot "cache\xdg"
    $env:TEMP = Join-Path $RuntimeRoot "tmp"
    $env:TMP = Join-Path $RuntimeRoot "tmp"
    $env:PADDLE_PDX_CACHE_HOME = Join-Path $RuntimeRoot "paddlex_cache"
    $env:PADDLE_PDX_MODEL_SOURCE = $RuntimeLock.cache_environment.PADDLE_PDX_MODEL_SOURCE
    $env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = $RuntimeLock.cache_environment.PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK
    $env:OCR_WORKER_HOME_ROOT = $HomeRoot
}

function New-SetupFailureResult {
    param(
        [Parameter(Mandatory = $true)][string]$DefaultStage,
        [Parameter(Mandatory = $true)][string]$ErrorMessage,
        [Parameter(Mandatory = $true)][string]$SelectedProfile
    )
    $stage = $DefaultStage
    if ($ErrorMessage -match '(?i)network (?:error|failure)|connection|timed? out|timeout|temporary failure|name resolution|resolve host|\bdns\b|proxy|\btls\b|ssl certificate|unreachable|unable to connect|failed to connect|error sending request') {
        $stage = "network"
    }
    $bounded = [string]$ErrorMessage
    if (-not [string]::IsNullOrWhiteSpace($HomeRoot)) {
        $bounded = [regex]::Replace($bounded, [regex]::Escape($HomeRoot), "<WORKER_HOME>", [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    }
    $bounded = [regex]::Replace($bounded, '(?i)(https?://)([^/\s:@]+):([^@\s/]+)@', '$1<redacted>@')
    $bounded = [regex]::Replace($bounded, '(?i)(token|key|password|secret)=([^&\s]+)', '$1=<redacted>')
    if ($bounded.Length -gt 1200) { $bounded = $bounded.Substring($bounded.Length - 1200) }
    $exitCode = if ($stage -eq "configuration") { 2 } elseif ($stage -in @("dependency", "paddle_import")) { 4 } else { 3 }
    return [pscustomobject]@{
        exit_code = $exitCode
        payload = [pscustomobject]@{
            schema_version = "2.1"
            status = "BLOCKED"
            profile = $SelectedProfile
            stage = $stage
            error = $bounded
        }
        stderr = ""
    }
}

function Invoke-Setup {
    if (-not $ApprovedByUser) { throw "setup requires explicit user approval and -ApprovedByUser" }
    $doctor = Invoke-Doctor
    if ($doctor.status -ne "READY") { throw "doctor is BLOCKED; setup was not started" }
    $selected = $doctor.selected_profile
    $setupStateIssue = Get-SetupStateIssue
    if (-not [string]::IsNullOrWhiteSpace([string]$setupStateIssue)) {
        return New-SetupFailureResult -DefaultStage "configuration" -ErrorMessage $setupStateIssue -SelectedProfile $selected
    }
    $setupLock = $null
    try {
        # This is the first mutating operation in setup. The lock is held until
        # setup_runtime.py exits so concurrent agents cannot share bootstrap state.
        $setupLock = Enter-SetupLock
        Set-LocalRuntimeEnvironment
        try { $uv = Get-LockedUv }
        catch { return New-SetupFailureResult -DefaultStage "installer" -ErrorMessage $_.Exception.Message -SelectedProfile $selected }
        try { $managedPython = Install-AndFindManagedPython -UvPath $uv }
        catch { return New-SetupFailureResult -DefaultStage "python" -ErrorMessage $_.Exception.Message -SelectedProfile $selected }

        try {
            $setupRuntime = Join-Path $WorkerRoot "setup_runtime.py"
            Assert-SafeHomePath $setupRuntime
            if (-not (Test-Path -LiteralPath $setupRuntime -PathType Leaf)) {
                throw "worker/setup_runtime.py is missing"
            }
            $setupResult = Invoke-NativeCapture -FilePath $managedPython -Arguments @(
                "-B", "-I", $setupRuntime,
                "--home-root", $HomeRoot,
                "--profile", $selected,
                "--uv-path", $uv,
                "--runtime-lock", $RuntimeLockPath
            )
            if ($setupResult.exit_code -notin @(0, 1, 2, 3, 4)) {
                throw "setup runtime failed with unexpected exit code $($setupResult.exit_code): $(Get-NativeDiagnostic $setupResult)"
            }
            $payload = ConvertFrom-SingleJsonOutput -Result $setupResult -Context "setup runtime"
            $status = [string](Get-OptionalProperty -Object $payload -Name "status")
            if ($setupResult.exit_code -eq 0 -and $status -ne "READY") {
                throw "setup runtime exited successfully but reported status '$status'"
            }
        }
        catch { return New-SetupFailureResult -DefaultStage "dependency" -ErrorMessage $_.Exception.Message -SelectedProfile $selected }
        return [pscustomobject]@{
            exit_code = [int]$setupResult.exit_code
            payload = $payload
            stderr = [string]$setupResult.stderr
        }
    }
    catch {
        return New-SetupFailureResult -DefaultStage "installer" -ErrorMessage $_.Exception.Message -SelectedProfile $selected
    }
    finally {
        if ($null -ne $setupLock) { $setupLock.Dispose() }
    }
}

function Get-WorkerPython {
    param([Parameter(Mandatory = $true)][bool]$NeedsProfile)
    $state = Get-SetupState
    if ($null -eq $state) { throw "Runtime is not set up. Run doctor, obtain approval, then run setup." }
    if ($NeedsProfile) {
        $profile = [string](Get-OptionalProperty -Object $state -Name "active_profile")
        if ([string]::IsNullOrWhiteSpace($profile)) { throw "No active runtime profile" }
        $python = Get-ProfilePythonPath -State $state -SelectedProfile $profile
    }
    else {
        $managedPython = [string](Get-OptionalProperty -Object $state -Name "managed_python")
        if ([string]::IsNullOrWhiteSpace($managedPython)) { throw "Managed Python is missing from setup state" }
        $python = Resolve-HomeRelativePath $managedPython
    }
    Assert-SafeHomePath $python
    Assert-SafeMutationPath $python
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Local Worker Home Python is missing" }
    return [pscustomobject]@{ python = $python; state = $state }
}

function Invoke-WorkerPython {
    param([Parameter(Mandatory = $true)][string]$Mode)
    [void](Test-HomeChecksums)
    $needsProfile = $Mode -in @("run", "pack")
    $runtime = Get-WorkerPython -NeedsProfile $needsProfile
    Set-LocalRuntimeEnvironment
    $activeProfile = [string](Get-OptionalProperty -Object $runtime.state -Name "active_profile")
    $env:OCR_WORKER_PROFILE = $activeProfile
    if ($Mode -eq "run") {
        $runtimeProbe = Join-Path $WorkerRoot "runtime_probe.py"
        Assert-SafeHomePath $runtimeProbe
        $probeResult = Invoke-NativeCapture -FilePath $runtime.python -Arguments @(
            "-B", "-I", $runtimeProbe,
            "--home-root", $HomeRoot,
            "--profile", $activeProfile,
            "--runtime-lock", $RuntimeLockPath,
            "--deep"
        ) -TimeoutSeconds 180
        if ($probeResult.exit_code -notin @(0, 2, 4)) {
            throw "Deep runtime probe failed with exit code $($probeResult.exit_code): $(Get-NativeDiagnostic $probeResult)"
        }
        $probe = ConvertFrom-SingleJsonOutput -Result $probeResult -Context "deep runtime probe"
        if ($probeResult.exit_code -ne 0) {
            $probeError = [string](Get-OptionalProperty -Object $probe -Name "error")
            throw "Deep runtime probe blocked run with exit code $($probeResult.exit_code): $probeError"
        }
        if (
            [string](Get-OptionalProperty -Object $probe -Name "status") -ne "READY" -or
            [string](Get-OptionalProperty -Object $probe -Name "mode") -ne "deep" -or
            [string](Get-OptionalProperty -Object $probe -Name "profile") -ne $activeProfile
        ) {
            throw "Deep runtime probe returned an invalid readiness report"
        }
    }
    $arguments = @((Join-Path $WorkerRoot "ocr_worker.py"), $Mode, "--home-root", $HomeRoot)
    if ($Mode -eq "import" -and -not [string]::IsNullOrWhiteSpace($JobZip)) {
        $arguments += @("--job-zip", $JobZip)
    }
    if ($Mode -in @("run", "pack")) {
        if ([string]::IsNullOrWhiteSpace($JobId)) { throw "$Mode requires -JobId" }
        $arguments += @("--job-id", $JobId, "--profile", $activeProfile)
    }
    & $runtime.python -B -I @arguments
    if ($LASTEXITCODE -ne 0) { throw "Worker $Mode failed with exit code $LASTEXITCODE" }
}

switch ($Command) {
    "doctor" {
        $result = Invoke-Doctor
        $result | ConvertTo-Json -Depth 12
        if ($result.status -ne "READY") { exit 2 }
    }
    "setup" {
        $setup = Invoke-Setup
        if (-not [string]::IsNullOrWhiteSpace([string]$setup.stderr)) {
            [Console]::Error.Write([string]$setup.stderr)
        }
        $setup.payload | ConvertTo-Json -Depth 12
        if ($setup.exit_code -ne 0) { exit $setup.exit_code }
    }
    "import" { Invoke-WorkerPython "import" }
    "status" { Invoke-WorkerPython "status" }
    "run" { Invoke-WorkerPython "run" }
    "pack" { Invoke-WorkerPython "pack" }
}
