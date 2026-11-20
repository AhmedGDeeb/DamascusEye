@echo off
rem Damascus Eye - one-time setup of the YOLO manager (needs internet, about 1-2 GB).
rem Creates yolo_manager\.venv with the official Python from python.org (PyTorch does not
rem support the Inkscape/MinGW Python used by the dashboard), installs ultralytics and
rem downloads every model the demo uses, so start_demo.bat then works offline.

setlocal
set "ROOT=%~dp0"
set "VENV=%ROOT%yolo_manager\.venv"
set "YOLO_PY=%VENV%\Scripts\python.exe"

if exist "%YOLO_PY%" goto :install

echo [1/3] Looking for the official Python (3.12, 3.13 or 3.14)...
set "BASE="
for %%V in (3.12 3.13 3.14) do (
    if not defined BASE (
        py -%%V -c "import sysconfig, sys; sys.exit(sysconfig.get_platform() != 'win-amd64')" >nul 2>&1 && set "BASE=py -%%V"
    )
)
if not defined BASE (
    echo [!] No official 64-bit Python found. Install Python 3.12 from https://www.python.org/downloads/
    echo     ^(tick "Add python.exe to PATH" and the "py launcher"^), then run this file again.
    pause
    exit /b 1
)
echo        Using: %BASE%
%BASE% -m venv "%VENV%" || goto :failed

:install
echo [2/3] Installing PyTorch ^(CPU^) and ultralytics...
"%YOLO_PY%" -m pip install --upgrade pip -q || goto :failed
"%YOLO_PY%" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu -q || goto :failed
"%YOLO_PY%" -m pip install -r "%ROOT%yolo_manager\requirements.txt" -q || goto :failed

echo [3/3] Downloading the models ^(objects, YOLOE sidewalk + text encoder, Cityscapes sidewalk^)...
pushd "%ROOT%yolo_manager"
"%YOLO_PY%" -c "from ultralytics import YOLO; YOLO('yolo26n.pt'); YOLO('yolo26n-sem.pt'); m = YOLO('yoloe-26s-seg.pt'); m.set_classes(['sidewalk', 'pavement']); print('Models ready.')"
set "RESULT=%ERRORLEVEL%"
popd
if not "%RESULT%"=="0" goto :failed

echo.
echo YOLO manager ready. Run start_demo.bat - it now opens the YOLO window too.
pause
exit /b 0

:failed
echo [!] Setup failed, see the messages above. Check the internet connection and run this file again.
pause
exit /b 1
