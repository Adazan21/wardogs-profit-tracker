; installer.iss — Inno Setup script that packages dist\profitdog.exe (already
; a fully self-contained PyInstaller build — steam_api64.dll and everything
; else are baked into that one file) into a proper Windows setup.exe: pick
; an install folder, Start Menu/Desktop shortcuts, an uninstaller entry in
; Add/Remove Programs. Run `pyinstaller profitdog.spec` first so dist\
; profitdog.exe exists, then compile this with ISCC (bundled on GitHub's
; windows-latest runners, or install Inno Setup locally — innosetup.org).
;
; Installs per-user, no admin/UAC needed (PrivilegesRequired=lowest) — the
; app's own self-update (updater.py) replaces its exe in place after
; install, which only works if that folder is writable by the user without
; elevation, and autolaunch.py's Run-key entry is per-user (HKCU) too, so
; an admin-owned install location would fight both of those.

#define MyAppVersion GetVersionNumbersString(SourcePath + "\dist\profitdog.exe")

[Setup]
AppId={{4C6F5F9F-6E3A-4B7E-9C7B-2D3E9A1F7C1D}
AppName=Wardogs Profit Tracker
AppVersion={#MyAppVersion}
AppPublisher=Addison Wightman
AppPublisherURL=https://profitdogs.app
AppSupportURL=https://github.com/Adazan21/wardogs-profit-tracker/issues
AppUpdatesURL=https://profitdogs.app
DefaultDirName={localappdata}\Programs\WardogsProfitTracker
DefaultGroupName=Wardogs Profit Tracker
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename=WardogsProfitTrackerSetup
SetupIconFile=icons\app_icon.ico
UninstallDisplayIcon={app}\profitdog.exe
LicenseFile=LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\profitdog.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Wardogs Profit Tracker"; Filename: "{app}\profitdog.exe"
Name: "{group}\Uninstall Wardogs Profit Tracker"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Wardogs Profit Tracker"; Filename: "{app}\profitdog.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\profitdog.exe"; Description: "Launch Wardogs Profit Tracker"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Cleans up the auto-launch-with-Wardogs registry entry (autolaunch.py) if
; the app ever wrote one — left behind otherwise, pointing at a now-deleted
; exe. Ignored if it was never set; not an Inno-owned [Registry] entry
; since the app itself writes this at runtime, not the installer.
Filename: "{cmd}"; Parameters: "/C reg delete ""HKCU\Software\Microsoft\Windows\CurrentVersion\Run"" /v WardogsProfitTracker /f"; Flags: runhidden; RunOnceId: "DelAutolaunchRegKey"
