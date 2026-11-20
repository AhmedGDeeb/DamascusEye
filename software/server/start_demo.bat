@echo off
rem Damascus Eye - start everything for a demo.
rem   1. Dashboard server   http://127.0.0.1:8000/
rem   2. ESP bridge         images from the ESP32-CAM WiFi (EdgeNode-CAM) -> dashboard (live, no reload)
rem   3. YOLO manager       objects + automatic sidewalk (purple) -> on / off the sidewalk
rem                         needs yolo_manager\.venv: run setup_yolo.bat once, with internet
rem Connect this computer to the EdgeNode-CAM WiFi before or after starting; the bridge keeps retrying.

setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%bin\python.exe"
set "YOLO_PY=%ROOT%yolo_manager\.venv\Scripts\python.exe"
set "NODE=esp32cam-01"
set "URL=http://127.0.0.1:8000/"
rem Sidewalk detection without training: yoloe (default), sem (YOLO26-sem, Cityscapes) or off.
set "SIDEWALK=yoloe"

if not exist "%PY%" (
    echo [!] Python environment not found: %PY%
    pause
    exit /b 1
)

echo [1/5] Preparing the database...
"%PY%" "%ROOT%backend\manage.py" migrate --noinput -v 0 || goto :failed
"%PY%" "%ROOT%backend\manage.py" register_node %NODE% --name "ESP32-S3 CAM"
if errorlevel 1 (
    echo [!] Could not register node %NODE% automatically - continuing.
    echo     Copy backend\monitoring\management\ from the original computer,
    echo     or add the node in the dashboard: Nodes, Add node, code %NODE%.
)

echo [2/5] Compiling the Arabic translation...
"%PY%" "%ROOT%backend\manage.py" compile_translations >nul
if errorlevel 1 echo [!] Translation not compiled - the dashboard may show English text.

echo [3/5] Starting the dashboard server...
start "Damascus Eye - Server" /d "%ROOT%backend" cmd /k ""%PY%" manage.py runserver 127.0.0.1:8000"

rem Wait up to 30 s for the server to answer.
set /a TRIES=0
:wait_server
curl -s -o nul "%URL%login/" && goto :server_up
set /a TRIES+=1
if %TRIES% geq 30 (
    echo [!] The server did not start. Check the "Damascus Eye - Server" window.
    pause
    exit /b 1
)
timeout /t 1 /nobreak >nul
goto :wait_server
:server_up

echo [4/5] Starting the ESP bridge and the YOLO manager...
start "Damascus Eye - ESP Bridge" /d "%ROOT%gateway_bridge" cmd /k ""%PY%" esp_bridge.py --node %NODE%"

if exist "%YOLO_PY%" (
    start "Damascus Eye - YOLO" /d "%ROOT%yolo_manager" cmd /k ""%YOLO_PY%" manager.py run --sidewalk %SIDEWALK%"
    echo        YOLO manager started ^(sidewalk detection: %SIDEWALK%^).
) else (
    echo [!] YOLO manager skipped: run setup_yolo.bat once ^(needs internet^), then start again.
)

echo [5/5] Opening the dashboard...
start "" "%URL%captures/?node=%NODE%"

echo.
echo Running. Close the "Damascus Eye" windows to stop.
timeout /t 5 >nul
exit /b 0

:failed
echo [!] Setup failed, see the messages above.
pause
exit /b 1
