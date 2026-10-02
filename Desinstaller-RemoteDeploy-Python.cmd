@echo off
rem ==========================================================================================
rem  RemoteDeploy (version Python) 1.0.0 - desinstallation
rem
rem  Usage :
rem    Desinstaller-RemoteDeploy-Python.cmd             retire l'application et ses raccourcis
rem    Desinstaller-RemoteDeploy-Python.cmd /detached D:\Dossier [/silent]   (usage interne)
rem ==========================================================================================
setlocal EnableExtensions
title Desinstallation de RemoteDeploy (Python)

if /i "%~1"=="/detached" goto :detache

set "CIBLE=%~dp0"
if "%CIBLE:~-1%"=="\" set "CIBLE=%CIBLE:~0,-1%"
set "SILENT=0"
if /i "%~2"=="/silent" set "SILENT=1"
if /i "%~1"=="/silent" set "SILENT=1"

echo.
echo  =============================================================================
echo   Desinstallation de RemoteDeploy (version Python)
echo  =============================================================================
echo    Dossier   : %CIBLE%
echo    Supprime  : l'application, les raccourcis, l'entree de desinstallation
echo                et la configuration memorisee (identifiants inclus).
echo  =============================================================================
echo.

if "%SILENT%"=="1" goto :confirme
choice /c on /n /m "   Confirmer la desinstallation [O/N] ? "
if errorlevel 2 exit /b 0
:confirme

rem --- le script se recopie dans TEMP : un fichier .cmd ne peut pas se supprimer lui-meme
set "TEMP_DIR=%TEMP%"
if not defined TEMP_DIR set "TEMP_DIR=%USERPROFILE%"
copy /y "%~f0" "%TEMP_DIR%\_rd_desinstalleur.cmd" >nul 2>&1
if errorlevel 1 (
    echo   [ERREUR] Impossible de poursuivre : dossier temporaire inaccessible.
    echo.
    pause
    exit /b 2
)
start "" "%TEMP_DIR%\_rd_desinstalleur.cmd" /detached "%CIBLE%" /silent
exit /b 0

rem ==========================================================================================
:detache
set "CIBLE=%~2"
timeout /t 1 /nobreak >nul 2>&1

rem --- raccourcis -----------------------------------------------------------------------------
for /f "delims=" %%D in ('powershell.exe -NoProfile -Command "[Environment]::GetFolderPath('Desktop')" 2^>nul') do (
    if exist "%%D\RemoteDeploy (Python).lnk" del /f /q "%%D\RemoteDeploy (Python).lnk" >nul 2>&1
)
for /f "delims=" %%D in ('powershell.exe -NoProfile -Command "[Environment]::GetFolderPath('Programs')" 2^>nul') do (
    if exist "%%D\RemoteDeploy (Python).lnk" del /f /q "%%D\RemoteDeploy (Python).lnk" >nul 2>&1
)

rem --- registre ----------------------------------------------------------------------------------
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\RemoteDeploy-Python" /f >nul 2>&1
reg delete "HKCU\Software\RemoteDeploy" /f >nul 2>&1

rem --- fichiers ------------------------------------------------------------------------------------
if exist "%CIBLE%\RemoteDeploy.py" del /f /q "%CIBLE%\RemoteDeploy.py" >nul 2>&1
for %%F in (Lancer-RemoteDeploy-Python.cmd Desinstaller-RemoteDeploy-Python.cmd Mode-Emploi-Python.md LISEZ-MOI.txt postes-exemple.txt Exemple-Script.ps1 remotedeploy.config.json remotedeploy.credentials.dat) do (
    if exist "%CIBLE%\%%F" del /f /q "%CIBLE%\%%F" >nul 2>&1
)
for /d %%D in ("%CIBLE%\*") do rd /s /q "%%D" >nul 2>&1
rd /s /q "%CIBLE%" >nul 2>&1

if exist "%CIBLE%" (
    echo   [ATTENTION] Certains fichiers n'ont pas pu etre supprimes dans :
    echo               %CIBLE%
    echo   Fermez RemoteDeploy puis relancez la desinstallation.
) else (
    echo   Desinstallation terminee : %CIBLE% a ete supprime.
)
del /f /q "%~f0" >nul 2>&1
timeout /t 5 /nobreak >nul 2>&1
exit /b 0
