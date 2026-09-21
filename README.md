# vs-mod-updater

A small command-line tool for [Vintage Story](https://www.vintagestory.at/) that:

- Scans a `VintagestoryData/Mods` folder and checks each installed mod against [mods.vintagestory.at](https://mods.vintagestory.at) for a newer release.
- Checks each installed mod's declared dependencies and installs anything that's missing (recursively, including dependencies of dependencies).
- Can install one or more mods by id on demand, along with their dependencies.
- Never leaves duplicate versions of the same mod installed — an old file/folder is removed when it's replaced.
- Uses only the Python standard library (`urllib`, `zipfile`, `json`, ...) — no third-party dependencies required.

## Requirements

- Python 3.9+
- No other dependencies.

## Installation

### pip install directly from git

```bash
pip install git+https://github.com/<your-username>/vs-mod-updater.git
```

Replace `<your-username>/vs-mod-updater` with wherever you push this repo.
This installs a `vs-mod-updater` command on your `PATH`.

To install a specific branch or tag:

```bash
pip install "git+https://github.com/<your-username>/vs-mod-updater.git@<branch-or-tag>"
```

To upgrade later:

```bash
pip install --upgrade git+https://github.com/<your-username>/vs-mod-updater.git
```

### From a local clone

```bash
git clone https://github.com/<your-username>/vs-mod-updater.git
cd vs-mod-updater
pip install .
```

Use `pip install -e .` instead if you want an editable install while
developing.

### Without installing

Since it's a single stdlib-only script, you can also just run it directly
with no install step:

```bash
python3 updater.py
```

## Usage

```bash
vs-mod-updater [-p PATH] [-o DIR] [-y] [-i MODID [MODID ...]]
```

(If you didn't `pip install`, replace `vs-mod-updater` with `python3 updater.py`.)

| Flag | Description |
|---|---|
| `-p`, `--path` | Path to `VintagestoryData`, or directly to its `Mods` folder. Defaults to `~/.config/VintagestoryData`. |
| `-o`, `--output` | Directory to write downloaded/updated mod files to. Defaults to the same folder as `--path`; if you point this elsewhere, the source `Mods` folder is left untouched. |
| `-y`, `--yes` | Don't prompt for confirmation — apply changes immediately. |
| `-i`, `--install` | Install one or more mods by id (plus their dependencies) instead of checking for updates. |

### Check for updates (default)

```bash
vs-mod-updater
vs-mod-updater -p ~/.config/VintagestoryData
vs-mod-updater -p ~/.config/VintagestoryData -y
```

This scans your installed mods, reports which have newer releases and which
dependencies are missing, then prompts before downloading anything (skip the
prompt with `-y`).

### Install specific mods

```bash
vs-mod-updater -i carrycapacity xskills
```

Resolves `carrycapacity` and `xskills` plus their full dependency trees, shows
what would be installed/updated, and prompts before applying (again, `-y` to
skip the prompt).

### Write output somewhere other than your Mods folder

```bash
vs-mod-updater -o ~/vs-mod-staging -y
```

Downloads land in `~/vs-mod-staging` instead of your live Mods folder, so you
can review or redistribute them before copying them over yourself.
