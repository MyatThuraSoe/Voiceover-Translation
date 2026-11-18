# Build RecapUpgrade.exe

Windows 10/11 x64 desktop build of the Recap → Myanmar / English app.

## Requirements

- Python **3.12 x64** (the same version used for development)
- Build tools: `py -3.12 -m pip install -r requirements-build.txt`
- `ffmpeg\ffmpeg.exe` + `ffmpeg\ffprobe.exe` in the project root
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the installer
  (`winget install --id JRSoftware.InnoSetup -e`)
- No separate Chromium download is required: the app uses the installed
  Chrome or Edge through Playwright. Keep Playwright's bundled driver.

## Build

```bat
build_exe.bat
```

or:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

The script runs PyInstaller, copies the large runtime folders next to the exe,
verifies the result with three checks against a throw-away data folder, builds
the installer, then creates the portable zip.

| Check | What it proves |
|---|---|
| `RecapUpgrade.exe --self-test` | TTS, Whisper, FFmpeg, Playwright, Gemini imports and runtime files |
| `RecapUpgrade.exe --smoke-test` | the real Tk window builds and closes |
| `RecapUpgrade.exe --render-test` | Chromium renders Myanmar subtitle + title PNGs |

## Output

| Path | What it is |
|---|---|
| `dist\RecapUpgradeSetup.exe` | **production installer** (per-user, no admin required) |
| `dist\RecapUpgrade\RecapUpgrade.exe` | the desktop app folder |
| `dist\RecapUpgrade-win64.zip` | portable folder; extract it anywhere and run the exe |

The build is **onedir** on purpose. Workers (translation planner and renderer)
relaunch the same exe with `--run-worker`, so they share one runtime instead of
re-extracting a multi-hundred-MB single-file bundle for every stage.

## Where user data lives

Program files are read-only and stay next to `RecapUpgrade.exe`. User data is
written to:

```text
%LOCALAPPDATA%\RecapUpgrade\
    config.json     app settings
    presets.json    saved presets
    .env            GEMINI_API_KEY=... (seeded from .env.example)
    jobs\           job output and run logs
    logs\           app logs and font cache
    models\         optional local TTS models (MMS)
```

Nothing personal (`config.json`, `presets.json`, `.env`) is copied into the
build. On first installed run the app migrates those files from an older
portable build sitting next to the exe, then seeds `.env` from `.env.example`.

## Portable mode

The portable zip contains a `portable.flag`. With that file present the app
writes user data to `userdata\` beside the exe instead of `%LOCALAPPDATA%`.
Set `RECAP_DATA_DIR` to use any other writable folder.

## Notes

- This installer is **not code-signed**. Windows SmartScreen will warn on first
  run until the setup exe is signed with an Authenticode certificate.
- Uninstalling keeps `%LOCALAPPDATA%\RecapUpgrade` so settings and finished
  jobs survive an upgrade or reinstall.
- The first job downloads the Whisper model to the user cache; later runs reuse it.
