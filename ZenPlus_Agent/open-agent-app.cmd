@echo off
setlocal
start "" "%~dp0dist\zenplus-agent-app.exe" --config "%~dp0config\agent.yaml"

