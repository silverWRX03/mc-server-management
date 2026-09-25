# Third-party notices and licenses

mcsm itself is licensed under the [Apache License 2.0](LICENSE).

mcsm has **no third-party runtime dependencies**: it uses only Python and its
standard library. The web UI is hand-written HTML, CSS and JavaScript, with **no
third-party code, fonts, icons or images**. Nothing from another project is copied
into this repository. The downloadable executables bundle the Python runtime and the
PyInstaller bootloader; they're listed below.

`mcsm licenses` prints this list, and the web UI shows it under **Settings → About**.
The list itself lives in `src/mcsm/licenses.py`; keep the two in sync.

## Used by mcsm at runtime

| Software | License | Notes |
|---|---|---|
| [Python](https://www.python.org/) and its standard library | [PSF License 2.0](https://docs.python.org/3/license.html) | Runs mcsm. Installed by you, not bundled. |

## Bundled into the downloadable executables

The standalone executables on the releases page (`mcsm-windows-x64.exe`,
`mcsm-macos-arm64`, `mcsm-linux-*`) are built with PyInstaller and include the
following. Their full license texts ship inside each executable; print them with
`mcsm licenses --full`. pip/pipx installs don't include these.

| Software | License | Notes |
|---|---|---|
| [Python](https://www.python.org/) runtime and standard library | [PSF License 2.0](https://docs.python.org/3/license.html) | Python's license file also covers components it includes, such as OpenSSL (Apache-2.0), zlib (zlib license), libffi and expat (MIT), bzip2, SQLite (public domain), xz and mpdecimal. |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) bootloader | GPL-2.0-or-later with the bootloader exception | The exception explicitly allows distributing programs built with PyInstaller under any license. |

The Linux executables use Python from
[python-build-standalone](https://github.com/astral-sh/python-build-standalone): an
ordinary CPython build (PSF-2.0) made portable across Linux distributions. It includes
the libraries Python depends on, each under its own open-source license: OpenSSL
(Apache-2.0), libffi (MIT), SQLite (public domain), zlib (zlib), bzip2 (bzip2), xz/liblzma
(0BSD/public domain), mpdecimal (BSD-2-Clause), expat (MIT), ncurses (X11-style) and
libedit (BSD-3-Clause). Its build scripts are MPL-2.0, but none of them are included.

## Used only for development

These are not installed for users.

| Software | License | Used for |
|---|---|---|
| [pytest](https://github.com/pytest-dev/pytest) | MIT | the test suite (`pip install -e ".[dev]"`) |
| [setuptools](https://github.com/pypa/setuptools) | MIT | building the package |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | GPL-2.0-or-later with the bootloader exception | building the downloadable executables |
| [build](https://github.com/pypa/build) | MIT | building the wheel for releases |
| [uv](https://github.com/astral-sh/uv) | MIT / Apache-2.0 | fetching the portable Python for Linux builds |

## Software mcsm downloads for you

mcsm downloads these from their official sources, on your machine and at your
request. It **never bundles or redistributes** them, and each one stays under its
own license.

| Software | License | When |
|---|---|---|
| Minecraft server (Mojang) | [Minecraft EULA](https://www.minecraft.net/en-us/eula) (proprietary). You must accept it yourself. | every server |
| [Fabric Loader and installer](https://github.com/FabricMC/fabric-loader) | Apache-2.0 | `loader = "fabric"` |
| [Quilt Loader and installer](https://github.com/QuiltMC/quilt-loader) | Apache-2.0 | `loader = "quilt"` |
| [NeoForge](https://github.com/neoforged/NeoForge) | LGPL-2.1 | `loader = "neoforge"` |
| [Minecraft Forge](https://github.com/MinecraftForge/MinecraftForge) | LGPL-2.1 | `loader = "forge"` |
| [Eclipse Temurin](https://adoptium.net/about/) (OpenJDK) | GPL-2.0 with Classpath Exception | when mcsm manages Java |
| Mods from Modrinth or CurseForge | each mod's own license, shown on its project page | the mods you add |

## Online services mcsm talks to

No account or usage data is sent to any of them. Their terms apply to your use.

| Service | Why | Terms |
|---|---|---|
| Mojang version manifest and profile API | Minecraft versions; player UUIDs when editing ops/bans offline | [Minecraft terms](https://www.minecraft.net/en-us/terms) |
| Modrinth API | mod versions, search, identifying imported jars | [Modrinth terms](https://modrinth.com/legal/terms) |
| CurseForge API | CurseForge mods, only with your own API key | [CurseForge API terms](https://support.curseforge.com/en/support/solutions/articles/9000207405) |
| Fabric, Quilt, NeoForge and Forge metadata and maven servers | loader versions and installers | see each project |
| Adoptium API | Java downloads | [adoptium.net](https://adoptium.net/) |
| GitHub API | checking for new mcsm releases | [GitHub terms](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service) |
| Discord webhooks | notifications, only if you configure one | [Discord terms](https://discord.com/terms) |

## AI assistance

Much of mcsm was written with the help of AI (Anthropic's Claude). The code is
covered by the project's Apache-2.0 license like any other contribution.
