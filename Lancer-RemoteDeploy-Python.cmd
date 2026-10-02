@echo off
rem ==========================================================================================
rem  RemoteDeploy (version Python) 1.0.0 - lanceur de l'application
rem
rem  Fichier texte : aucun binaire, donc rien a signaler pour l'antivirus.
rem  Double-cliquez sur ce fichier : les droits administrateur sont demandes automatiquement.
rem
rem  Cas d'usage particuliers (ligne de commande) :
rem    Lancer-RemoteDeploy-Python.cmd                 -> interface graphique (sans console)
rem    Lancer-RemoteDeploy-Python.cmd console         -> avec console (diagnostic)
rem    python RemoteDeploy.py --selftest              -> auto-test en ligne de commande
rem ==========================================================================================
setlocal EnableExtensions
title RemoteDeploy (Python)
cd /d "%~dp0"

rem --- 1. elevation des droits (UAC) --------------------------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  RemoteDeploy : demande d'elevation des droits ^(UAC^)...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    if errorlevel 1 echo  Elevation refusee : lancement sans droits administrateur.
    if not errorlevel 1 exit /b
)

rem --- 2. presence de l'application ---------------------------------------------------------
if not exist "%~dp0RemoteDeploy.py" (
    echo.
    echo  ERREUR : RemoteDeploy.py est introuvable dans ce dossier :
    echo    %~dp0
    echo.
    echo  Placez ce lanceur dans le meme dossier que RemoteDeploy.py,
    echo  ou relancez Installer-RemoteDeploy-Python.cmd.
    echo.
    pause
    exit /b 2
)

rem --- 3. recherche de Python 3 -------------------------------------------------------------
set "PY="
set "PYW="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PY=py -3"
        set "PYW=pyw -3"
    )
)
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PY=python"
    )
)
if not defined PY (
    echo.
    echo  ERREUR : Python 3 est introuvable sur ce poste.
    echo.
    echo  RemoteDeploy est un script Python : rien n'est a compiler, mais Python
    echo  doit etre installe ^(version 3.8 minimum^). Trois solutions :
    echo    - Microsoft Store : recherchez "Python 3"
    echo    - ligne de commande : winget install Python.Python.3.12
    echo    - python.org : https://www.python.org/downloads/windows/
    echo      ^(cochez "Add python.exe to PATH" pendant l'installation^)
    echo.
    pause
    exit /b 3
)

rem --- 4. module tkinter (interface graphique) ----------------------------------------------
%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERREUR : le module tkinter est absent de votre installation Python.
    echo  Relancez l'installateur Python en cochant l'option "tcl/tk and IDLE".
    echo.
    pause
    exit /b 4
)

rem --- 5. mode console (diagnostic) ---------------------------------------------------------
if /i "%~1"=="console" set "PYW="
if /i "%~1"=="--console" set "PYW="

rem --- 6. lancement -------------------------------------------------------------------------
if defined PYW goto :lancement
set "PYDIR="
for /f "delims=" %%P in ('%PY% -c "import os,sys;print(os.path.dirname(sys.executable))"') do set "PYDIR=%%P"
if not defined PYDIR goto :lancement
if exist "%PYDIR%\pythonw.exe" set "PYW=%PYDIR%\pythonw.exe"

:lancement

echo  Lancement de RemoteDeploy...
if defined PYW (
    start "" %PYW% "%~dp0RemoteDeploy.py"
    exit /b 0
)

%PY% "%~dp0RemoteDeploy.py"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo  RemoteDeploy s'est termine avec le code %RC%.
    echo  Relancez avec : Lancer-RemoteDeploy-Python.cmd console
    echo.
    pause
)
endlocal
