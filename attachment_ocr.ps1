# UTF-8 JSON in/out. Fixed local OCR only; never evaluates document text.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
$taskMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and
    $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await-Local($operation, [Type]$resultType) {
    $task = $taskMethod.MakeGenericMethod($resultType).Invoke($null, @($operation))
    if (-not $task.Wait(15000)) { throw 'OCR timeout' }
    return $task.Result
}
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if ($request.paths.Count -gt 40) { throw 'Too many OCR regions' }
    $engines = @()
    foreach ($tag in @('ja', 'en-US')) {
        $language = New-Object Windows.Globalization.Language $tag
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
        if ($null -eq $engine) { throw 'Japanese and English OCR must be installed' }
        $engines += $engine
    }
    $texts = New-Object 'System.Collections.Generic.List[string]'
    foreach ($path in $request.paths) {
        $file = Await-Local ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await-Local ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $bitmap = $null
        try {
            $decoder = Await-Local ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmap = Await-Local ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore)) ([Windows.Graphics.Imaging.SoftwareBitmap])
            foreach ($engine in $engines) {
                $result = Await-Local ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
                $texts.Add($result.Text)
            }
        } finally {
            if ($null -ne $bitmap) { $bitmap.Dispose() }
            $stream.Dispose()
        }
    }
    @{ok=$true; texts=@($texts.ToArray()); languages=@('ja','en-US')} | ConvertTo-Json -Depth 4 -Compress
} catch {
    # No filenames or extracted private content in diagnostics.
    @{ok=$false; error='Local OCR unavailable or failed'} | ConvertTo-Json -Compress
    exit 1
}
