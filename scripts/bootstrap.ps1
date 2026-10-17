#requires -Version 7
<#
作業場の準備（冪等）。
  -Only <repo,...>  指定したリポジトリだけを clone・設定する
  -SkipTools        外部ツールの取得を飛ばす
#>
param(
    [string[]]$Only = @(),
    [switch]$SkipTools
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$Utf8 = [Text.UTF8Encoding]::new($false)
$Root = Split-Path -Parent $PSScriptRoot

function Fail([string]$m) { Write-Error $m; exit 1 }
function Run([string]$exe, [string[]]$a) {
    & $exe @a
    if ($LASTEXITCODE -ne 0) { Fail "失敗: $exe $($a -join ' ')" }
}
# workspace.local.toml の「キー = "値"」だけを読む
function Get-LocalValue([string]$key) {
    $p = Join-Path $Root 'workspace.local.toml'
    foreach ($line in Get-Content -LiteralPath $p -Encoding utf8) {
        if ($line -match "^\s*$key\s*=\s*`"(.*)`"\s*(#.*)?$") { return $Matches[1] }
    }
    return $null
}

# 1. workspace.local.toml
$LocalToml = Join-Path $Root 'workspace.local.toml'
if (-not (Test-Path -LiteralPath $LocalToml)) {
    $tpl = @'
# PC 固有の設定（Git 管理外）。値を書いてから bootstrap.ps1 をもう一度実行する。
git = "C:/Program Files/Git/cmd/git.exe"
python = "C:/path/to/python3.12/python.exe"
ida_dir = "C:/Program Files/IDA Professional 9.3"
legacy_root = ""
'@
    [IO.File]::WriteAllText($LocalToml, ($tpl -replace "`r`n", "`n") + "`n", $Utf8)
    Write-Host "workspace.local.toml の雛形を作りました。値を書いてから、もう一度実行してください。"
    exit 1
}

# 2. git（Windows 版）
$Git = $env:AC3DS_GIT
if (-not $Git) { $Git = Get-LocalValue 'git' }
if (-not $Git -or -not (Test-Path -LiteralPath $Git)) {
    $cand = Join-Path $env:ProgramFiles 'Git\cmd\git.exe'
    if (Test-Path -LiteralPath $cand) { $Git = $cand }
    elseif (Get-Command git -ErrorAction SilentlyContinue) { $Git = (Get-Command git).Source }
    else { Fail 'git が見つかりません（workspace.local.toml の git を確認）' }
}
$env:AC3DS_GIT = $Git
Write-Host "git: $Git"

# 3. python（設定ファイルの読み込み用）
$Py = Get-LocalValue 'python'
if (-not $Py -or -not (Test-Path -LiteralPath $Py)) {
    $c = Get-Command python -ErrorAction SilentlyContinue
    if ($c) { $Py = $c.Source } else { Fail 'python が見つかりません（workspace.local.toml の python を確認）' }
}
$json = & $Py -c "import json,tomllib,sys; print(json.dumps(tomllib.load(open(sys.argv[1],'rb'))))" (Join-Path $Root 'workspace.toml')
if ($LASTEXITCODE -ne 0) { Fail 'workspace.toml を読めません' }
$ws = $json | ConvertFrom-Json -AsHashtable
$dataDir = if ($ws.ContainsKey('data')) { $ws['data'] } else { '_local' }

# 4. clone と git の設定
function Set-GitConfig([string]$dir, [string]$hooks) {
    Run $Git @('-C', $dir, 'config', 'core.longpaths', 'true')
    Run $Git @('-C', $dir, 'config', 'core.autocrlf', 'false')
    if ($hooks) {
        $abs = [IO.Path]::GetFullPath((Join-Path $dir $hooks)).Replace('\', '/')
        Run $Git @('-C', $dir, 'config', 'core.hooksPath', $abs)
    }
}
foreach ($name in $ws['repos'].Keys) {
    if ($Only.Count -gt 0 -and $Only -notcontains $name) { continue }
    $r = $ws['repos'][$name]
    $dir = Join-Path $Root $name
    if (Test-Path -LiteralPath (Join-Path $dir '.git')) {
        Write-Host "${name}: clone 済み"
    } else {
        Write-Host "${name}: clone します"
        Run $Git @('clone', $r['remote'], $dir)
    }
    $hooks = if ($r.ContainsKey('hooks')) { $r['hooks'] } else { $null }
    Set-GitConfig $dir $hooks
}
if (Test-Path -LiteralPath (Join-Path $Root '.git')) { Set-GitConfig $Root $null }

# 5. _local/
foreach ($d in 'builds', 'tools', 'venv', 'worktrees', 'refs', 'evidence', 'scratch', 'logs', 'state', 'orchestrator', 'legacy-ida') {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "$dataDir/$d") | Out-Null
}

# 6. venv
$VenvDir = Join-Path $Root "$dataDir/venv/tools"
$VenvPy = Join-Path $VenvDir 'Scripts/python.exe'
if (-not (Test-Path -LiteralPath $VenvPy)) {
    Write-Host 'venv を作ります'
    Run $Py @('-m', 'venv', $VenvDir)
}
Run $VenvPy @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '-r', (Join-Path $Root 'scripts/requirements.txt'))

# symbols の CSV は行単位で統合する（Git 設定は同じ値で上書きするので冪等）。
if (($Only.Count -eq 0 -or $Only -contains 'ac3ds-re') -and
    (Test-Path -LiteralPath (Join-Path $Root 'ac3ds-re/.git'))) {
    $MergePy = [IO.Path]::GetFullPath($VenvPy).Replace('\', '/')
    $Driver = '"' + $MergePy + '" tools/sync/merge_symbols.py "%O" "%A" "%B" "%P" --marker-size %L'
    Run $Git @('-C', (Join-Path $Root 'ac3ds-re'), 'config', 'merge.ac3ds-symbols.driver', $Driver)
    Run $Git @('-C', (Join-Path $Root 'ac3ds-re'), 'config', 'merge.ac3ds-symbols.name', 'ac3ds symbols CSV merge')
}

# 7. 外部ツールとスキルの写し
if (-not $SkipTools) { Run $VenvPy @((Join-Path $Root 'scripts/fetch_tools.py')) }
Run $VenvPy @((Join-Path $Root 'scripts/sync_skills.py'))
Write-Host 'bootstrap: 完了'
