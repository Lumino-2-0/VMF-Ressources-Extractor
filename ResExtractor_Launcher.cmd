cls
@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title VMF Resource Extractor - Launcher
color 0B

echo ================================================================
echo    VMF RESOURCE EXTRACTOR - LAUNCHER
echo    GMod / Source SDK 2013
echo ================================================================
echo.

:: ---------------------------------------------------------------
:: 1. Detection de Python
:: ---------------------------------------------------------------
set "PYEXE="

where python >nul 2>nul
if %errorlevel%==0 (
    python -c "import sys; sys.exit(0 if sys.version_info[0]==3 else 1)" >nul 2>nul
    if !errorlevel!==0 set "PYEXE=python"
)

if not defined PYEXE (
    where py >nul 2>nul
    if !errorlevel!==0 set "PYEXE=py -3"
)

if not defined PYEXE (
    echo [ERREUR] Python 3 est introuvable dans le PATH.
    echo.
    echo Installez Python 3.8 ou superieur depuis :
    echo   https://www.python.org/downloads/
    echo Pensez a cocher "Add Python to PATH" pendant l'installation.
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%v in ('%PYEXE% --version 2^>^&1') do set "PYVER_STR=%%v"
echo [OK] Python detecte : %PYVER_STR%  (commande : %PYEXE%)

%PYEXE% -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul
if not !errorlevel!==0 (
    echo [ATTENTION] Python 3.8+ est recommande, une version plus ancienne
    echo             peut provoquer des erreurs inattendues.
    echo.
)

:: ---------------------------------------------------------------
:: 2. Verification des dependances
:: Le script n'utilise que la bibliotheque standard : aucun
:: "pip install" n'est necessaire. On verifie juste que
:: l'installation Python n'est pas cassee.
:: ---------------------------------------------------------------
%PYEXE% -c "import os,re,sys,time,shutil,argparse,struct,csv,threading,concurrent.futures,pathlib,collections" >nul 2>nul
if not !errorlevel!==0 (
    echo [ERREUR] Des modules standards de Python sont manquants ou
    echo          votre installation Python est corrompue.
    pause
    exit /b 1
)
echo [OK] Dependances : bibliotheque standard uniquement, rien a installer.
echo.

:: ---------------------------------------------------------------
:: 3. Localisation du script principal
:: ---------------------------------------------------------------
set "SCRIPT_DIR=%~dp0"
set "EXTRACTOR=%SCRIPT_DIR%VMF_ResExtractor.py"

if not exist "%EXTRACTOR%" (
    echo [ERREUR] VMF_ResExtractor.py est introuvable a cote de ce launcher.
    echo          Attendu : %EXTRACTOR%
    echo.
    pause
    exit /b 1
)

:: ---------------------------------------------------------------
:: 4. Questions interactives
:: ---------------------------------------------------------------
set "VMF_PATH=%~1"
if not defined VMF_PATH (
    set /p "VMF_PATH=Chemin du fichier .vmf : "
)
if not exist "!VMF_PATH!" (
    echo [ERREUR] Fichier introuvable : !VMF_PATH!
    pause
    exit /b 1
)

set /p "GAME_SRC=Dossier garrysmod (ex: C:\...\Steam\steamapps\common\GarrysMod\garrysmod) : "
if not exist "!GAME_SRC!" (
    echo [ERREUR] Dossier introuvable : !GAME_SRC!
    pause
    exit /b 1
)

set "DEST_PATH="
set /p "DEST_PATH=Dossier de sortie (cree automatiquement) [.\custom] : "
if not defined DEST_PATH set "DEST_PATH=.\custom"

set "LANG_CHOICE="
set /p "LANG_CHOICE=Langue [FR/EN] (defaut FR) : "
if not defined LANG_CHOICE set "LANG_CHOICE=FR"

set "THREADS="
set /p "THREADS=Nombre de threads (defaut 4) : "
if not defined THREADS set "THREADS=4"

set "SHOW_MISSING="
set /p "SHOW_MISSING=Afficher le detail des ressources manquantes ? [O/N] (defaut O) : "
if not defined SHOW_MISSING set "SHOW_MISSING=O"

set "CSV_PATH="
set /p "CSV_PATH=Chemin d'un rapport CSV a generer (vide = aucun) : "

set "EXTRA_ARGS="
if /i "!SHOW_MISSING!"=="O" set "EXTRA_ARGS=!EXTRA_ARGS! -missing"
if /i "!SHOW_MISSING!"=="Y" set "EXTRA_ARGS=!EXTRA_ARGS! -missing"
if defined CSV_PATH set "EXTRA_ARGS=!EXTRA_ARGS! -csv "!CSV_PATH!""

:: ---------------------------------------------------------------
:: 5. Lancement
:: ---------------------------------------------------------------
echo.
echo ================================================================
echo   Lancement de l'extraction...
echo ================================================================
echo.

%PYEXE% "%EXTRACTOR%" -source "!VMF_PATH!" -gameSrc "!GAME_SRC!" -dest "!DEST_PATH!" -Threads !THREADS! -Lang !LANG_CHOICE! !EXTRA_ARGS!

echo.
echo ================================================================
echo   Termine.
echo ================================================================
pause
endlocal
