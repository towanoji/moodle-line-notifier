@echo off
cd /d "%~dp0"
del /f ".git\index.lock" 2>nul
git add -A
git commit -m "Fix: web login for SSO"
git push --force origin main
pause
