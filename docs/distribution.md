# Distribution

LabGPU supports two packaging levels.

## Lightweight Launcher Packages

These are small installer/launcher packages. They install or call the normal
`labgpu` command, then start `labgpu desktop`.

```bash
scripts/package_macos_dmg.sh
scripts/package_windows_zip.sh
```

Use these when you want a simple wrapper around the Python package.

## Standalone Desktop Packages

These packages bundle LabGPU with Python using PyInstaller. Users can download
the artifact and run it directly without installing `labgpu` first.

```bash
python scripts/build_standalone.py --clean
```

Outputs:

```text
dist/release/LabGPU-<version>-macOS.dmg
dist/release/LabGPU-<version>-Windows.zip
```

On macOS, the DMG contains `LabGPU.app`. On Windows, the ZIP contains
`LabGPU.exe`.

The standalone app still keeps the same security model:

- binds the UI to `127.0.0.1` by default
- reads the user's normal SSH configuration
- does not install daemons on remote GPU servers
- keeps AI provider secrets local by default

## GitHub Actions

The `Release Build` workflow runs on tags matching `v*` and on manual dispatch.
It builds:

- macOS DMG on `macos-latest`
- Windows ZIP on `windows-latest`

Manual workflow runs upload artifacts to the workflow run. Tag pushes also
create a GitHub Release automatically and attach the generated `.dmg` / `.zip`
files.

```bash
git tag -a v0.1.1-alpha -m "LabGPU v0.1.1 alpha"
git push origin v0.1.1-alpha
```

Release troubleshooting:

- Existing tags do not rebuild automatically after the workflow is added or
  changed. Push a new `v*` tag for the current commit.
- Manual `workflow_dispatch` runs are useful for testing packages, but they do
  not create a GitHub Release.
- Keep `pyproject.toml` and `src/labgpu/__init__.py` in sync before cutting a
  new release tag. The standalone artifact names use `src/labgpu/__init__.py`.

## Current Limitations

- macOS artifacts are not codesigned or notarized yet.
- Windows artifacts are not signed yet.
- There is no tray/menu-bar controller yet; the app opens the local web UI.
- First-run onboarding happens inside the local web UI instead of a native wizard.
