"""cmake-base: shared Conan 2 recipe logic for CMake applications.

Register once per machine (and again after every change):

    conan create .

Use from a project's conanfile.py:

    class MyApp(ConanFile):
        name = "myapp"
        version = "1.0"
        python_requires = "cmake-base/[>=1.0 <2]"
        python_requires_extend = "cmake-base.CMakeAppBase"

Projects extend exactly one class:

    cmake-base.CMakeAppBase   plain CMake application
    cmake-base.QtApp          + locally installed Qt 6 kit (QtMixin)

New feature combinations are added as classes at the bottom of this file
(e.g. `class QtCudaApp(QtMixin, CudaMixin, CMakeAppBase)`), never by listing
several classes in a project's python_requires_extend (see the note there).

Conventions a project follows to get everything for free:
  * CMakeLists.txt in the project root
  * an install() rule for its executables         (used by `conan create`)
  * tests registered with CTest behind BUILD_TESTING (used by -o "&:with_tests=True")

Extension points (override, and call super() first):
  * configure_toolchain(self, tc)  -> extra CMake cache variables / toolchain tweaks
  * post_build(self)               -> steps after a successful build
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from conan import ConanFile
from conan.errors import ConanException
from conan.tools.cmake import CMake, CMakeDeps, CMakeToolchain, cmake_layout


# =============================================================================
#  CMakeAppBase: everything every CMake application shares
# =============================================================================

class CMakeAppBase:
    package_type = "application"
    settings = "os", "compiler", "build_type", "arch"

    options = {"with_tests": [True, False]}
    default_options = {"with_tests": False}

    # Whole project folder, minus build output and IDE / VCS clutter.
    exports_sources = (
        "*",
        "!build/*",
        "!CMakeUserPresets.json",
        "!.vs/*",
        "!.vscode/*",
        "!.git/*",
    )

    def init(self):
        # A project that declares its own `options` replaces the class attribute
        # above; merge the base options back in so with_tests always exists.
        base = self.python_requires["cmake-base"].module.CMakeAppBase
        self.options.update(base.options, base.default_options)

    def layout(self):
        cmake_layout(self)

        # Name build folders after the toolchain, so outputs of different
        # compilers never mix, even when Windows and WSL share one checkout:
        #   Linux GCC/Clang  -> build/Release_Linux, build/Debug_Linux
        #   MSYS2 / MinGW    -> build/Release_Mingw, build/Debug_Mingw
        #   Visual Studio    -> build/Windows  (one solution holds Debug + Release)
        if self._is_multi_config():
            # cmake_layout produced <base>; put everything in <base>/<tag>.
            self.folders.build = os.path.join(self.folders.build, self.platform_tag())
        else:
            # cmake_layout produced <base>/<BuildType>; swap the last part.
            base = os.path.dirname(self.folders.build)
            name = f"{self.settings.build_type}_{self.platform_tag()}"
            self.folders.build = os.path.join(base, name)
        self.folders.generators = os.path.join(self.folders.build, "generators")

    def generate(self):
        # find_package() support for whatever the project lists in `requires`.
        # Generates nothing when there are no requirements.
        CMakeDeps(self).generate()

        tc = CMakeToolchain(self)
        # CMakeUserPresets.json in the project root is shared by every build
        # folder. Distinct prefixes keep names unique across toolchains
        # (conan-linux-release vs conan-release), which CMake requires.
        # Visual Studio keeps Conan's standard names: conan-default,
        # conan-release, conan-debug.
        if self.settings.get_safe("compiler") != "msvc":
            tc.presets_prefix = f"conan-{self.platform_tag().lower()}"
        # Lets editors (VS Code, Windsurf, clangd) see real include paths.
        # Only produced by Makefile / Ninja generators, not by Visual Studio.
        tc.cache_variables["CMAKE_EXPORT_COMPILE_COMMANDS"] = "ON"
        tc.cache_variables["BUILD_TESTING"] = bool(self.options.with_tests)
        self.configure_toolchain(tc)
        tc.generate()

    def build(self):
        cmake = CMake(self)
        cmake.configure()
        cmake.build()
        if self.options.with_tests:
            cmake.test()
        self.post_build()

    def package(self):
        CMake(self).install()

    # ---- extension points ---------------------------------------------------

    def configure_toolchain(self, tc):
        """Override to add cache variables etc. Call super() first."""

    def post_build(self):
        """Override to run steps after the build. Call super() first."""

    # ---- helpers for mixins and projects ------------------------------------

    def platform_tag(self):
        """Short toolchain name used in build folder and preset names."""
        os_name = str(self.settings.os)
        compiler = self.settings.get_safe("compiler")
        if os_name == "Windows":
            return {"msvc": "Windows", "gcc": "Mingw", "clang": "Clang"}.get(compiler, "Windows")
        return os_name   # Linux, Macos, ...

    def _is_multi_config(self):
        """Same rule cmake_layout uses to pick a multi-config generator."""
        gen = self.conf.get("tools.cmake.cmaketoolchain:generator")
        if gen:
            return any(k in gen for k in ("Visual", "Xcode", "Multi-Config"))
        return self.settings.get_safe("compiler") == "msvc"

    def built_executables(self):
        """Executables in the build folder (handles VS's per-config subfolder)."""
        build = Path(self.build_folder)
        suffix = ".exe" if self.settings.os == "Windows" else ""
        found = []
        for folder in (build / str(self.settings.build_type), build):
            if not folder.is_dir():
                continue
            for f in folder.iterdir():
                if not f.is_file():
                    continue
                if suffix and f.suffix.lower() == suffix:
                    found.append(f)
                elif not suffix and os.access(f, os.X_OK) and not f.suffix:
                    found.append(f)
        return found


# =============================================================================
#  QtMixin: a locally installed Qt 6 kit (not a Conan dependency)
# =============================================================================

class QtMixin:
    """Finds a Qt 6 kit on this machine and wires it into the build.

    Lookup order (first hit wins):
      1. profile conf    [conf] user.qt:dir=C:/Qt/6.12.0/msvc2022_64
                         or on the command line: -c user.qt:dir=...
      2. env variable    QT_DIR
      3. qtpaths6 / qtpaths / qmake6 / qmake on PATH (only if the kit fits the profile)
      4. Qt installer folders C:/Qt (Windows), ~/Qt and QT_ROOT:
         newest Qt 6.x whose kit matches the profile's compiler and arch

    On Windows it also
      * lets CMake pick the MSVC runtime per configuration (/MDd Debug, /MD else),
        matching how Qt kits are built, and
      * runs windeployqt after the build so the .exe starts by double-click.
    """

    def configure_toolchain(self, tc):
        super().configure_toolchain(tc)

        # Conan would set the MSVC runtime only for the invoked build_type,
        # leaving other VS configurations on /MT, which corrupts std::string
        # passed to/from Qt's /MD(d) DLLs.
        tc.blocks.remove("vs_runtime")

        qt_dir = self.qt_dir()
        if qt_dir:
            tc.cache_variables["CMAKE_PREFIX_PATH"] = qt_dir.as_posix()

    def post_build(self):
        super().post_build()
        if self.settings.os != "Windows":
            return
        qt_dir = self.qt_dir()
        windeployqt = qt_dir / "bin" / "windeployqt.exe" if qt_dir else None
        if not windeployqt or not windeployqt.is_file():
            self.output.warning("windeployqt not found; Qt DLLs were not copied next to the .exe")
            return
        mode = "--debug" if self.settings.build_type == "Debug" else "--release"
        exes = self.built_executables()
        if not exes:
            self.output.warning("No built .exe found; skipped windeployqt")
        for exe in exes:
            self.run(f'"{windeployqt}" {mode} --no-translations "{exe}"')

    # ---- discovery ------------------------------------------------------------

    def qt_dir(self):
        """Qt kit prefix as a Path, or None to let CMake search its defaults.
        Cached, because generate() and build() both need it."""
        if not hasattr(self, "_qt_dir_cache"):
            self._qt_dir_cache = self._find_qt()
        return self._qt_dir_cache

    @staticmethod
    def _is_qt6_prefix(path):
        return bool(path) and (Path(path) / "lib" / "cmake" / "Qt6" / "Qt6Config.cmake").is_file()

    def _find_qt(self):
        lookups = [
            ("profile conf user.qt:dir", lambda: self.conf.get("user.qt:dir"), True),
            ("environment variable QT_DIR", lambda: os.environ.get("QT_DIR"), True),
            ("Qt tool on PATH", self._qt_from_path_tools, False),
            ("Qt installer folder", self._qt_from_install_dirs, False),
        ]
        for source, lookup, explicit in lookups:
            found = lookup()
            if not found:
                continue
            found = Path(found)
            if self._is_qt6_prefix(found):
                self.output.info(f"Using Qt from {found.as_posix()} ({source})")
                return found
            if explicit:   # a wrong explicit setting must fail, not be skipped
                raise ConanException(
                    f"{source} points to '{found}', but there is no "
                    "lib/cmake/Qt6/Qt6Config.cmake below it. It must be a Qt kit "
                    "folder such as C:/Qt/6.12.0/msvc2022_64."
                )

        if self.settings.os == "Windows":
            available = ", ".join(sorted({f"{'.'.join(map(str, v))}/{k.name}"
                                          for v, k in self._all_kits()})) or "none"
            raise ConanException(
                "Could not find a Qt 6 kit matching this profile "
                f"(compiler={self.settings.compiler}, arch={self.settings.arch}).\n"
                f"Kits found: {available}.\n"
                f"Searched: {', '.join(r.as_posix() for r in self._install_roots())} and PATH.\n"
                "Install a matching kit with the Qt Maintenance Tool, or set "
                "user.qt:dir in your profile."
            )
        # Linux/macOS distro packages live in CMake's default search paths.
        self.output.info("No Qt kit detected; relying on CMake's default search paths")
        return None

    def _qt_from_path_tools(self):
        for tool, args in (
            ("qtpaths6", ["--query", "QT_INSTALL_PREFIX"]),
            ("qtpaths",  ["--query", "QT_INSTALL_PREFIX"]),
            ("qmake6",   ["-query", "QT_INSTALL_PREFIX"]),
            ("qmake",    ["-query", "QT_INSTALL_PREFIX"]),
        ):
            exe = shutil.which(tool)
            if not exe:
                continue
            try:
                out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                continue
            prefix = out.stdout.strip()
            if self._is_qt6_prefix(prefix) and self._kit_matches(Path(prefix).name):
                return prefix
        return None

    def _install_roots(self):
        roots = []
        if os.environ.get("QT_ROOT"):
            roots.append(Path(os.environ["QT_ROOT"]))
        if self.settings.os == "Windows":
            roots.append(Path("C:/Qt"))
        roots.append(Path.home() / "Qt")
        return roots

    def _all_kits(self):
        """(version tuple, kit path) for every Qt 6 kit under the install roots."""
        for root in self._install_roots():
            if not root.is_dir():
                continue
            for version_dir in root.iterdir():
                m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version_dir.name)
                if not m or m.group(1) != "6" or not version_dir.is_dir():
                    continue
                version = tuple(int(x) for x in m.groups())
                for kit in version_dir.iterdir():
                    if self._is_qt6_prefix(kit):
                        yield version, kit

    def _qt_from_install_dirs(self):
        matching = [(v, self._kit_preference(k.name), k)
                    for v, k in self._all_kits() if self._kit_matches(k.name)]
        if not matching:
            return None
        return max(matching, key=lambda t: t[:2])[2]

    def _kit_matches(self, kit_name):
        """Does an installer kit folder name fit the profile's compiler and arch?"""
        suffix = {"x86_64": "_64", "armv8": "_arm64", "x86": ""}.get(str(self.settings.arch))
        if suffix is None:
            return False
        compiler = str(self.settings.compiler)
        if self.settings.os == "Windows":
            if compiler == "msvc":
                return re.fullmatch(rf"msvc\d{{4}}{suffix}", kit_name) is not None
            if compiler == "gcc":
                return kit_name == f"mingw{suffix}"
            if compiler == "clang":
                return kit_name in (f"llvm-mingw{suffix}", f"mingw{suffix}")
            return False
        if self.settings.os == "Linux":
            return kit_name in (f"gcc{suffix}", f"linux_gcc{suffix}")
        if self.settings.os == "Macos":
            return kit_name == "macos"
        return False

    @staticmethod
    def _kit_preference(kit_name):
        """Among matching kits of one Qt version, prefer the newest MSVC year."""
        m = re.match(r"msvc(\d{4})", kit_name)
        return int(m.group(1)) if m else 0


# =============================================================================
#  Ready-made combinations. Projects extend exactly ONE of these.
#
#  Mixins are composed here, in plain Python, rather than by listing several
#  classes in a project's python_requires_extend: Conan builds that list into
#  base classes in reverse order, which silently puts CMakeAppBase in front of
#  the mixin and skips its overrides.
# =============================================================================

class QtApp(QtMixin, CMakeAppBase):
    """CMake application using a locally installed Qt 6 kit."""


# =============================================================================
#  The package itself: only carries the classes above
# =============================================================================

class CMakeBasePackage(ConanFile):
    name = "cmake-base"
    version = "1.2"
    package_type = "python-require"
    description = "Shared Conan recipe logic for CMake applications (base + Qt mixin)"
