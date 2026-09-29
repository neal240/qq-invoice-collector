param([Parameter(Mandatory = $true)][string]$ImagePath)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } |
    Select-Object -First 1

function Await-WinRT($operation, $resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    return $task.GetAwaiter().GetResult()
}

$storageFileType = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$streamType = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$decoderType = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$bitmapType = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$ocrType = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$ocrResultType = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
$fileAccessType = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType=WindowsRuntime]

$engine = $ocrType::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    throw 'Windows OCR language pack unavailable'
}

$file = Await-WinRT ($storageFileType::GetFileFromPathAsync($ImagePath)) $storageFileType
$stream = Await-WinRT ($file.OpenAsync($fileAccessType::Read)) $streamType
try {
    $decoder = Await-WinRT ($decoderType::CreateAsync($stream)) $decoderType
    $bitmap = Await-WinRT ($decoder.GetSoftwareBitmapAsync()) $bitmapType
    try {
        $result = Await-WinRT ($engine.RecognizeAsync($bitmap)) $ocrResultType
        $lines = @($result.Lines | ForEach-Object { $_.Text })
        ConvertTo-Json -InputObject $lines -Compress
    } finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
    }
} finally {
    if ($null -ne $stream) { $stream.Dispose() }
}
