@echo off
cd /d "%~dp0"
runtime\python\python.exe -m pip install --upgrade "yt-dlp[default]" --index-url https://pypi.org/simple
if errorlevel 1 (
  echo Falha ao atualizar. Verifique a conexao com a internet.
) else (
  echo Componente YouTube atualizado. Reinicie o Kwai Analyzer.
)
pause
