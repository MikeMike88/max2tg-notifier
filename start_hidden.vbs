' Скрытый запуск Нотификатора MAX -> Telegram (без всякого окна, значок в трее).
' Этот .vbs не показывает даже мелькающего окна cmd, в отличие от .bat.
' Положи ярлык на него в shell:startup, если нужен автозапуск при входе в Windows.
' pythonw должен быть в PATH (иначе впиши полный путь к нему).
Dim sh, fso, here
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
' 0 = окно скрыто, False = не ждать завершения.
sh.Run "pythonw """ & here & "\main.py""", 0, False
