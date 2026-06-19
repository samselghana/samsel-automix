Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
batPath = FSO.BuildPath(scriptDir, "Start_Downloader_Service.bat")
WshShell.Run Chr(34) & batPath & Chr(34), 0, False
