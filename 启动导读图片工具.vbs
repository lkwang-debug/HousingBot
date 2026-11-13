Option Explicit
Dim shell, files, root, python, entry
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
python = files.BuildPath(root, ".venv\Scripts\pythonw.exe")
entry = files.BuildPath(root, "marketing\share_tool.pyw")
If Not files.FileExists(python) Then
    MsgBox "Missing .venv\Scripts\pythonw.exe", 16, "HousingBot"
    WScript.Quit 1
End If
shell.CurrentDirectory = root
shell.Run Chr(34) & python & Chr(34) & " -X utf8 " & Chr(34) & entry & Chr(34), 0, False
