# Release Creation Instructions

This document describes the recommended release pipeline for **MapGenie Map Extractor**.

The goal is to produce a reproducible Windows release containing the GUI and CLI builds, validate the frozen executables, optionally create an installer and sign the binaries, and then publish the artifacts as a GitHub Release.

> This guide does **not** publish a release automatically. Run each step manually and verify the outputs before proceeding.

---

## 1. Recommended Release Flow

```text
Source
  ↓
Bump application version
  ↓
Clean Python 3.14 build environment
  ↓
Install runtime + build dependencies
  ↓
Run regression tests
  ↓
Build GUI and CLI with PyInstaller
  ↓
Test frozen executables
  ├─ GTA III analysis
  ├─ RDR2 known-good tile probe
  └─ Low-zoom extraction + stitching
  ↓
Test on a clean Windows machine / Windows Sandbox
  ↓
Create portable ZIP
  ↓
Generate SHA-256 checksum
  ↓
Optional: create Inno Setup installer
  ↓
Optional: code-sign executables and installer
  ↓
Create Git tag
  ↓
Publish GitHub Release
```

---

# 2. Prerequisites

Recommended build environment:

- Windows 10/11 x64
- Python 3.14 x64
- Git
- PowerShell
- PyInstaller
- Project runtime dependencies from `requirements.txt`

Optional:

- Inno Setup – for a Windows installer
- Windows SDK / SignTool – for code signing
- A trusted code-signing certificate or managed signing service

PyInstaller builds are platform-specific. Build the Windows release on Windows.

---

# 3. Prepare the Source Tree

Start from a clean working tree.

```powershell
git status
```

Review any uncommitted changes before continuing.

The application version should be defined in the extractor source, for example:

```python
APP_VERSION = "1.0"
```

Before a new release, update it:

```python
APP_VERSION = "1.1"
```

Verify it:

```powershell
python map_extractor.py --version
```

The following should all use the same version:

```text
APP_VERSION
Git tag
Release title
ZIP filename
Installer filename
```

Recommended Git tag format:

```text
v1.1
```

---

# 4. Create a Clean Build Environment

Do not create official builds from a general-purpose development environment.

From the repository root:

```powershell
py -3.14 -m venv .venv-build
```

Activate it:

```powershell
.\.venv-build\Scripts\Activate.ps1
```

Upgrade pip:

```powershell
python -m pip install --upgrade pip
```

Install runtime dependencies:

```powershell
python -m pip install -r requirements.txt
```

Install build/test dependencies:

```powershell
python -m pip install pytest pyinstaller
```

Verify:

```powershell
python --version
pyinstaller --version
```

---

# 5. Run Tests Before Packaging

Run the full regression suite:

```powershell
python -m pytest -q
```

Do not proceed with a release if tests fail.

Also verify the source application:

```powershell
python map_extractor.py --version
```

Check the CLI help:

```powershell
python map_extractor.py --help
```

Test a known-good RDR2 tile:

```powershell
python map_extractor.py "https://rdr2map.com/" `
  --probe-tile 0,7,95,56 `
  --transport chrome
```

Expected characteristics:

```text
status: 200
content_type: image/jpeg
is_image: true
http_version: 2
```

---

# 6. Clean Previous Build Artifacts

Before a release build:

```powershell
Remove-Item -Recurse -Force .\build -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force .\dist -ErrorAction SilentlyContinue
```

If old `.spec` files are not intentionally maintained, remove them as well.

---

# 7. Build the CLI

For the first stable release workflow, use **PyInstaller `--onedir`**.

This is easier to debug and generally more reliable with native libraries such as `curl_cffi`.

Run:

```powershell
pyinstaller `
  --noconfirm `
  --clean `
  --onedir `
  --console `
  --name MapExtractorCLI `
  --hidden-import=_cffi_backend `
  --collect-all curl_cffi `
  map_extractor.py
```

Expected output:

```text
dist\
  MapExtractorCLI\
    MapExtractorCLI.exe
    _internal\
      ...
```

Do not move only the `.exe`.

The `_internal` directory is part of the application.

---

# 8. Build the GUI

Build the GUI separately:

```powershell
pyinstaller `
  --noconfirm `
  --clean `
  --onedir `
  --windowed `
  --name MapExtractor `
  --hidden-import=_cffi_backend `
  --collect-all curl_cffi `
  gui.py
```

Expected output:

```text
dist\
  MapExtractor\
    MapExtractor.exe
    _internal\
      ...
```

`--windowed` prevents a console window from appearing behind the Tkinter interface.

---

# 9. Verify the Frozen CLI

Test the packaged executable rather than the Python source.

Version:

```powershell
.\dist\MapExtractorCLI\MapExtractorCLI.exe --version
```

Help:

```powershell
.\dist\MapExtractorCLI\MapExtractorCLI.exe --help
```

---

## GTA III Analysis Test

```powershell
.\dist\MapExtractorCLI\MapExtractorCLI.exe `
  "https://mapgenie.io/grand-theft-auto-3/maps/liberty-city" `
  --inspect
```

Confirm:

- page discovery succeeds
- tile sets are found
- zoom information appears correctly

---

## RDR2 Chrome-Transport Test

Use the known-good z7 probe:

```powershell
.\dist\MapExtractorCLI\MapExtractorCLI.exe `
  "https://rdr2map.com/" `
  --probe-tile 0,7,95,56 `
  --transport chrome `
  --tls-status
```

Confirm:

```text
status = 200
content type = image/jpeg
is image = true
HTTP version = 2
```

This is an important packaging test because it exercises:

- `curl_cffi`
- Chrome impersonation
- HTTP/2
- branded-domain request headers
- TLS handling

---

# 10. Run a Small End-to-End Extraction

Avoid testing a full high-resolution map during release validation.

Use a low zoom level:

```powershell
.\dist\MapExtractorCLI\MapExtractorCLI.exe `
  "https://rdr2map.com/" `
  --zoom 2 `
  --tileset 0 `
  --output .\package-test `
  --open-output
```

This validates:

```text
map discovery
→ tile transport
→ downloading
→ caching
→ stitching
→ timer
→ output-folder opening
```

Verify that a stitched image is produced.

---

# 11. Test the Frozen GUI

Launch:

```powershell
.\dist\MapExtractor\MapExtractor.exe
```

Verify:

- application starts without a console window
- URL analysis works
- zoom override works
- transport selection works
- extraction works
- elapsed time appears after completion
- output folder opens when enabled
- closing and reopening the application works normally

Perform at least one low-zoom extraction.

---

# 12. Test on a Clean Windows Environment

This step is strongly recommended.

Use either:

- another Windows PC
- Windows Sandbox
- a clean virtual machine

Ideally, Python should **not** be installed there.

Copy only the packaged release build.

Verify:

```text
GUI launches
CLI launches
GTA III analysis works
RDR2 z7 probe works
RDR2 low-zoom extraction works
PNG stitching works
timer works
Open Output Folder works
TLS handling works
```

This catches dependencies that may accidentally exist only on the development PC.

---

# 13. Optional: Test a One-File Build

Only attempt this after `--onedir` builds work reliably.

CLI:

```powershell
pyinstaller `
  --noconfirm `
  --clean `
  --onefile `
  --console `
  --name MapExtractorCLI `
  --hidden-import=_cffi_backend `
  --collect-all curl_cffi `
  map_extractor.py
```

GUI:

```powershell
pyinstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name MapExtractor `
  --hidden-import=_cffi_backend `
  --collect-all curl_cffi `
  gui.py
```

The resulting files are approximately:

```text
MapExtractor.exe
MapExtractorCLI.exe
```

### Recommendation

Prefer `--onedir` for the initial official release.

One-file builds unpack themselves into a temporary directory at startup and may receive more scrutiny from corporate endpoint-security software.

---

# 14. Recommended Future Improvement: Commit a PyInstaller Spec

Once the build is stable, commit a PyInstaller `.spec` file to the repository.

This avoids maintaining long command lines and makes official builds reproducible.

The release command then becomes approximately:

```powershell
pyinstaller --noconfirm --clean MapExtractor.spec
```

A future shared spec can also package the GUI and CLI while sharing common dependencies.

Do this only after the simple GUI and CLI builds are known to work reliably.

---

# 15. Create the Release Directory

Create a clean release folder:

```powershell
New-Item -ItemType Directory -Force .\release
```

Suggested structure:

```text
release\
  MapGenie Map Extractor\
    GUI\
      MapExtractor.exe
      _internal\
    CLI\
      MapExtractorCLI.exe
      _internal\
    README.md
```

Copy the built applications and README into it.

Example:

```powershell
Copy-Item -Recurse `
  ".\dist\MapExtractor" `
  ".\release\MapGenie Map Extractor\GUI"

Copy-Item -Recurse `
  ".\dist\MapExtractorCLI" `
  ".\release\MapGenie Map Extractor\CLI"

Copy-Item `
  ".\README.md" `
  ".\release\MapGenie Map Extractor\README.md"
```

---

# 16. Create the Portable ZIP

Recommended filename:

```text
MapGenie-Map-Extractor-v1.1-Windows-x64.zip
```

Create it:

```powershell
Compress-Archive `
  -Path ".\release\MapGenie Map Extractor" `
  -DestinationPath ".\release\MapGenie-Map-Extractor-v1.1-Windows-x64.zip"
```

Inspect the ZIP before publication.

---

# 17. Generate a SHA-256 Checksum

Run:

```powershell
Get-FileHash `
  ".\release\MapGenie-Map-Extractor-v1.1-Windows-x64.zip" `
  -Algorithm SHA256
```

Save the hash.

Recommended release file:

```text
SHA256SUMS.txt
```

Example contents:

```text
<sha256>  MapGenie-Map-Extractor-v1.1-Windows-x64.zip
```

Publish this file alongside the release artifact.

---

# 18. Optional: Create a Windows Installer

For a polished Windows release, use **Inno Setup**.

Suggested installer filename:

```text
MapGenie-Map-Extractor-v1.1-Setup.exe
```

Typical installer behavior:

```text
Install to:
C:\Program Files\MapGenie Map Extractor\

Install:
GUI executable
CLI executable
bundled dependencies
README

Create:
Start Menu shortcut
optional Desktop shortcut
uninstaller
```

Keep the portable ZIP available even if an installer is provided.

Some users prefer software that does not require installation.

---

# 19. Optional: Code-Sign the Binaries

For wider distribution, signing is strongly recommended.

Unsigned downloaded executables can trigger additional Windows reputation and security warnings.

Windows signing uses `SignTool` from the Windows SDK.

Example:

```powershell
signtool sign `
  /fd SHA256 `
  /tr YOUR_TIMESTAMP_SERVER `
  /td SHA256 `
  /a `
  MapExtractor.exe
```

Verify:

```powershell
signtool verify /pa /v MapExtractor.exe
```

Normally sign:

```text
MapExtractor.exe
MapExtractorCLI.exe
Setup.exe
```

Never commit:

- private signing keys
- PFX files
- certificate passwords

Use environment/repository secrets or a managed signing service for automated builds.

---

# 20. Final Pre-Release Validation

Before creating the Git tag, verify all of the following:

- [ ] `APP_VERSION` is correct
- [ ] source tests pass
- [ ] CLI build succeeds
- [ ] GUI build succeeds
- [ ] CLI `--version` is correct
- [ ] CLI `--help` works
- [ ] GTA III `--inspect` works
- [ ] RDR2 z7 Chrome probe returns 200
- [ ] low-zoom RDR2 extraction succeeds
- [ ] PNG stitching succeeds
- [ ] elapsed extraction timer works
- [ ] `--open-output` works
- [ ] GUI opens cleanly
- [ ] GUI extraction works
- [ ] GUI opens output folder when configured
- [ ] clean-machine test succeeds
- [ ] README is current
- [ ] ZIP opens correctly
- [ ] SHA-256 checksum is generated
- [ ] installer is tested, if included
- [ ] signatures verify, if signing is used

Only continue if all applicable checks pass.

---

# 21. Commit and Tag the Release

Review the repository:

```powershell
git status
```

Commit:

```powershell
git add .
git commit -m "Release v1.1"
```

Create an annotated tag:

```powershell
git tag -a v1.1 -m "MapGenie Map Extractor v1.1"
```

Push the source:

```powershell
git push origin main
```

Push the tag:

```powershell
git push origin v1.1
```

---

# 22. Create the GitHub Release

Create the release from tag:

```text
v1.1
```

Recommended title:

```text
MapGenie Map Extractor v1.1
```

Attach:

```text
MapGenie-Map-Extractor-v1.1-Windows-x64.zip
SHA256SUMS.txt
MapGenie-Map-Extractor-v1.1-Setup.exe    (optional)
```

GitHub automatically generates source archives for the tagged commit, so manually uploading source ZIPs is normally unnecessary.

---

# 23. Suggested Release Notes Format

```markdown
# MapGenie Map Extractor v1.1

## Changes

- ...
- ...
- ...

## Downloads

### Windows Portable

`MapGenie-Map-Extractor-v1.1-Windows-x64.zip`

### Windows Installer

`MapGenie-Map-Extractor-v1.1-Setup.exe`

## Verification

SHA-256 checksums are provided in `SHA256SUMS.txt`.

## Notes

- Windows x64
- No separate Python installation required
- Supports canonical MapGenie maps and supported branded MapGenie domains
```

---

# 24. Post-Release Verification

After publication:

1. Download the artifact from the GitHub Release itself.
2. Verify its SHA-256 checksum.
3. Extract/install it on a clean machine.
4. Launch the GUI.
5. Run the CLI `--version`.
6. Run the RDR2 z7 probe.
7. Perform one low-zoom extraction.

Testing the downloaded artifact catches upload or packaging mistakes that local testing cannot.

---

# 25. Recommended Next Repository Additions

Once manual releases are stable, consider adding:

```text
MapExtractor.spec
build_windows.bat
installer\
  MapExtractor.iss
```

A future `build_windows.bat` could automate:

```text
clean
→ test
→ build GUI
→ build CLI
→ stage release directory
→ create ZIP
→ calculate SHA-256
```

Keep Git tagging, signing, and GitHub publication manual until the build process has proven reliable.

---

# 26. Recommended Philosophy

Keep the release process simple until there is a reason to make it more complex.

For this project, the recommended progression is:

```text
Manual PyInstaller build
→ reproducible spec file
→ build script
→ installer
→ signing
→ optional CI/CD
```

Avoid introducing Docker, Poetry, Nuitka, MSIX, auto-update infrastructure, or complex CI/CD solely for packaging unless the project develops a concrete need for them.

The current PyInstaller-based workflow is sufficient for a reliable Windows release of MapGenie Map Extractor.
