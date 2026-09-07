Option Explicit

Dim fso, shell, root, powershell, cleanup, pythonw, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)

' Stop known legacy Web/Terminal launchers before claiming the App port.
' The PowerShell window is hidden and the VBS waits for cleanup to finish;
' this is a launcher step, not a user-facing CMD or a click-to-close prompt.
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
If Not fso.FileExists(powershell) Then powershell = "powershell.exe"
cleanup = Quote(powershell) & " -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Quote(root & "\backend\stop_legacy_instances.ps1")
If fso.FileExists(root & "\backend\stop_legacy_instances.ps1") Then
    On Error Resume Next
    shell.Run cleanup, 0, True
    On Error GoTo 0
End If

pythonw = FindPythonW(fso, shell, root)
If pythonw = "" Then
    MsgBox "Python was not found. Run windows install.bat first.", 16, "ancserTPX"
    WScript.Quit 1
End If

shell.CurrentDirectory = root
command = Quote(pythonw) & " -m backend.desktop_app"
'pythonw.exe has no console. Keep the process consoleless while allowing
'the WinForms/WebView2 window to be shown normally.
shell.Run command, 1, False

Function FindPythonW(file_system, command_shell, project_root)
    Dim candidate, local_app_data, version

    candidate = project_root & "\.venv\Scripts\pythonw.exe"
    If file_system.FileExists(candidate) Then
        FindPythonW = candidate
        Exit Function
    End If

    local_app_data = command_shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
    For Each version In Array("Python313", "Python312", "Python311", "Python310")
        candidate = local_app_data & "\Programs\Python\" & version & "\pythonw.exe"
        If file_system.FileExists(candidate) Then
            FindPythonW = candidate
            Exit Function
        End If
    Next

    FindPythonW = FindFromPath(file_system, command_shell, "pythonw.exe")
    If FindPythonW = "" Then
        candidate = FindFromPath(file_system, command_shell, "python.exe")
        If candidate <> "" Then
            candidate = file_system.GetParentFolderName(candidate) & "\pythonw.exe"
            If file_system.FileExists(candidate) Then FindPythonW = candidate
        End If
    End If
End Function

Function FindFromPath(file_system, command_shell, executable)
    Dim process, candidate
    On Error Resume Next
    Set process = command_shell.Exec("where " & executable)
    If Err.Number <> 0 Then
        Err.Clear
        FindFromPath = ""
        Exit Function
    End If
    On Error GoTo 0

    Do Until process.StdOut.AtEndOfStream
        candidate = Trim(process.StdOut.ReadLine)
        If file_system.FileExists(candidate) Then
            FindFromPath = candidate
            Exit Function
        End If
    Loop
    FindFromPath = ""
End Function

Function Quote(value)
    Quote = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
