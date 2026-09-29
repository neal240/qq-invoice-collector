$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$zipPath = Join-Path $appRoot '发票归集_分享版.zip'
$rootInZip = '发票归集'

# Explicit allowlist: never package settings.json, mail data, credentials or caches.
$files = @(
    '启动发票归集.cmd',
    'bootstrap.ps1',
    '使用说明.txt',
    'LICENSE',
    'invoice_gui.pyw',
    'invoice_mail.py',
    'ocr_windows.ps1',
    'credential_store.py',
    'vendor/LICENSE-pypdf.txt',
    '发行资源/python-3.13.15-amd64.exe'
)
$vendorRoot = Join-Path $appRoot 'vendor\pypdf'
$files += Get-ChildItem -LiteralPath $vendorRoot -Recurse -File |
    Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -ne '.pyc' } |
    ForEach-Object { $_.FullName.Substring($appRoot.Length + 1).Replace('\', '/') }

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath }
$archive = [System.IO.Compression.ZipFile]::Open($zipPath, 'Create')
try {
    foreach ($relative in $files) {
        $source = Join-Path $appRoot $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "分享包缺少文件：$relative"
        }
        $entryName = "$rootInZip/$($relative.Replace('\', '/'))"
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $archive, $source, $entryName,
            [System.IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
    }
} finally {
    $archive.Dispose()
}
Write-Output "已生成：$zipPath"
