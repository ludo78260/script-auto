@echo off
rem ==========================================================================================
rem  RemoteDeploy (version Python) 1.0.0 - installation en une seule fois
rem
rem  Fichiers texte uniquement (.py, .cmd, .md, .txt) : aucun binaire, rien a signaler
rem  pour l'antivirus (Trellix, Defender, ...).
rem
rem  Usage :
rem    Installer-RemoteDeploy-Python.cmd                     installation standard
rem    Installer-RemoteDeploy-Python.cmd D:\Mon\Dossier       dossier impose
rem    Installer-RemoteDeploy-Python.cmd D:\Dossier /silent   sans question
rem ==========================================================================================
setlocal EnableExtensions
title Installation de RemoteDeploy (Python)
cd /d "%~dp0"

set "APP_VERSION=1.0.0"
set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"

rem --- arguments ----------------------------------------------------------------------------
set "SILENT=0"
set "CIBLE="
if not "%~1"=="" (
    if /i "%~1"=="/silent" ( set "SILENT=1" ) else ( set "CIBLE=%~1" )
)
if not "%~2"=="" if /i "%~2"=="/silent" set "SILENT=1"

rem --- destination par defaut ----------------------------------------------------------------
if not defined CIBLE (
    if defined LOCALAPPDATA (
        set "CIBLE=%LOCALAPPDATA%\RemoteDeploy"
    ) else (
        set "CIBLE=%USERPROFILE%\RemoteDeploy"
    )
)
set "CIBLE=%CIBLE:"=%"
if "%CIBLE:~-1%"=="\" set "CIBLE=%CIBLE:~0,-1%"

rem --- horodatage -----------------------------------------------------------------------------
set "STAMP="
for /f "delims=" %%T in ('powershell.exe -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss" 2^>nul') do set "STAMP=%%T"
if not defined STAMP set "STAMP=%RANDOM%"
set "LOG="

echo.
echo  =============================================================================
echo   RemoteDeploy (version Python) %APP_VERSION% - installation
echo  =============================================================================
echo   Source      : %SRC%
echo   Destination : %CIBLE%
echo  =============================================================================
echo.

rem --- 1. fichiers sources ---------------------------------------------------------------------
set "MANQUANT=0"
for %%F in (RemoteDeploy.py Lancer-RemoteDeploy-Python.cmd Desinstaller-RemoteDeploy-Python.cmd) do (
    if not exist "%SRC%\%%F" set "MANQUANT=1"
)
if "%MANQUANT%"=="1" (
    echo   [ERREUR] Fichier(s) de livraison manquant(s) dans :
    echo            %SRC%
    echo.
    echo   Utilisez le dossier complet de livraison RemoteDeploy-Python.
    echo.
    pause
    exit /b 2
)
echo   [1/6] Fichiers de livraison verifies.

rem --- 2. Python 3 ------------------------------------------------------------------------------
set "PY="
set "PYVER="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PY=python"
    )
)
if not defined PY goto :sans_python
for /f "delims=" %%V in ('%PY% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%V"
echo   [2/6] Python detecte : version %PYVER%  ^(commande : %PY%^)
goto :python_ok

:sans_python
echo   [2/6] ATTENTION : Python 3 n'est pas detecte sur ce poste.
echo         L'application sera installee, mais elle ne pourra pas demarrer
echo         avant l'installation de Python 3 ^(3.8 minimum, avec tkinter^) :
echo           winget install Python.Python.3.12
echo           https://www.python.org/downloads/windows/
echo.
if "%SILENT%"=="0" (
    choice /c on /n /m "   Continuer l'installation quand meme [O/N] ? "
    if errorlevel 2 exit /b 3
)
goto :python_ok

:python_ok

rem --- 3. dossier de destination ------------------------------------------------------------------
mkdir "%CIBLE%" 2>nul
if exist "%CIBLE%" goto :dossier_pret
net session >nul 2>&1
if not errorlevel 1 goto :dossier_erreur
echo   [3/6] Droits insuffisants pour creer %CIBLE% : demande d'elevation ^(UAC^)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '%CIBLE%','/silent' -Verb RunAs"
exit /b

:dossier_erreur
echo   [ERREUR] Impossible de creer le dossier : %CIBLE%
echo.
pause
exit /b 4

:dossier_pret
echo   [3/6] Dossier de destination pret.

rem --- 4. copie des fichiers -------------------------------------------------------------------------
set "NCOPIER=0"
for %%F in (RemoteDeploy.py Lancer-RemoteDeploy-Python.cmd Desinstaller-RemoteDeploy-Python.cmd) do (
    copy /y "%SRC%\%%F" "%CIBLE%\" >nul
    if errorlevel 1 set "NCOPIER=1"
)
for %%F in (Mode-Emploi-Python.md LISEZ-MOI.txt postes-exemple.txt Exemple-Script.ps1) do (
    if exist "%SRC%\%%F" copy /y "%SRC%\%%F" "%CIBLE%\" >nul
)
if "%NCOPIER%"=="1" (
    echo   [ERREUR] La copie des fichiers a echoue (droits ou antivirus).
    echo.
    pause
    exit /b 5
)
for /f "delims=" %%H in ('certutil -hashfile "%CIBLE%\RemoteDeploy.py" SHA256 2^>nul ^| findstr /v /i "hash CertUtil"') do set "SHA=%%H"
echo   [4/6] Application copiee. SHA256 de RemoteDeploy.py :
echo         %SHA%

rem --- 5. raccourcis ---------------------------------------------------------------------------------
set "LNK_DESK="
set "LNK_MENU="
for /f "delims=" %%D in ('powershell.exe -NoProfile -Command "[Environment]::GetFolderPath('Desktop')" 2^>nul') do set "LNK_DESK=%%D\RemoteDeploy (Python).lnk"
for /f "delims=" %%D in ('powershell.exe -NoProfile -Command "[Environment]::GetFolderPath('Programs')" 2^>nul') do set "LNK_MENU=%%D\RemoteDeploy (Python).lnk"

if defined LNK_DESK powershell.exe -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK_DESK%'); $s.TargetPath='%CIBLE%\Lancer-RemoteDeploy-Python.cmd'; $s.WorkingDirectory='%CIBLE%'; $s.Description='RemoteDeploy (Python) - deploiement de scripts sur postes distants'; $s.Save()" >nul 2>&1
if defined LNK_MENU powershell.exe -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK_MENU%'); $s.TargetPath='%CIBLE%\Lancer-RemoteDeploy-Python.cmd'; $s.WorkingDirectory='%CIBLE%'; $s.Description='RemoteDeploy (Python) - deploiement de scripts sur postes distants'; $s.Save()" >nul 2>&1

set "LNK_OK=non"
if exist "%LNK_DESK%" set "LNK_OK=oui"
echo   [5/6] Raccourci Bureau : %LNK_OK%  ^(menu Demarrer : %LNK_MENU%^)

rem --- 6. registre -----------------------------------------------------------------------------------
set "CLE=HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\RemoteDeploy-Python"
reg add "HKCU\Software\RemoteDeploy" /v InstallPath /t REG_SZ /d "%CIBLE%" /f >nul 2>&1
reg add "HKCU\Software\RemoteDeploy" /v Version /t REG_SZ /d "%APP_VERSION%" /f >nul 2>&1
reg add "HKCU\Software\RemoteDeploy" /v Langage /t REG_SZ /d "Python" /f >nul 2>&1
reg add "%CLE%" /v DisplayName /t REG_SZ /d "RemoteDeploy (Python) %APP_VERSION%" /f >nul 2>&1
reg add "%CLE%" /v DisplayVersion /t REG_SZ /d "%APP_VERSION%" /f >nul 2>&1
reg add "%CLE%" /v Publisher /t REG_SZ /d "Outil interne" /f >nul 2>&1
reg add "%CLE%" /v InstallLocation /t REG_SZ /d "%CIBLE%" /f >nul 2>&1
reg add "%CLE%" /v UninstallString /t REG_SZ /d "\"%CIBLE%\Desinstaller-RemoteDeploy-Python.cmd\"" /f >nul 2>&1
reg add "%CLE%" /v NoModify /t REG_DWORD /d 1 /f >nul 2>&1
reg add "%CLE%" /v NoRepair /t REG_DWORD /d 1 /f >nul 2>&1
echo   [6/6] Entree de desinstallation enregistree ^(Applications et fonctionnalites^).

rem --- journal -----------------------------------------------------------------------------------------
set "LOG=%CIBLE%\installer-%STAMP%.log"
echo RemoteDeploy (Python) %APP_VERSION% - installation du %DATE% %TIME% > "%LOG%"
echo Source      : %SRC% >> "%LOG%"
echo Destination : %CIBLE% >> "%LOG%"
echo Python      : %PYVER% ^(%PY%^) >> "%LOG%"
echo SHA256      : %SHA% >> "%LOG%"
echo Raccourci   : %LNK_DESK% >> "%LOG%"
echo Fichiers    : >> "%LOG%"
dir /b "%CIBLE%" >> "%LOG%" 2>&1

echo.
echo  =============================================================================
echo   Installation terminee.
echo  =============================================================================
echo    Application   : %CIBLE%\RemoteDeploy.py
echo    Lanceur       : %CIBLE%\Lancer-RemoteDeploy-Python.cmd
echo    Mode d'emploi : %CIBLE%\Mode-Emploi-Python.md
echo    Journal       : %LOG%
echo.
echo    Aucun binaire non signe n'est installe : rien a signaler pour l'antivirus.
echo  =============================================================================
echo.

if "%SILENT%"=="1" exit /b 0

choice /c on /n /m "   Lancer RemoteDeploy maintenant [O/N] ? "
if errorlevel 2 exit /b 0
start "" "%CIBLE%\Lancer-RemoteDeploy-Python.cmd"
exit /b 0
