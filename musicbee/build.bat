@echo off
setlocal
set CSC=C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe
set OUT=%APPDATA%\MusicBee\Plugins\mb_ListenBrainzSync.dll

"%CSC%" /nologo /target:library /platform:x86 /optimize+ ^
  /out:"%OUT%" ^
  /r:System.dll ^
  /r:System.Net.Http.dll ^
  /r:System.Windows.Forms.dll ^
  /r:System.Web.Extensions.dll ^
  "%~dp0MusicBeeInterface.cs" "%~dp0ListenBrainzSync.cs"

if errorlevel 1 exit /b 1
echo Built %OUT%
