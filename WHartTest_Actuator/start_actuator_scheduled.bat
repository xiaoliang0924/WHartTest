@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py --no-gui --config config.toml >> "data\actuator.out.log" 2>> "data\actuator.err.log"
