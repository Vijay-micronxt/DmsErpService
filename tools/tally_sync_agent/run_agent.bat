@echo off
rem Starts the DMS -> Tally sync agent with config.ini from this folder.
cd /d "%~dp0"
python agent.py --config config.ini
pause
