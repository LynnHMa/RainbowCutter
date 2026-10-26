@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

rem ===== Config =====
set "MODEL=ggml-small.bin"
set "WLANG=zh"
set "THREADS=8"
rem ==================

if not exist "whisper-cli.exe" (
    echo [ERROR] whisper-cli.exe not found in this folder.
    pause & exit /b 1
)
if not exist "%MODEL%" (
    echo [ERROR] Model file not found: %MODEL%
    pause & exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [ERROR] ffmpeg not found in PATH.
    pause & exit /b 1
)

if not exist "input" mkdir "input"
if not exist "output" mkdir "output"
if not exist "temp" mkdir "temp"

set /a OK=0, SKIP=0, FAIL=0

for %%F in ("input\*.*") do call :process "%%F"

echo.
echo ===== Done: %OK% ok, %SKIP% skipped, %FAIL% failed =====
rmdir /s /q "temp" 2>nul
pause
exit /b 0

:process
set "SRC=%~1"
set "NAME=%~n1"

if exist "output\%NAME%.srt" (
    echo [SKIP] %~nx1 ^(srt already exists^)
    set /a SKIP+=1
    exit /b 0
)

echo.
echo [START] %~nx1
del /q "temp\work.*" 2>nul

ffmpeg -y -loglevel error -i "%SRC%" -vn -ar 16000 -ac 1 -c:a pcm_s16le "temp\work.wav"
if errorlevel 1 (
    echo [FAIL] ffmpeg could not convert: %~nx1
    set /a FAIL+=1
    exit /b 0
)

whisper-cli.exe -m "%MODEL%" -f "temp\work.wav" -l %WLANG% -t %THREADS% -osrt -of "temp\work"

if exist "temp\work.srt" (
    move /y "temp\work.srt" "output\%NAME%.srt" >nul
    if exist "%~dp0t2s.py" python "%~dp0t2s.py" "output\%NAME%.srt"
    echo [OK] output\%NAME%.srt
    set /a OK+=1
) else (
    echo [FAIL] no srt generated: %~nx1
    set /a FAIL+=1
)
del /q "temp\work.*" 2>nul
exit /b 0