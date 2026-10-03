# Security

While the project is at version `0.x`, only the latest version gets fixes.

To report a vulnerability, do not open a public issue: use **Security** → **Report a
vulnerability**, with the description, the steps to reproduce it and the impact. You can write in
English or Portuguese. I answer within 7 days.

Before sending logs, replace the names of projects, folders and clients with generic ones (e.g.
`ProjectA`, `ClientC`) and do not attach `.pln`, `.BIMProject` or `.BIMLibrary` files.

The BIMcloud password never goes through the program. The sign-in is kept in the Windows
Credential Manager and can be deleted by the uninstaller or with `BIMcloudBackup.exe logout`.
