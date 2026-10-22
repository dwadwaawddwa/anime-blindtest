@echo off
rem Envoie le code du blind test sur le Pi et redemarre le service (blindtest.dwa.gold).
rem Le .env, le venv, la base blindtest.db et le cache audio restent sur le Pi, intacts.
cd /d "%~dp0"
set PI=pi@192.168.1.73
tar czf "%TEMP%\anime-blindtest.tgz" --exclude=__pycache__ app static templates requirements.txt || goto :err
scp "%TEMP%\anime-blindtest.tgz" %PI%:/tmp/anime-blindtest.tgz || goto :err
ssh %PI% "mkdir -p ~/anime-blindtest && tar xzf /tmp/anime-blindtest.tgz -C ~/anime-blindtest && rm /tmp/anime-blindtest.tgz && cd ~/anime-blindtest && venv/bin/pip install -q -r requirements.txt && sudo systemctl restart anime-blindtest && sleep 3 && systemctl is-active anime-blindtest" || goto :err
echo.
echo OK : https://blindtest.dwa.gold   (attention : les salons en cours sont fermes par le redemarrage)
pause
exit /b 0
:err
echo.
echo ECHEC de la mise a jour (Pi joignable ? ssh %PI%)
pause
exit /b 1
