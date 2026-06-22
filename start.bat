@echo off
rem Запуск Нотификатора MAX -> Telegram скрытно, со значком в системном трее.
rem pythonw.exe = без консольного окна. Управление и выход — через значок в трее.
rem Первый вход / ввод SMS-кода и 2FA делается вручную с консолью:  python main.py
rem pythonw должен быть в PATH (или впиши полный путь к нему ниже).
rem Для запуска совсем без мелькания окна используй start_hidden.vbs.
cd /d "%~dp0"
start "" pythonw main.py
