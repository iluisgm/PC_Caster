' PC Caster — silent launcher. Runs the app with pythonw (no console window).
' Portable: works wherever this folder lives, so it survives folder renames.
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir  = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = appDir
sh.Run "pythonw.exe " & Chr(34) & appDir & "\pc_caster.py" & Chr(34), 0, False
