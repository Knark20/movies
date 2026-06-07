@echo off
call keys.bat
cd /d "%~dp0"
C:\Users\31611\AppData\Local\Programs\Python\Python311\python.exe fetch_movies.py
if %ERRORLEVEL% == 0 (
    start movies.html
)
pause
