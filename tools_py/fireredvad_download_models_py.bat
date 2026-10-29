@echo off
setlocal
set "PROJECT_ROOT=%~dp0.."
pushd "%PROJECT_ROOT%" || exit /b 1
python "%~dp0fireredvad_models_py_downloader\fireredvad_download_models_py.py" --project-root "%PROJECT_ROOT%" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
