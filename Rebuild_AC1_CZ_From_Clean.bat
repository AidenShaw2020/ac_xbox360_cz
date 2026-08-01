@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

set "PYTHON=python"
where "%PYTHON%" >nul 2>nul
if errorlevel 1 (
  echo CHYBA: Python nebyl nalezen v PATH.
  echo Nainstalujte Python 3.11 nebo novejsi a zkuste to znovu.
  exit /b 2
)

if "%~1"=="" goto interactive
if "%~5"=="" goto usage

set "XBOX_PATH=%~1"
set "PC_PATH=%~2"
set "OUTPUT_PATH=%~3"
set "FFMPEG_PATH=%~4"
set "XMA_PATH=%~5"
set "EXTRA_ARG=%~6"
goto run

:interactive
echo Assassin's Creed Xbox 360 CZ - kompletni sestaveni z cistych souboru
echo.
set /p "XBOX_PATH=Cesta ke koreni ciste Xbox 360 hry: "
set /p "PC_PATH=Cesta k ceske PC instalaci: "
set /p "OUTPUT_PATH=Vystupni adresar: "
set /p "FFMPEG_PATH=Cesta k ffmpeg.exe: "
set /p "XMA_PATH=Cesta k xma2encode.exe: "
set "EXTRA_ARG="

:run
echo.
"%PYTHON%" "%~dp0tools\ac1_rebuild_from_clean.py" ^
  --xbox-dir "%XBOX_PATH%" ^
  --pc-dir "%PC_PATH%" ^
  --output "%OUTPUT_PATH%" ^
  --ffmpeg "%FFMPEG_PATH%" ^
  --xma2encode "%XMA_PATH%" ^
  %EXTRA_ARG%
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" (
  echo Sestaveni selhalo. Podrobnosti jsou ve vystupnim adresari _build.
) else (
  if /I "%EXTRA_ARG%"=="--plan-only" (
    echo Kontrola vstupu probehla uspesne. Zadna herni data nebyla zmenena.
  ) else (
    echo Hotovo. Soubory pro Xbox jsou ve slozce Ready_To_Copy.
  )
)
exit /b %RESULT%

:usage
echo Pouziti:
echo   %~nx0 "XBOX_CISTA_HRA" "PC_CZ_HRA" "VYSTUP" "ffmpeg.exe" "xma2encode.exe" [--plan-only ^| --resume]
echo.
echo Bez parametru se spusti interaktivni pruvodce.
exit /b 2
