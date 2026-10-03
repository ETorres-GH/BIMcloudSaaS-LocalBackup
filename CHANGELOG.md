# Changelog

Changes to the program. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Interface in English or Brazilian Portuguese, chosen the first time the program opens
  (English by default) and changed at any time in the top right corner of the window.
- Single window: state of the backups on the left, settings on the right.
- Sign-in on the BIMcloud page, with two-step verification.
- Backup of the BIMcloud files into a folder with date and time.
- Export of projects (`.BIMProject` and/or the latest `.pln`) and libraries (`.BIMLibrary`),
  with the option to include BIMcloud's snapshots, libraries included.
- Choice of the BIMcloud folders to copy, and of single projects and libraries.
- Files that did not change are not downloaded again.
- History by days, always keeping a minimum of backups, or only the latest backup.
- Limits of maximum duration and of free disk space.
- **Cancel backup** button.
- Automatic backup every so many minutes, hours or days, even with nobody signed in to Windows,
  for servers.
- Windows notification when an automatic backup needs attention.
- The program stays in the notification area when the window is closed, with Open, Back up now
  and Exit; option to open it with Windows, off by default.
- Progress of the backup in the window: projects, libraries and files copied.
- List of the saved backups, with date, size, status and the **Open folder** button.
- List of the items with errors of each backup, with the reason in plain words and what to do.
- Several files downloaded at the same time, with new attempts after temporary failures.
- Warning about unsaved changes, with the Discard and Save buttons.
- "?" next to the options, with a short hint about each one.
- Login address that can be opened in another browser or computer.
- Use on Windows Server Core from the Command Prompt.
- Per-user installer, with no administrator rights.
- Daily log.
- `BIMcloudBackup.exe` with its own icon and a `.sha256` file to check the download.
- User guide, and README in English and Portuguese.

### Changed

- Settings in numbered steps, in the order to fill them in.
- The time and the interval of the automatic backup are chosen from lists.
- The folder picker shows how many projects and libraries each folder has.
- The window follows the Windows scale and fits 1366x768 screens without scrolling; the advanced
  options open in a window of their own when they do not fit.
- The BIMcloud user became optional.
- The command-line options `--agendado` and `--bandeja` are now `--scheduled` and `--tray`; the
  old names keep working.

### Fixed

- The BIMcloud `.pln` is downloaded again, also on BIMcloud servers that redirect the download;
  exports show their progress and cancelling the backup also stops them on BIMcloud.
- The log shows the reason of each item that failed and says "1 item failed" instead of
  "1 items failed".

### Security

- Passwords and access keys never show up in the messages nor in the log.
- The sign-in is only sent to BIMcloud itself, over https.
- A download that fails does not leave a half-written file.
- File names that Windows does not accept are renamed, without overwriting anything.
- Executable built only with checked dependencies.
