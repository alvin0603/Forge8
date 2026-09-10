# Windows PowerShell 5.1+. Run only from a trusted Forge8 checkout.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$Assets,
    [Parameter(Mandatory = $true)][string]$State,
    [string]$Archives
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitProcess) { throw 'Use native 64-bit Windows PowerShell, not WSL.' }
if ($env:PATHEXT -notmatch '(?i)(^|;)\.EXE(;|$)') { throw 'This shell cannot launch .exe files because PATHEXT lacks .EXE. Open a fresh native Windows PowerShell with its normal environment; no system settings were changed.' }
$Repo = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
function DirectoryPath([string]$Value) {
    if ($Value -notmatch '^[A-Za-z]:[\\/]' -or $Value -match '(^|[\\/])\.\.([\\/]|$)') { throw 'Choose an absolute local drive path without .. components.' }
    $Full = [IO.Path]::GetFullPath($Value).TrimEnd([char[]]'\/')
    if ($Full.Length -le 3) { throw 'Do not select a drive root.' }
    $Current = $Full
    while ($Current) {
        if (Test-Path -LiteralPath $Current) {
            $Item = Get-Item -LiteralPath $Current -Force
            if (-not $Item.PSIsContainer -or ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Directory is a file or redirected: $Current" }
        }
        $Current = Split-Path -Parent $Current
    }
    return $Full
}
function ContainsPath([string]$Parent, [string]$Child) {
    return $Parent -eq $Child -or $Child.StartsWith($Parent + '\', [StringComparison]::OrdinalIgnoreCase)
}
$Assets = DirectoryPath $Assets; $State = DirectoryPath $State
foreach ($Place in @($Assets, $State)) {
    if ((ContainsPath $Repo $Place) -or (ContainsPath $Place $Repo)) { throw 'Assets and state must be outside the code checkout, not its ancestor.' }
}
if ((ContainsPath $Assets $State) -or (ContainsPath $State $Assets)) { throw 'Use separate, nonoverlapping asset and private-state directories.' }
if ($Archives) { $Archives = DirectoryPath $Archives; if (-not (Test-Path -LiteralPath $Archives)) { throw 'Local archive directory does not exist.' } }
foreach ($Setting in @(@('FORGE8_HOME', $Assets), @('FORGE8_STATE_HOME', $State))) {
    $Override = [Environment]::GetEnvironmentVariable($Setting[0])
    if ($null -ne $Override -and $Override -ne $Setting[1]) { throw "Existing $($Setting[0]) overrides this setup; deliberately clear or correct it first." }
}
$Runtime = Get-Content -LiteralPath "$Repo\config\runtimes\llama_cpp_b10621.json" -Raw | ConvertFrom-Json
$Model = Get-Content -LiteralPath "$Repo\config\models\qwen35_9b_q4_k_m.json" -Raw | ConvertFrom-Json
$Python = "$Repo\.venv\Scripts\python.exe"; $Forge8 = "$Repo\.venv\Scripts\forge8.exe"
if (Test-Path -LiteralPath "$Repo\.venv") { $null = DirectoryPath "$Repo\.venv"; if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Existing .venv is not a Windows Python environment; it will not be replaced.' } }
elseif (-not (Get-Command py.exe -ErrorAction SilentlyContinue)) { throw 'Install native Python 3.10+ with its py launcher first; this script does not install Python.' }
if (-not $Archives -and -not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { throw 'curl.exe is required for explicit asset downloads, or supply -Archives.' }
$RuntimeRoot = DirectoryPath "$Assets\runtime"; $ModelRoot = DirectoryPath "$Assets\models"
$Install = DirectoryPath "$RuntimeRoot\$($Runtime.install_dir)"
Write-Host "Code/venv: $Repo\.venv`nAssets: $Assets`nPrivate state: $State"
Write-Host "Qwen: $($Model.files[0].size_bytes) bytes; installed Windows runtime: $(($Runtime.installed_files | Measure-Object size_bytes -Sum).Sum) bytes."
Write-Host 'Two runtime ZIP sizes are not declared in the manifest. Reserve 12 GiB for a fresh asset installation; no Gemma weights, GPU start, driver or global PATH changes.'
Write-Host 'Existing files are reused only after complete SHA-256 checks. Wrong/partial files are never overwritten or deleted.'
foreach ($Asset in $Runtime.assets) { Write-Host "Runtime archive: $RuntimeRoot\$($Asset.filename)" }
Write-Host "Model file: $ModelRoot\$($Model.files[0].filename)"
Write-Host $(if ($Archives) { "Assets come only from $Archives; Python packaging may still contact PyPI." } else { 'Missing assets download from the pinned GitHub/Hugging Face URLs; Python packaging uses PyPI.' })
foreach ($Place in @($Assets, $State, $Repo)) {
    $Drive = New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($Place))
    $Required = 512MB
    if ($Place -eq $Assets) {
        if (-not (Test-Path -LiteralPath "$ModelRoot\$($Model.files[0].filename)")) { $Required = 12GB }
        elseif (-not (Test-Path -LiteralPath $Install) -or @($Runtime.assets | Where-Object { -not (Test-Path -LiteralPath "$RuntimeRoot\$($_.filename)") }).Count) { $Required = 4GB }
    }
    if ($Drive.AvailableFreeSpace -lt $Required) { throw "Insufficient space on $($Drive.Name): need at least $Required free bytes for this plan." }
}
if (-not $PSCmdlet.ShouldProcess($Assets, 'Install Forge8 and provision the pinned Windows Qwen reader')) { return }
if ((Read-Host 'Type YES to install/provision at these paths (anything else cancels)') -cne 'YES') { throw 'Cancelled; nothing was installed.' }
function CheckExit([string]$Step) { if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE); stop and inspect the output above." } }
function Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function CheckFile([string]$Path, [string]$Expected) {
    $Item = Get-Item -LiteralPath $Path -Force
    if ($Item.PSIsContainer -or ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or (Hash $Path) -ne $Expected) { throw "File is redirected or SHA-256 differs; left untouched: $Path" }
}
if (-not (Test-Path -LiteralPath $Python)) { & py.exe -3 -m venv "$Repo\.venv"; CheckExit 'Create native venv' }
& $Python -I -c 'import sys,os; assert os.name == chr(110)+chr(116) and sys.version_info >= (3,10) and sys.maxsize > 2**32'; CheckExit 'Python 3.10+ x64 check'
& $Python -I -m pip --isolated --disable-pip-version-check install --no-cache-dir 'setuptools==84.0.0'; CheckExit 'Install pinned build backend'
& $Python -I -m pip --isolated --disable-pip-version-check install --no-cache-dir --no-deps --no-build-isolation -e $Repo; CheckExit 'Install local Forge8'
$SavedText = & $Forge8 configure; CheckExit 'Read existing deployment'
$Saved = ($SavedText -join "`n") | ConvertFrom-Json
if ($Saved.saved -and ($Saved.saved.assets -ne $Assets -or $Saved.saved.state -ne $State)) { throw 'Forge8 is already configured elsewhere. Keep those paths, or deliberately change them with configure --replace; setup will not replace them.' }
New-Item -ItemType Directory -Path $RuntimeRoot, $ModelRoot -Force | Out-Null
$Config = "$Assets\config"
if (Test-Path -LiteralPath $Config) {
    $null = DirectoryPath $Config
    foreach ($File in Get-ChildItem -LiteralPath "$Repo\config" -File -Recurse) {
        $Relative = $File.FullName.Substring(("$Repo\config\").Length)
        CheckFile "$Config\$Relative" (Hash $File.FullName)
    }
} else { Copy-Item -LiteralPath "$Repo\config" -Destination $Config -Recurse }
function Acquire([string]$Name, [string]$Url, [string]$Expected, [string]$Directory) {
    $Target = Join-Path $Directory $Name
    if (Test-Path -LiteralPath $Target) { CheckFile $Target $Expected; return }
    if ($Archives) {
        $Local = Join-Path $Archives $Name; CheckFile $Local $Expected
        [IO.File]::Copy($Local, $Target, $false)
    } else {
        $Partial = $Target + '.partial'
        if (Test-Path -LiteralPath $Partial) { throw "Partial download already exists; inspect it manually: $Partial" }
        $Reserved = [IO.File]::Open($Partial, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write); $Reserved.Dispose()
        & curl.exe --fail --location --proto '=https' --proto-redir '=https' --output $Partial $Url
        CheckExit "Download $Name"; CheckFile $Partial $Expected
        [IO.File]::Move($Partial, $Target)
    }
    CheckFile $Target $Expected
}
function ExpandPinnedRuntime($Runtime, [string]$RuntimeRoot, [string]$Install) {
    if (-not (Test-Path -LiteralPath $Install)) {
        Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
        New-Item -ItemType Directory -Path $Install | Out-Null
        $Expected = @{}; foreach ($File in $Runtime.installed_files) { $Expected[$File.filename] = $File }
        foreach ($Asset in $Runtime.assets) {
            CheckFile (Join-Path $RuntimeRoot $Asset.filename) $Asset.sha256
            $Archive = [IO.Compression.ZipFile]::OpenRead((Join-Path $RuntimeRoot $Asset.filename))
            try {
                foreach ($Entry in $Archive.Entries) {
                    if ($Entry.FullName -cne $Entry.Name -or -not $Expected.ContainsKey($Entry.Name) -or $Entry.Length -ne $Expected[$Entry.Name].size_bytes) { throw 'Runtime ZIP has an unexpected entry, path or size; extraction stopped.' }
                    $Destination = Join-Path $Install $Entry.Name
                    $InputStream = $Entry.Open()
                    try {
                        $OutputStream = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
                        try { $InputStream.CopyTo($OutputStream) } finally { $OutputStream.Dispose() }
                    } finally { $InputStream.Dispose() }
                    CheckFile $Destination $Expected[$Entry.Name].sha256
                }
            } finally { $Archive.Dispose() }
        }
    } else { $null = DirectoryPath $Install }
}
foreach ($Asset in $Runtime.assets) { Acquire $Asset.filename $Asset.url $Asset.sha256 $RuntimeRoot }
Acquire $Model.files[0].filename $Model.files[0].download_url $Model.files[0].sha256 $ModelRoot
ExpandPinnedRuntime $Runtime $RuntimeRoot $Install
& $Forge8 runtime verify --root $RuntimeRoot --manifest "$Config\runtimes\llama_cpp_b10621.json"; CheckExit 'Full runtime verification'
& $Forge8 model verify --root $ModelRoot --manifest "$Config\models\qwen35_9b_q4_k_m.json"; CheckExit 'Full Qwen verification'
& $Forge8 configure --assets $Assets --state $State; CheckExit 'Save deployment'
Write-Host "Ready for an explicit reading request (GPU/answer quality not tested by setup).`nRun: & '$Forge8' read '$Repo\examples\isolated-functions'"
Write-Host 'Keep the terminal open; the printed browser URL is private. Ctrl+C shuts down the desk. Gemma repair and the optional WASI trial runtime were not installed.'
