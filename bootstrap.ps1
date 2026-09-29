param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$guiPath = Join-Path $appRoot 'invoice_gui.pyw'
$localPython = Join-Path $appRoot '.python\python.exe'
$installer = Join-Path $appRoot '发行资源\python-3.13.15-amd64.exe'
$expectedHash = 'EDEC09C4853AEAE9AC36EFB8C9F95B6B8E2FEE65EEE56D9767A8B7C69C574403'

function Show-Notice([string]$message, [string]$title, [string]$icon) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $message, $title, 'OK', $icon
    ) | Out-Null
}

function Test-Python([string]$candidate) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $false }
    if (-not (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $candidate) 'pythonw.exe'))) { return $false }
    try {
        & $candidate -c 'import sys, tkinter; assert sys.version_info >= (3, 10); import tkinter.ttk' 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Find-Python {
    if (Test-Python $localPython) { return $localPython }
    foreach ($command in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        if (Test-Python $command.Source) { return $command.Source }
    }
    return $null
}

try {
    if (-not (Test-Path -LiteralPath $guiPath -PathType Leaf)) {
        throw '找不到 invoice_gui.pyw。请重新解压完整分享包。'
    }

    $python = Find-Python
    if ($CheckOnly) {
        if ($python) { Write-Output "READY: $python"; exit 0 }
        Write-Output 'INSTALL_REQUIRED'
        exit 0
    }

    if (-not $python) {
        if (-not [Environment]::Is64BitOperatingSystem) {
            throw '此分享包适用于 64 位 Windows。'
        }
        if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
            throw '安装文件缺失。请使用完整的“发票归集_分享版.zip”重新解压。'
        }
        $actualHash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
        if ($actualHash -ne $expectedHash) {
            throw 'Python 安装文件校验失败，请重新获取分享包。'
        }
        Show-Notice '首次启动正在安装图形界面所需的 Python，可能需要几分钟。安装完成后会自动打开软件；请勿关闭接下来的安装窗口。' '发票归集 · 首次安装' 'Information'
        Write-Host '正在安装 Python，请稍候……'
        $targetDir = Join-Path $appRoot '.python'
        $args = @(
            '/passive', 'InstallAllUsers=0', "TargetDir=`"$targetDir`"",
            'Include_tcltk=1', 'Include_pip=0', 'Include_launcher=0',
            'PrependPath=0', 'Include_test=0', 'Include_doc=0',
            'Shortcuts=0', 'AssociateFiles=0'
        )
        $process = Start-Process -FilePath $installer -ArgumentList $args -Wait -PassThru
        if ($process.ExitCode -ne 0 -or -not (Test-Python $localPython)) {
            throw "Python 安装没有完成（退出码：$($process.ExitCode)）。请检查磁盘空间后重试。"
        }
        $python = $localPython
    }

    $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
    Start-Process -FilePath $pythonw -ArgumentList "`"$guiPath`"" -WorkingDirectory $appRoot
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-Host $message -ForegroundColor Red
    if (-not $CheckOnly) {
        Show-Notice $message '发票归集 · 启动失败' 'Error'
    }
    exit 1
}
