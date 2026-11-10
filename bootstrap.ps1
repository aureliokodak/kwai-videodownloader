$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$RuntimeRoot = Join-Path $Root 'runtime'
$PythonDir = Join-Path $RuntimeRoot 'python'
$PythonExe = Join-Path $PythonDir 'python.exe'
$ReadyMarker = Join-Path $PythonDir '.kwai-portable-ready-v1'
$InstallLog = Join-Path $Root 'portable-install.log'
$StartupLog = Join-Path $Root 'app-startup.log'
$PythonVersion = '3.12.10'
$PythonZipUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$GetPipUrl = 'https://bootstrap.pypa.io/get-pip.py'

function Write-Step([string]$Text) {
    Write-Host "[Kwai Analyzer] $Text" -ForegroundColor Cyan
    Add-Content -Path $InstallLog -Value "$(Get-Date -Format s) $Text"
}

function Fail([string]$Message) {
    Write-Host ''
    Write-Host "ERRO: $Message" -ForegroundColor Red
    Write-Host "Log de instalação: $InstallLog" -ForegroundColor Yellow
    Write-Host "Log do aplicativo:  $StartupLog" -ForegroundColor Yellow
    Write-Host ''
    Read-Host 'Pressione ENTER para fechar'
    exit 1
}

try {
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null

    if (-not (Test-Path $ReadyMarker)) {
        Write-Step 'Primeira execução: preparando o runtime portátil. Não é necessário instalar Python no Windows.'
        Write-Step 'Isso requer internet e pode levar alguns minutos.'

        $TempDir = Join-Path $RuntimeRoot '_setup'
        if (Test-Path $TempDir) { Remove-Item -Recurse -Force $TempDir }
        New-Item -ItemType Directory -Force -Path $TempDir | Out-Null

        $PythonZip = Join-Path $TempDir 'python-embed.zip'
        Write-Step "Baixando Python portátil $PythonVersion..."
        Invoke-WebRequest -UseBasicParsing -Uri $PythonZipUrl -OutFile $PythonZip

        if (Test-Path $PythonDir) { Remove-Item -Recurse -Force $PythonDir }
        New-Item -ItemType Directory -Force -Path $PythonDir | Out-Null
        Write-Step 'Extraindo runtime...'
        Expand-Archive -Path $PythonZip -DestinationPath $PythonDir -Force

        $PthFile = Join-Path $PythonDir 'python312._pth'
        @(
            'python312.zip',
            '.',
            '..\..',
            'Lib\site-packages',
            'import site'
        ) | Set-Content -Path $PthFile -Encoding ASCII

        New-Item -ItemType Directory -Force -Path (Join-Path $PythonDir 'Lib\site-packages') | Out-Null

        $GetPip = Join-Path $TempDir 'get-pip.py'
        Write-Step 'Instalando o gerenciador de dependências no runtime portátil...'
        Invoke-WebRequest -UseBasicParsing -Uri $GetPipUrl -OutFile $GetPip
        & $PythonExe $GetPip --disable-pip-version-check --no-warn-script-location
        if ($LASTEXITCODE -ne 0) { throw "get-pip retornou código $LASTEXITCODE" }

        Write-Step 'Instalando FastAPI, Playwright e dependências...'
        & $PythonExe -m pip install --disable-pip-version-check --no-warn-script-location -r (Join-Path $Root 'requirements-portable.txt')
        if ($LASTEXITCODE -ne 0) { throw "pip retornou código $LASTEXITCODE" }

        New-Item -ItemType File -Force -Path $ReadyMarker | Out-Null
        if (Test-Path $TempDir) { Remove-Item -Recurse -Force $TempDir }
        Write-Step 'Runtime portátil concluído.'
    }

    if (-not (Test-Path $PythonExe)) {
        throw 'python.exe do runtime portátil não foi encontrado.'
    }

    $env:PYTHONUTF8 = '1'
    $env:PYTHONUNBUFFERED = '1'
    Write-Step 'Iniciando Kwai Analyzer v2.1...'
    Write-Host ''
    Write-Host 'Esta janela ficará aberta enquanto o Kwai Analyzer estiver rodando.' -ForegroundColor Green
    Write-Host 'Quando aparecer "Uvicorn running", o painel deve abrir automaticamente.' -ForegroundColor Green
    Write-Host 'Se não abrir, acesse manualmente: http://127.0.0.1:8000' -ForegroundColor Yellow
    Write-Host ''

    # Run synchronously and visibly. The old v1.0 used pythonw.exe, which hid
    # startup exceptions and made it look as if nothing happened.
    & $PythonExe (Join-Path $Root 'start.py')
    $Code = $LASTEXITCODE
    if ($Code -ne 0) {
        throw "O aplicativo encerrou com código $Code. Consulte app-startup.log."
    }
}
catch {
    Add-Content -Path $InstallLog -Value "$(Get-Date -Format s) ERROR $($_.Exception.ToString())"
    Fail $_.Exception.Message
}
