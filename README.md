# BIMcloud Backup Local

English | [Português (Brasil)](README.pt-BR.md)

[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

Free Windows program that keeps a copy of your **Graphisoft BIMcloud SaaS** on your own
computer, automatically: Archicad Teamwork projects, libraries and every other file.

![BIMcloud Backup Local window](docs/images/window.en.png)

> [!WARNING]
> Version in development. File backups and project exports have been tested on a real
> BIMcloud SaaS; library exports have not yet.

## Features

- Exports projects (`.BIMProject`, the latest `.pln` made by BIMcloud, or both) and libraries
  (`.BIMLibrary`), and copies every other file.
- Can include BIMcloud's snapshots in the exported files.
- Copies the whole BIMcloud or only the folders, projects and libraries you choose.
- Automatic backup every so many minutes, hours or days, even on a server with nobody signed in.
- One folder with date and time per backup; files that did not change are not downloaded again.
- Deletes backups older than the period you choose, always keeping a minimum.
- Stops by itself past a time limit or when the disk runs low on space; can be cancelled at any
  time.
- Shows a Windows notification when an automatic backup fails.
- Sign-in on BIMcloud's own page, with two-step verification. The program never sees your
  password.
- Interface in English or Brazilian Portuguese.

## Download

Download the latest version from [Releases](../../releases), the only official download place:

- `BIMcloudBackup-Setup.exe`: the installer (recommended);
- `BIMcloudBackup.exe`: the program alone, with no installation.

Each file comes with a `.sha256` to check it: `Get-FileHash .\BIMcloudBackup-Setup.exe` in
PowerShell must give the same code.

## Installation

- **Installer:** open `BIMcloudBackup-Setup.exe`. It does not ask for administrator rights.
- **No installation:** keep `BIMcloudBackup.exe` in a fixed folder and open it from there.

The program is not code-signed. If Windows shows "Windows protected your PC", click
**More info** and **Run anyway**.

The first time it opens, the program asks for the language: English or Português (Brasil). You
can change it later in the top right corner of the window.

![Language choice](docs/images/language.png)

## Requirements

- Windows 10, Windows 11 or Windows Server 2016 to 2025.
- An account on your office's BIMcloud SaaS.

## Configuration

Follow the numbered steps of the window and click **Save changes**:

1. **Connection to BIMcloud:** type the BIMcloud address and click **Sign in to BIMcloud**.
2. **What to copy:** projects, libraries and other files; **Choose...** limits it to some
   folders.
3. **Where to save:** the destination folder of the backups.
4. **History:** for how many days to keep the backups.
5. **Automatic backup:** turn on the switch and choose when.

## How to Use

- Open the program from the Start menu. **Back up now** tests it right away.
- Closing the window keeps the program in the notification area, near the clock. To close it
  for good, use **Exit** in the icon's menu.
- The automatic backup runs through the Windows Task Scheduler, even with the program closed.
- Do not edit anything inside the backup folders: to work on a file, copy it first.

The [User guide](docs/GUIDE.md) has the step by step, how to restore and how to use it on a
server.

## Updating

Download the new version and run the installer over the old one. The configuration, the
sign-in and the automatic backup keep working.

## Troubleshooting

- **"Access expired: sign in again":** click **Sign in again**.
- **Windows blocks the program without offering "Run anyway":** that is Windows 11 Smart App
  Control; see the [guide](docs/GUIDE.md#2-the-windows-warning-when-opening).
- **The automatic backup did not run:** check that the computer was on and you were signed in,
  or use the option for servers.
- **"Free space below X GB":** free some space, change the destination or keep fewer days.
- **"Save As" windows in the browser during the backup:** close BIMcloud Manager in the browser.

More cases are in the [User guide](docs/GUIDE.md).

## Report a Bug

Open an [issue](../../issues), in English or Portuguese. Before attaching logs, replace the
names of projects, folders and clients with generic ones, and never attach `.pln`,
`.BIMProject` or `.BIMLibrary` files. Vulnerabilities go through the private report described
in [SECURITY.md](SECURITY.md).

## License

[PolyForm Shield 1.0.0](LICENSE): free to use, including in offices and companies. You may not
sell the program or build a competing product with it.

## Disclaimer

This project is independent and is not affiliated with, endorsed or sponsored by Graphisoft SE
or the Nemetschek Group. *Graphisoft*, *Archicad* and *BIMcloud* are trademarks of their
owners. The program is provided as is, without warranty; do not use it as your only backup.

---

Copyright © 2026 Ettore Torres
