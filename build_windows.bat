@echo off
setlocal
cd /d "%~dp0"

py -3.12 make_icon.py
if errorlevel 1 exit /b 1

py -3.12 -m PyInstaller --noconfirm --clean --onefile --windowed --name Plasmora --icon app-icon.ico --collect-all webview --add-data "index.html;." --add-data "app.js;." --add-data "enhancements.js;." --add-data "styles.css;." --add-data "compat.css;." --add-data "themes.css;." --add-data "app-icon.png;." desktop.py
if errorlevel 1 exit /b 1

set "ISCC="
if exist "%LocalAppData%\Programs\Inno Setup 7\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo Inno Setup 6 or 7 was not found. The application EXE is ready in dist\Plasmora.exe.
  exit /b 2
)

"%ISCC%" installer.iss
if errorlevel 1 exit /b 1
echo Installer created in release\.
