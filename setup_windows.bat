@echo off
cd /d "%~dp0"
echo Installing/updating Map Extractor dependencies...
python -m pip install -U -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency installation failed. If your proxy intercepts HTTPs,
    echo configure pip to trust your CA and run this script again.
    pause
    exit /b 1
)
python -c "import curl_cffi; print('curl_cffi', curl_cffi.__version__, 'installed')"
pause
