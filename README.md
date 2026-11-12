# conan-base

Shared Conan 2 recipe logic for CMake projects. Instead of every project carrying
its own full `conanfile.py`, projects inherit layout, toolchain, build, tests and
packaging from here (as the Conan package `cmake-base`) and only declare what is
specific to them, mainly their dependencies.

## 1. Register the base (once per environment)

Conan resolves `cmake-base` from its local cache, never from a neighbouring
folder. Register it before building any project that uses it:

```
cd C:\thisIsMyDirectory          (the folder that contains conan-base/)
conan create conan-base
conan list "cmake-base/*"        (should list cmake-base/1.x)
```

Run `conan create conan-base` again after **every** change to the base,
otherwise projects keep using the old copy from the cache.

Windows and WSL have **separate** Conan caches (`%USERPROFILE%\.conan2` vs
`~/.conan2`), so register the base in each of them.

## 2. Use it in a project

A project's `conanfile.py` extends exactly one class from the base:

```python
from conan import ConanFile

class MyApp(ConanFile):
    name = "myapp"
    version = "1.0"
    python_requires = "cmake-base/[>=1.0 <2]"
    python_requires_extend = "cmake-base.CMakeAppBase"   # or "cmake-base.QtApp"

    def requirements(self):                  # only if it needs Conan packages
        self.requires("fmt/11.0.2")
```

| Class | For |
|---|---|
| `CMakeAppBase` | any CMake application (console, libraries + apps) |
| `QtApp` | `CMakeAppBase` + a locally installed Qt 6 kit (auto-detected, windeployqt after build) |

The project's `CMakeLists.txt` should have an `install()` rule for its
executables and register tests with CTest behind `BUILD_TESTING`.
Tests are off by default; turn them on with `-o "&:with_tests=True"`.

Extension points (override in the project, call `super()` first):
`configure_toolchain(self, tc)` for extra CMake variables, `post_build(self)`
for steps after the build.

## 3. What profiles are for

Three files, three questions:

| File | Answers |
|---|---|
| `CMakeLists.txt` | *What* gets compiled? |
| `conanfile.py` | *What does the project need?* (dependencies, options) |
| **profile** | ***With what and how?*** (OS, compiler + version, C++ standard, Debug/Release, generator, flags) |

A profile describes the toolchain, not the project. Conan uses it to

- pick dependency binaries built for exactly these settings (or build them with
  `--build=missing`), and
- write `conan_toolchain.cmake`, which tells CMake the compiler, flags and
  generator.

Without `-pr`, Conan uses the **default** profile that `conan profile detect`
guessed for this machine. That guess differs between machines, so projects
keep explicit profiles in a `profiles/` folder (see `lan_comm/profiles`).
Debug profiles include their Release profile and only switch the build type:

```
include(windows-msvc)

[settings]
build_type=Debug
```

`-pr:a` applies a profile to both Conan contexts: **host** (what you build for)
and **build** (tools that run during the build). Without cross-compiling, both
are the same machine, so `-pr:a` is what you want.

## 4. Which shell for which toolchain

A profile's `os` must match the shell you run Conan in. A Windows profile
generates `.bat` scripts and Windows paths, a Linux profile shell scripts.

| Toolchain | Run Conan from | Profiles | Build folder | CMake presets |
|---|---|---|---|---|
| Visual Studio (MSVC) | PowerShell / cmd | `windows-msvc`, `windows-msvc-debug` | `build/Windows` (one solution, Debug + Release) | `conan-default`, `conan-release`, `conan-debug` |
| MSYS2 MinGW GCC | PowerShell / cmd | `windows-mingw`, `windows-mingw-debug` | `build/Release_Mingw`, `build/Debug_Mingw` | `conan-mingw-release`, `conan-mingw-debug` |
| Linux / WSL GCC | WSL / Linux shell | `linux-gcc`, `linux-gcc-debug` | `build/Release_Linux`, `build/Debug_Linux` | `conan-linux-release`, `conan-linux-debug` |

The base names the folders after the toolchain, so all of them can live in one
checkout side by side without mixing outputs or clashing preset names.

### Visual Studio (MSVC)

```powershell
conan install . -pr:a profiles/windows-msvc
conan install . -pr:a profiles/windows-msvc-debug
conan build   . -pr:a profiles/windows-msvc
```

The two `install` lines prepare **both** configurations in the one solution;
without the Debug one, Visual Studio's Debug configuration cannot find the
dependency headers. Re-run both after changing dependencies. Then open the
`.slnx`/`.sln` in `build\Windows`.

### MinGW (MSYS2 UCRT64)

One-time setup, in the **"MSYS2 UCRT64"** shell (UCRT64 has GCC but no build tool):

```bash
pacman -S mingw-w64-ucrt-x86_64-ninja
```

Then from **PowerShell** (not the MSYS2 shell, not WSL):

```powershell
conan build . -pr:a profiles/windows-mingw --build=missing
```

- `--build=missing`: ConanCenter offers prebuilt packages mostly for MSVC, so
  non-header-only dependencies are compiled once for MinGW, then cached.
- The profile points at `C:/msys64/ucrt64/bin/g++.exe`, selects Ninja as
  generator and links with `-static`, so the `.exe` runs without MSYS2's DLLs
  (`libstdc++-6.dll`, `libgcc_s_seh-1.dll`, `libwinpthread-1.dll`) on `PATH`.
- `compiler.version` in the profile must match the installed GCC
  (`C:\msys64\ucrt64\lib\gcc\x86_64-w64-mingw32\<version>`).

### Linux / WSL

Newer Debian/Ubuntu refuse `pip install` into the system Python
("externally-managed-environment"). Install Conan with pipx instead:

```bash
sudo apt update
sudo apt install pipx build-essential cmake
pipx ensurepath                  # then reopen the terminal
pipx install conan
conan profile detect
conan create conan-base          # WSL has its own cache, see section 1
```

Then in the project:

```bash
conan build . -o "&:with_tests=True" --build=missing
```

Plain `conan build .` uses the detected compiler. The `linux-gcc` profiles pin
a GCC version; use them only if it matches `gcc --version`. CMake 3.22 or newer
is required (`pipx install cmake` if apt's is older).

Building inside `/mnt/c/...` works but is slow; a clone in WSL's own
filesystem (e.g. `~/projects`) builds much faster.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Version range '>=1.0 <2' from requirement 'cmake-base/[>=1.0 <2]' ... could not be resolved` | Base not in this environment's cache | `conan create conan-base` (in this shell: Windows and WSL are separate) |
| `conanbuild.bat: @echo: not found`, `call: not found` | Windows profile run from WSL | Run Windows profiles from PowerShell; delete the half-built folder first |
| `externally-managed-environment` on `pip install conan` | System Python is protected | `pipx install conan` |
| `Invalid setting ... compiler.version` | Conan older than the compiler | `pip install -U conan` / `pipx upgrade conan` |
| `Could not find a package configuration file provided by "Qt6"` | No Qt kit matching the profile's compiler | Install the matching kit (e.g. MSVC 2022 64-bit) or set `user.qt:dir` in the profile `[conf]` |
| Visual Studio cannot find a dependency header (e.g. `asio.hpp`) | Debug configuration not prepared | `conan install . -pr:a profiles/windows-msvc-debug`, then Reload All |
| Strange errors after updating the base | Old build folder generated by the previous version | Delete the toolchain's build folder and build again |

## 6. Changing the base

1. Edit `conanfile.py` here and bump `version` (`1.2` → `1.3` for compatible
   changes; `2.0` for breaking ones, which projects pinned to `<2` won't pick up).
2. `conan create conan-base` in every environment you build in.
3. Delete old build folders of affected projects once, then build as usual.
