@echo off
rem ==========================================================================================
rem  OPTIONNEL - fabrication d'un executable a partir de RemoteDeploy.py (PyInstaller)
rem
rem  ATTENTION : la version livree (script .py + lanceur .cmd) ne contient AUCUN binaire et
rem  ne declenche rien cote antivirus. Un executable PyInstaller, lui, est un binaire non
rem  signe qui emballe l'interpreteur Python : c'est exactement le profil que les EDR
rem  heuristiques (Trellix, etc.) classent volontiers en menace. Ne l'utilisez que si un
rem  executable est indispensable, et faites valider une exclusion ciblee avant.
rem
rem  Usage : Optionnel-Creer-EXE-PyInstaller.cmd
rem ==========================================================================================
setlocal EnableExtensions
title RemoteDeploy - fabrication d'un executable (optionnel)
cd /d "%~dp0"

if not exist "%~dp0RemoteDeploy.py" (
    echo   [ERREUR] RemoteDeploy.py est introuvable dans ce dossier.
    pause
    exit /b 2
)

set "PY="
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
if not defined PY (
    echo   [ERREUR] Python 3 est introuvable.
    pause
    exit /b 3
)

echo.
echo  =============================================================================
echo   Fabrication d'un executable RemoteDeploy.exe (PyInstaller)
echo  =============================================================================
echo   Rappel : binaire NON SIGNE - risque d'alerte antivirus/EDR.
echo             La version .py + .cmd livree par defaut est sans binaire.
echo   =============================================================================
echo.
choice /c on /n /m "   Continuer [O/N] ? "
if errorlevel 2 exit /b 0

%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo   PyInstaller n'est pas installe. Installation ^(pip^)...
    %PY% -m pip install --user pyinstaller
    if errorlevel 1 (
        echo   [ERREUR] Installation de PyInstaller impossible. Verifiez l'acces a Internet
        echo            ou installez-le manuellement : %PY% -m pip install pyinstaller
        pause
        exit /b 4
    )
)

rem --- le mode "onedir" (dossier) est un peu moins suspect que "onefile" pour les EDR
set "MODE=--onedir"
choice /c on /n /m "   Executable en un seul fichier ? [O = onefile / N = dossier (onedir)] "
if errorlevel 2 goto :mode_pret
set "MODE=--onefile"
:mode_pret

echo.
echo   Compilation en cours (%MODE%)...
%PY% -m PyInstaller --noconfirm --clean --windowed %MODE% --name RemoteDeploy "%~dp0RemoteDeploy.py"
if errorlevel 1 (
    echo.
    echo   [ERREUR] La compilation a echoue. Voir le journal ci-dessus.
    pause
    exit /b 5
)

echo.
echo  =============================================================================
echo   Termine : voir le dossier dist\ dans ce dossier.
echo.
echo   Pensez a faire valider une exclusion ciblee aupres de l'equipe securite
echo   AVANT de deployer l'executable sur d'autres postes.
echo  =============================================================================
echo.
pause
exit /b 0
