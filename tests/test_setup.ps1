# Pure PS5.1 fixtures: no Python, network, real runtime, model or Pester.
param([string]$WorkRoot = [IO.Path]::GetTempPath())
$ErrorActionPreference = 'Stop'; Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'Run with native Windows PowerShell.' }
$Root = Join-Path ([IO.Path]::GetFullPath($WorkRoot)) ('forge8-setup-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Root | Out-Null # Retained; no recursive deletion.
$Production = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\setup.ps1'
$Tokens = $null; $Errors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile($Production, [ref]$Tokens, [ref]$Errors)
if ($Errors.Count) { throw ($Errors | Out-String) }
$Names = @('DirectoryPath', 'ContainsPath', 'Hash', 'CheckFile', 'Acquire', 'ExpandPinnedRuntime')
foreach ($Name in $Names) {
    $Functions = @($Ast.FindAll({ param($Node) $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq $Name }, $true))
    if ($Functions.Count -ne 1) { throw "Missing/duplicate production function $Name" }
    . ([ScriptBlock]::Create($Functions[0].Extent.Text)) # Trusted function definitions only; not installer body.
}
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$script:Checks = 0
function Assert([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message }; $script:Checks++ }
function Fails([scriptblock]$Action, [string]$Pattern, [type]$ExceptionType = [Exception]) {
    $Caught = $null; try { & $Action } catch { $Caught = $_ }
    $Actual = if ($null -eq $Caught) { 'no exception' } else { $Caught.Exception.Message }
    Assert ($null -ne $Caught -and $Actual -match $Pattern -and $Caught.Exception.GetBaseException() -is $ExceptionType) "Expected refusal: $Pattern ($ExceptionType); got $Actual"
}
$Bytes = [Text.Encoding]::UTF8.GetBytes('owned inert bytes')
[IO.File]::WriteAllBytes("$Root\expected.txt", $Bytes); $Digest = Hash "$Root\expected.txt"
function Fixture([string]$Name, [string[]]$Entries, [string[]]$Declared = @('server.exe')) {
    $Zip = "$Root\$Name.zip"; $Archive = [IO.Compression.ZipFile]::Open($Zip, [IO.Compression.ZipArchiveMode]::Create)
    try { foreach ($Entry in $Entries) { $Stream = $Archive.CreateEntry($Entry).Open(); try { $Stream.Write($Bytes, 0, $Bytes.Length) } finally { $Stream.Dispose() } } }
    finally { $Archive.Dispose() }
    return [pscustomobject]@{ install_dir = 'installed'; assets = @([pscustomobject]@{ filename = "$Name.zip"; sha256 = (Hash $Zip) });
        installed_files = @($Declared | ForEach-Object { [pscustomobject]@{ filename = $_; sha256 = $Digest; size_bytes = $Bytes.Length } }) }
}
$Good = Fixture 'good' @('server.exe'); $Install = "$Root\good-installed"
ExpandPinnedRuntime $Good $Root $Install
Assert ((Hash "$Install\server.exe") -eq $Digest) 'Extracted bytes differ'
$Before = (Get-Item "$Install\server.exe").LastWriteTimeUtc
ExpandPinnedRuntime $Good $Root $Install
Assert ((Get-Item "$Install\server.exe").LastWriteTimeUtc -eq $Before) 'Reuse overwrote the file'
$Duplicate = Fixture 'duplicate' @('server.exe', 'server.exe')
Fails { ExpandPinnedRuntime $Duplicate $Root "$Root\duplicate-installed" } '.' ([IO.IOException])
Assert ((Hash "$Root\duplicate-installed\server.exe") -eq $Digest) 'Create-new failure overwrote prior bytes'
$Nested = Fixture 'nested' @('../server.exe')
Fails { ExpandPinnedRuntime $Nested $Root "$Root\nested-installed" } 'unexpected'
Assert (-not (Test-Path "$Root\server.exe")) 'Archive escaped its directory'
$Wrong = Fixture 'wrong' @('server.exe'); $Wrong.installed_files[0].sha256 = '0' * 64
Fails { ExpandPinnedRuntime $Wrong $Root "$Root\wrong-installed" } 'SHA-256'
$BadZip = Fixture 'bad-zip' @('server.exe'); $BadZip.assets[0].sha256 = '0' * 64
Fails { ExpandPinnedRuntime $BadZip $Root "$Root\bad-zip-installed" } 'SHA-256'
Assert (-not (Test-Path "$Root\bad-zip-installed\server.exe")) 'Read an unverified ZIP'
$Missing = Fixture 'missing' @('server.exe') @('server.exe', 'library.dll')
ExpandPinnedRuntime $Missing $Root "$Root\missing-installed"
Assert (-not (Test-Path "$Root\missing-installed\library.dll")) 'Missing inventory was invented'
# Full inventory/reused-directory rejection belongs to the subsequent existing runtime verify CLI.
Assert ($Ast.Extent.Text -match 'runtime verify --root') 'Installer omitted full runtime verification'
$Archives = "$Root\local"; $Target = "$Root\acquired"
New-Item -ItemType Directory -Path $Archives, $Target | Out-Null
[IO.File]::WriteAllBytes("$Archives\tiny.bin", $Bytes)
Acquire 'tiny.bin' 'https://unused.invalid/' $Digest $Target
Assert ((Hash "$Target\tiny.bin") -eq $Digest) 'Local acquisition changed bytes'
Acquire 'tiny.bin' 'https://unused.invalid/' $Digest $Target
[IO.File]::WriteAllText("$Target\tiny.bin", 'different'); $WrongDigest = Hash "$Target\tiny.bin"
Fails { Acquire 'tiny.bin' 'https://unused.invalid/' $Digest $Target } 'SHA-256'
Assert ((Hash "$Target\tiny.bin") -eq $WrongDigest) 'Wrong existing target was overwritten'
$Checkout = "$Root\checkout"
New-Item -ItemType Directory -Path "$Checkout\scripts", "$Checkout\config\runtimes", "$Checkout\config\models" | Out-Null
[IO.File]::Copy($Production, "$Checkout\scripts\setup.ps1")
[IO.File]::WriteAllText("$Checkout\config\runtimes\llama_cpp_b10621.json", ($Good | ConvertTo-Json -Depth 8))
[IO.File]::WriteAllText("$Checkout\config\models\qwen35_9b_q4_k_m.json", (@{ files = @(@{filename='tiny.bin'; size_bytes=17}) } | ConvertTo-Json -Depth 8))
# Deterministic prerequisite observations only; no mock native command may run.
function py.exe { throw 'Forbidden native process' }; function curl.exe { throw 'Forbidden network' }
function New-Object([string]$TypeName, $ArgumentList) {
    if ($TypeName -ne 'IO.DriveInfo') { throw 'Unexpected constructor' }
    return [pscustomobject]@{Name='fixture'; AvailableFreeSpace=32GB}
}
$PromptCount = @{ value = 0 }
function Read-Host { $PromptCount.value++; return 'NO' }
$OldAssets = $env:FORGE8_HOME; $OldState = $env:FORGE8_STATE_HOME
$OldExtensions = $env:PATHEXT
try {
    $env:FORGE8_HOME = $null; $env:FORGE8_STATE_HOME = $null
    $env:PATHEXT = '.CPL'
    Fails { & "$Checkout\scripts\setup.ps1" -Assets "$Root\planned-assets" -State "$Root\planned-state" -Archives $Archives -WhatIf } 'PATHEXT lacks .EXE'
    $env:PATHEXT = '.COM;.EXE;.BAT;.CMD'
    & "$Checkout\scripts\setup.ps1" -Assets "$Root\planned-assets" -State "$Root\planned-state" -Archives $Archives -WhatIf
    Assert ($PromptCount.value -eq 0) 'WhatIf prompted for execution'
    Fails { & "$Checkout\scripts\setup.ps1" -Assets "$Root\planned-assets" -State "$Root\planned-state" -Archives $Archives } '^Cancelled;'
    Assert ($PromptCount.value -eq 1) 'Decline did not reach the explicit consent question'
    foreach ($Path in @("$Root\planned-assets", "$Root\planned-state", "$Checkout\.venv")) { Assert (-not (Test-Path $Path)) "Dry/declined setup created $Path" }
} finally { $env:FORGE8_HOME = $OldAssets; $env:FORGE8_STATE_HOME = $OldState; $env:PATHEXT = $OldExtensions }
Write-Output (@{ok=$true; assertions=$script:Checks; fixture_root=$Root; native_processes=0; downloads=0} | ConvertTo-Json -Compress)
