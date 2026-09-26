"""Licenses of everything mcsm uses, downloads or talks to. Kept in sync with THIRD_PARTY_NOTICES.md."""

from __future__ import annotations

PROJECT = ("mcsm (mc-server-management)", "Apache-2.0",
           "https://github.com/silverWRX03/mc-server-management/blob/main/LICENSE")

# (name, how mcsm uses it, license, link)
RUNTIME = [
    ("Python and its standard library", "runs mcsm; mcsm has no other runtime dependencies",
     "PSF-2.0", "https://docs.python.org/3/license.html"),
]

# Only inside the downloadable executables (not pip/pipx installs); full texts ship inside them.
BUNDLED = [
    ("Python runtime and standard library", "bundled so no Python install is needed",
     "PSF-2.0 (includes components under their own licenses, e.g. OpenSSL: Apache-2.0, zlib, libffi: MIT)",
     "https://docs.python.org/3/license.html"),
    ("python-build-standalone (Linux executables)", "a portable CPython build so Linux downloads run on any distro",
     "PSF-2.0 for Python; bundled libraries under their own licenses (OpenSSL: Apache-2.0, libffi: MIT, ...)",
     "https://github.com/astral-sh/python-build-standalone"),
    ("PyInstaller bootloader", "starts the bundled program",
     "GPL-2.0-or-later with the PyInstaller bootloader exception",
     "https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt"),
]

DEVELOPMENT = [
    ("pytest", "running the test suite (not installed for users)", "MIT", "https://github.com/pytest-dev/pytest"),
    ("setuptools", "building the package", "MIT", "https://github.com/pypa/setuptools"),
    ("PyInstaller", "building the downloadable executables", "GPL-2.0-or-later with the bootloader exception",
     "https://github.com/pyinstaller/pyinstaller"),
    ("build", "building the wheel for releases", "MIT", "https://github.com/pypa/build"),
    ("uv", "fetching the portable Python for Linux builds", "MIT / Apache-2.0", "https://github.com/astral-sh/uv"),
]

# Software mcsm downloads for you. None of it is bundled with or redistributed by mcsm.
DOWNLOADED = [
    ("Minecraft server", "the game server itself (from Mojang)", "Minecraft EULA (proprietary)",
     "https://www.minecraft.net/en-us/eula"),
    ("Fabric Loader and installer", "the fabric loader", "Apache-2.0", "https://github.com/FabricMC/fabric-loader"),
    ("Quilt Loader and installer", "the quilt loader", "Apache-2.0", "https://github.com/QuiltMC/quilt-loader"),
    ("NeoForge", "the neoforge loader", "LGPL-2.1", "https://github.com/neoforged/NeoForge"),
    ("Minecraft Forge", "the forge loader", "LGPL-2.1", "https://github.com/MinecraftForge/MinecraftForge"),
    ("Paper", "the paper server", "GPL-3.0 (with MIT parts)", "https://github.com/PaperMC/Paper"),
    ("Eclipse Temurin (OpenJDK)", "the Java runtime, when mcsm manages Java", "GPL-2.0 with Classpath Exception",
     "https://adoptium.net/about/"),
    ("Mods", "whatever you add; each is downloaded from its author's page", "each mod's own license",
     "https://modrinth.com/"),
]

# Online services mcsm talks to (their terms apply; no account data is sent).
SERVICES = [
    ("Mojang version manifest and profile API", "https://www.minecraft.net/en-us/terms"),
    ("Mojang session and texture servers (player skins for the dashboard's head icons)",
     "https://www.minecraft.net/en-us/terms"),
    ("Modrinth API", "https://modrinth.com/legal/terms"),
    ("CurseForge API (only with your API key)", "https://support.curseforge.com/en/support/solutions/articles/9000207405"),
    ("Fabric / Quilt / NeoForge / Forge metadata and maven servers", "see each project above"),
    ("PaperMC API (Paper servers)", "https://papermc.io/"),
    ("Adoptium API", "https://adoptium.net/"),
    ("GitHub API (mcsm's own releases)", "https://docs.github.com/en/site-policy/github-terms/github-terms-of-service"),
    ("Discord webhooks and bot API (only if you set them up)", "https://discord.com/terms"),
    ("Public IP lookups (ipify, ifconfig.co; only when you ask)", "https://www.ipify.org/"),
]


def as_text() -> str:
    lines = [f"{PROJECT[0]}: {PROJECT[1]}  {PROJECT[2]}", "", "Used at runtime:"]
    lines += [f"  {n}: {lic}  ({use})  {url}" for n, use, lic, url in RUNTIME]
    lines += ["", "Bundled into the downloadable executables (full texts: `mcsm licenses --full`):"]
    lines += [f"  {n}: {lic}  ({use})  {url}" for n, use, lic, url in BUNDLED]
    lines += ["", "Used only for development:"]
    lines += [f"  {n}: {lic}  ({use})  {url}" for n, use, lic, url in DEVELOPMENT]
    lines += ["", "Downloaded for you (never bundled or redistributed by mcsm):"]
    lines += [f"  {n}: {lic}  ({use})  {url}" for n, use, lic, url in DOWNLOADED]
    lines += ["", "Online services it talks to:"]
    lines += [f"  {n}  {url}" for n, url in SERVICES]
    lines += ["", "The web UI uses no third-party code, fonts or images."]
    return "\n".join(lines)


def as_dict() -> dict:
    row = lambda n, use, lic, url: {"name": n, "use": use, "license": lic, "url": url}  # noqa: E731
    return {
        "project": {"name": PROJECT[0], "license": PROJECT[1], "url": PROJECT[2]},
        "runtime": [row(*r) for r in RUNTIME],
        "bundled": [row(*r) for r in BUNDLED],
        "development": [row(*r) for r in DEVELOPMENT],
        "downloaded": [row(*r) for r in DOWNLOADED],
        "services": [{"name": n, "url": url} for n, url in SERVICES],
    }


def full_texts() -> list[tuple[str, str]]:
    """(file name, text) of the license texts bundled into a standalone executable."""
    from importlib import resources

    folder = resources.files("mcsm").joinpath("licenses")
    try:
        return [(f.name, f.read_text(encoding="utf-8", errors="replace"))
                for f in sorted(folder.iterdir(), key=lambda f: f.name) if f.name.endswith((".txt", ".md"))]
    except (FileNotFoundError, NotADirectoryError):
        return []
