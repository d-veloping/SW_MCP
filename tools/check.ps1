# The one check before a commit (#8): the server import and the offline tests as in CI, then the live SOLIDWORKS
# checks one after another.  The first red step stops the run, and the script exits non-zero, so
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\check.ps1 && git commit ...
# commits nothing after a red run, from cmd, Bash or PowerShell 7 alike.  In Windows PowerShell 5.1, which has no
# `&&`, use the same `powershell ... -File tools\check.ps1` followed by `; if ($LASTEXITCODE -eq 0) { git commit ... }`;
# not `.\tools\check.ps1`, because a script the execution policy blocks leaves $LASTEXITCODE at its old value.
# Never pipe the check into another command (`| tail`): the exit code that counts is then the last command's.
#
# Every tests\live_*.py is either run here ($LiveTests) or listed with the reason it is not ($NotRun); a live script
# in neither list stops the run before anything starts, so a new one cannot drop out silently.  The live scripts
# need a running SOLIDWORKS; without one they fail, and so does the check.  If other processes drive the same
# SOLIDWORKS, run the live part only while you hold their lock.
#
# The run ends with a summary per step and the line
#   CHECK OK <offline|live> Head <sha>[+]      or      CHECK FAILED <step> Head <sha>[+]
# A `+` after the head means the working tree differs from it (changed or untracked files), so the run did not
# check exactly that commit.  Exit 0 when every step passed, 1 when a step failed or a live script is unclassified,
# 2 when the check itself cannot run (no Python, git cannot read the head or the working tree).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\check.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\check.ps1 -Offline
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\check.ps1 -Python C:\path\to\.venv\Scripts\python.exe
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\check.ps1 -SelfTest
param(
    [switch]$Offline,
    [switch]$SelfTest,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
# The checkout under test comes first on the import path, also in a worktree whose venv has the package installed
# editable from another checkout; the server import step prints where solidworks_mcp was loaded from.
$env:PYTHONPATH = $Root
if (-not $Python) { $Python = Join-Path $Root ".venv\Scripts\python.exe" }
$Mode = if ($SelfTest) { "selftest" } elseif ($Offline) { "offline" } else { "live" }

$LiveTests = @(
    "live_p0_regression.py",
    "live_list_dimensions.py",
    "live_set_dimension.py",
    "live_sheet_metal.py",
    "live_weldment.py"
)
$NotRun = [ordered]@{
    "live_activation.py"         = "measurement, always exits 0"
    "live_drawing_activation.py" = "measurement, always exits 0; needs --part"
    "live_open_document.py"      = "measurement, always exits 0; needs --target"
    "live_display_mode.py"       = "needs a saved part as argument"
    "live_drawing_clean.py"      = "needs a saved turned part with its axis along x as argument"
    "live_pdf_export.py"         = "needs a saved part as argument"
}

$script:Results = New-Object System.Collections.Generic.List[object]

trap {
    Write-Host ("CHECK ERROR {0}: {1}" -f $Mode, $_)
    exit 2
}

function Stop-CannotRun([string]$Text) {
    Write-Host ("CHECK ERROR {0}: {1}" -f $Mode, $Text)
    exit 2
}

# Live scripts in tests\ that are in neither list.
function Get-Unclassified([string]$TestsDir) {
    $known = @($LiveTests) + @($NotRun.Keys)
    return @(Get-ChildItem -Path $TestsDir -Filter "live_*.py" -File | ForEach-Object { $_.Name } |
        Where-Object { $known -notcontains $_ })
}

# Runs one step as its own process and records it.  The exit code decides, never output on stderr; nothing is
# piped behind the process except Out-Host, which leaves $LASTEXITCODE to the process.
function Invoke-Step([string]$Name, [string[]]$Arguments) {
    Write-Host ("=== {0}" -f $Name)
    $start = Get-Date
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 0
    & $Python @Arguments | Out-Host
    $code = $LASTEXITCODE
    $seconds = [int]((Get-Date) - $start).TotalSeconds
    $outcome = if ($code -eq 0) { "OK" } else { "FAILED" }
    $script:Results.Add([pscustomobject]@{ Step = $Name; Result = $outcome; Seconds = $seconds; Exit = $code })
    Write-Host ("--- {0}: {1} (exit {2}, {3} s)" -f $Name, $outcome, $code, $seconds)
    return ($code -eq 0)
}

function Write-Summary([string]$Line) {
    Write-Host ""
    Write-Host "Summary"
    foreach ($r in $script:Results) {
        $exit = if ($null -eq $r.Exit) { "" } else { "exit $($r.Exit)" }
        Write-Host ("  {0,-34} {1,-8} {2,5} s  {3}" -f $r.Step, $r.Result, $r.Seconds, $exit)
    }
    if ($Mode -eq "live") {
        foreach ($name in $NotRun.Keys) { Write-Host ("  {0,-34} not in this check: {1}" -f "tests\$name", $NotRun[$name]) }
    }
    Write-Host $Line
}

function Get-Head {
    $global:LASTEXITCODE = 0
    $sha = "$(git -C $Root rev-parse --short HEAD)".Trim()
    if ($LASTEXITCODE -ne 0 -or -not $sha) { Stop-CannotRun "git rev-parse HEAD failed: the head cannot be read" }
    $status = @(git -C $Root status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) { Stop-CannotRun "git status failed: the working tree cannot be read" }
    if (@($status | Where-Object { $_ }).Count -gt 0) { return "$sha+" }
    return $sha
}

# Runs the steps in order; the first red one stops the run and marks the rest as not run.
function Invoke-Check {
    $unclassified = @(Get-Unclassified (Join-Path $Root "tests"))
    if ($unclassified.Count -gt 0) {
        Write-Host ("Unclassified live scripts: {0}. Add each to `$LiveTests or `$NotRun in tools\check.ps1." -f ($unclassified -join ", "))
        Write-Host ("CHECK FAILED unclassified live scripts Head {0}" -f $Head)
        exit 1
    }

    $steps = @(
        @{ Name = "server import"; Arguments = @("-c", "import solidworks_mcp; from solidworks_mcp import server; print(len(server.TOOLS), 'tools registered from', solidworks_mcp.__file__)") },
        @{ Name = "unit tests"; Arguments = @("-m", "unittest", "discover", "-s", "tests", "-v") }
    )
    if (-not $Offline) {
        foreach ($name in $LiveTests) { $steps += @{ Name = "tests\$name"; Arguments = @("tests\$name") } }
    }

    for ($i = 0; $i -lt $steps.Count; $i++) {
        if (-not (Invoke-Step $steps[$i].Name $steps[$i].Arguments)) {
            for ($j = $i + 1; $j -lt $steps.Count; $j++) {
                $script:Results.Add([pscustomobject]@{ Step = $steps[$j].Name; Result = "not run"; Seconds = 0; Exit = $null })
            }
            Write-Summary ("CHECK FAILED {0} Head {1}" -f $steps[$i].Name, $Head)
            exit 1
        }
    }
    Write-Summary ("CHECK OK {0} Head {1}" -f $Mode, $Head)
    exit 0
}

# Self-test without SOLIDWORKS: the step runner, the classification and the exit code seen from outside, including
# the `&&` chain from the issue.  Returns the number of failed cases.
function Invoke-SelfTest {
    $failed = 0
    $dir = Join-Path ([System.IO.Path]::GetTempPath()) ("sw-mcp-check-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force $dir | Out-Null
    try {
        $cases = @(
            @{ Name = "exit 0"; Code = "raise SystemExit(0)"; Pass = $true },
            @{ Name = "exit 1"; Code = "raise SystemExit(1)"; Pass = $false },
            @{ Name = "unhandled exception"; Code = "raise RuntimeError('red on purpose')"; Pass = $false },
            @{ Name = "stderr output, exit 0"; Code = "import sys; sys.stderr.write('only a warning\n')"; Pass = $true }
        )
        foreach ($c in $cases) {
            $script = Join-Path $dir ("case{0}.py" -f [array]::IndexOf($cases, $c))
            Set-Content -Path $script -Value $c.Code -Encoding ASCII
            $passed = Invoke-Step ("self-test: " + $c.Name) @($script)
            if ($passed -eq $c.Pass) { Write-Host ("ok: {0} counts as {1}" -f $c.Name, $(if ($passed) { "passed" } else { "failed" })) }
            else { $failed++; Write-Host ("FAILED: {0} counted as passed={1}" -f $c.Name, $passed) }
        }

        $tests = Join-Path $dir "tests"
        New-Item -ItemType Directory -Force $tests | Out-Null
        foreach ($name in @($LiveTests) + @($NotRun.Keys) + @("live_new.py", "test_other.py")) {
            Set-Content -Path (Join-Path $tests $name) -Value "" -Encoding ASCII
        }
        $found = @(Get-Unclassified $tests)
        if (($found -join ",") -eq "live_new.py") { Write-Host "ok: an unlisted live_new.py is unclassified" }
        else { $failed++; Write-Host ("FAILED: unclassified scripts found: [{0}], expected [live_new.py]" -f ($found -join ",")) }

        $red = Join-Path $dir "red.cmd"
        $green = Join-Path $dir "green.cmd"
        Set-Content -Path $red -Value "@exit /b 1" -Encoding ASCII
        Set-Content -Path $green -Value "@exit /b 0" -Encoding ASCII

        # A throwaway repo whose tests\ holds only an unlisted live script: the whole run has to stop with exit 1.
        $repo = Join-Path $dir "repo"
        New-Item -ItemType Directory -Force (Join-Path $repo "tools"), (Join-Path $repo "tests") | Out-Null
        Copy-Item $PSCommandPath (Join-Path $repo "tools\check.ps1")
        Set-Content -Path (Join-Path $repo "tests\live_new.py") -Value "" -Encoding ASCII
        $git = @("-C", $repo, "-c", "user.name=selftest", "-c", "user.email=selftest@example.invalid")
        git @git init -q | Out-Null
        git @git add -A | Out-Null
        git @git commit -q -m selftest | Out-Null

        foreach ($c in @(@{ Name = "red.cmd"; Script = $PSCommandPath; Fake = $red; Expected = 1; Line = "^CHECK FAILED server import Head " },
                         @{ Name = "green.cmd"; Script = $PSCommandPath; Fake = $green; Expected = 0; Line = "^CHECK OK offline Head " },
                         @{ Name = "an unlisted live_new.py"; Script = (Join-Path $repo "tools\check.ps1"); Fake = $green;
                            Expected = 1; Line = "^CHECK FAILED unclassified live scripts Head " })) {
            $global:LASTEXITCODE = 0
            $output = @(& powershell -NoProfile -ExecutionPolicy Bypass -File $c.Script -Offline -Python $c.Fake |
                ForEach-Object { "$_" })
            $code = $LASTEXITCODE
            $last = $output | Where-Object { $_ -match "^CHECK " } | Select-Object -Last 1
            if ($code -eq $c.Expected -and $last -match $c.Line) {
                Write-Host ("ok: from outside, {0} gives exit {1}: {2}" -f $c.Name, $code, $last)
            } else {
                $failed++
                Write-Host ("FAILED: from outside, {0} gave exit {1} and {2}" -f $c.Name, $code, $last)
                $output | ForEach-Object { Write-Host ("    " + $_) }
            }
        }

        # The chain from the issue: the red check has to run, fail, and stop the command after &&.
        $chain = Join-Path $dir "chain.cmd"
        Set-Content -Path $chain -Encoding ASCII -Value @(
            ('@powershell -NoProfile -ExecutionPolicy Bypass -File "{0}" -Offline -Python "{1}" && echo CHAIN CONTINUED' -f $PSCommandPath, $red),
            '@echo CHAIN EXIT %errorlevel%')
        $output = @(& cmd.exe /d /c $chain | ForEach-Object { "$_" })
        $ran = @($output | Where-Object { $_ -match "^CHECK FAILED server import Head " }).Count -eq 1
        if ($ran -and $output -notcontains "CHAIN CONTINUED" -and $output -contains "CHAIN EXIT 1") {
            Write-Host "ok: the && chain stops after a red check (exit 1)"
        } else {
            $failed++
            Write-Host "FAILED: the && chain after a red check"
            $output | ForEach-Object { Write-Host ("    " + $_) }
        }
    } finally {
        Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
    }
    return $failed
}

if (-not (Test-Path $Python)) {
    Stop-CannotRun ("Python not found: {0}. Create the venv (README, Installation) or pass -Python." -f $Python)
}
$Head = Get-Head
Write-Host ("SW_MCP check {0}, head {1}, python {2}" -f $Mode, $Head, $Python)

if ($SelfTest) {
    $n = Invoke-SelfTest
    $line = if ($n -eq 0) { "CHECK OK selftest Head $Head" } else { "CHECK FAILED selftest ($n cases) Head $Head" }
    Write-Host $line
    if ($n -eq 0) { exit 0 } else { exit 1 }
}
Invoke-Check
