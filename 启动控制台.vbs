' 双击打开 DD轨迹互动记录（无黑框，自动打开浏览器）
Option Explicit
Dim sh, fso, base
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base
sh.Run """" & base & "\DD轨迹.exe"" --ui --no-browser", 0, False
