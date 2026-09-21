from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, BinaryIO, TypedDict, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_MOD_URL = "https://mods.vintagestory.at/api/mod/{modid}"
DEFAULT_DATA_PATH = "~/.config/VintagestoryData"
USER_AGENT = "vs-mod-updater"
MAX_WORKERS = 8

ModInfo = dict[str, Any]
"""A mod's parsed modinfo.json contents."""

Release = dict[str, Any]
"""One release entry from the moddb API (mainfile, filename, modversion, ...)."""

Dependencies = dict[str, str]
"""Mapping of dependency modid -> minimum version string."""


def strip_json_comments(text: str) -> str:
    """Strip // and /* */ comments and trailing commas from a JSON5-ish string.

    Some mod authors ship modinfo.json files that aren't strictly valid JSON.
    String contents are left untouched so URLs like "http://..." aren't
    mistaken for comments.
    """
    out: list[str] = []
    in_string = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue

        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue

        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue

        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue

        out.append(c)
        i += 1

    cleaned = "".join(out)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned


def parse_modinfo(raw_text: str) -> ModInfo:
    """Parse a modinfo.json string, tolerating comments/trailing commas."""
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return json.loads(strip_json_comments(raw_text))


def get_ci(d: dict[str, Any], *keys: str) -> Any | None:
    """Look up the first matching key in dict `d`, ignoring key case."""
    lowered = {k.lower(): v for k, v in d.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def get_dependencies(info: ModInfo) -> Dependencies:
    """Return a modinfo.json's declared mod dependencies, excluding "game"."""
    deps = get_ci(info, "dependencies") or {}
    if not isinstance(deps, dict):
        return {}
    return {k: v for k, v in deps.items() if k.lower() != "game"}


class LocalMod:
    """A mod found on disk, with metadata parsed from its modinfo.json."""

    def __init__(
        self,
        source: str,
        modid: str | None = None,
        version: str | None = None,
        name: str | None = None,
        error: str | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        """Store one local mod's file/folder name and parsed modinfo fields."""
        self.source: str = source
        self.modid: str | None = modid
        self.version: str | None = version
        self.name: str = name or modid or os.path.basename(source)
        self.error: str | None = error
        self.dependencies: Dependencies = dependencies or {}


class InstallPlanEntry(TypedDict):
    """One resolved mod in an install/dependency plan, ready to preview or apply."""

    modid: str
    name: str
    version: str | None
    release: Release
    data: bytes | None
    existing: list[LocalMod]
    requested: bool
    required_by: str | None


def modinfo_from_zip_source(zip_source: Union[str, BinaryIO]) -> str | None:
    """Read modinfo.json's raw text out of a zip file path or file-like object."""
    with zipfile.ZipFile(zip_source) as zf:
        candidates = [
            n for n in zf.namelist() if os.path.basename(n).lower() == "modinfo.json"
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda n: n.count("/"))
        with zf.open(candidates[0]) as f:
            return f.read().decode("utf-8-sig")


def read_modinfo_from_dir(path: str) -> str | None:
    """Read modinfo.json's raw text out of an unpacked mod folder."""
    modinfo_path = os.path.join(path, "modinfo.json")
    if not os.path.isfile(modinfo_path):
        for entry in os.listdir(path):
            if entry.lower() == "modinfo.json":
                modinfo_path = os.path.join(path, entry)
                break
        else:
            return None
    with open(modinfo_path, "r", encoding="utf-8-sig") as f:
        return f.read()


def scan_local_mods(mods_dir: str) -> list[LocalMod]:
    """Scan a Mods folder and return a LocalMod for every zip/folder mod found."""
    mods: list[LocalMod] = []
    for entry in sorted(os.listdir(mods_dir)):
        full_path = os.path.join(mods_dir, entry)

        if entry.lower().endswith(".zip") and os.path.isfile(full_path):
            try:
                raw = modinfo_from_zip_source(full_path)
            except (zipfile.BadZipFile, OSError) as e:
                mods.append(LocalMod(entry, error=f"could not read zip: {e}"))
                continue
            if raw is None:
                mods.append(LocalMod(entry, error="no modinfo.json found in zip"))
                continue
        elif os.path.isdir(full_path):
            try:
                raw = read_modinfo_from_dir(full_path)
            except OSError as e:
                mods.append(LocalMod(entry, error=f"could not read folder: {e}"))
                continue
            if raw is None:
                continue
        elif entry.lower().endswith(".cs") and os.path.isfile(full_path):
            continue
        elif entry.lower().endswith(".dll") and os.path.isfile(full_path):
            mods.append(
                LocalMod(entry, error="compiled mod without modinfo.json; skipped")
            )
            continue
        else:
            continue

        try:
            info = parse_modinfo(raw)
        except json.JSONDecodeError as e:
            mods.append(LocalMod(entry, error=f"invalid modinfo.json: {e}"))
            continue

        modid = get_ci(info, "modid", "modID")
        version = get_ci(info, "version", "modversion")
        name = get_ci(info, "name")

        if not modid or not version:
            mods.append(LocalMod(entry, error="modinfo.json missing modid/version"))
            continue

        mods.append(
            LocalMod(
                entry,
                modid=modid,
                version=version,
                name=name,
                dependencies=get_dependencies(info),
            )
        )

    return mods


def build_installed_index(local_mods: list[LocalMod]) -> dict[str, list[LocalMod]]:
    """Group local mods by lowercased modid, so duplicates are easy to spot."""
    index: dict[str, list[LocalMod]] = {}
    for mod in local_mods:
        if not mod.modid:
            continue
        index.setdefault(mod.modid.lower(), []).append(mod)
    return index


def fetch_mod_info(modid: str) -> tuple[ModInfo | None, str | None]:
    """Fetch a mod's full moddb record (name, releases, ...) by modid."""
    url = API_MOD_URL.format(modid=modid)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=15) as response:
            data = json.load(response)
    except HTTPError as e:
        if e.code == 404:
            return None, "not found on moddb"
        return None, f"http error {e.code}"
    except URLError as e:
        return None, f"network error: {e.reason}"

    if data.get("statuscode") not in ("200", 200):
        if data.get("statuscode") in ("404", 404):
            return None, "not found on moddb"
        return None, f"api returned status {data.get('statuscode')}"

    return data.get("mod", {}), None


def fetch_latest_release(modid: str) -> tuple[str | None, Release | None, str | None]:
    """Fetch a mod's name and newest release dict (mainfile, filename, modversion)."""
    mod, error = fetch_mod_info(modid)
    if error or mod is None:
        return None, None, error

    releases = mod.get("releases", [])
    if not releases:
        return mod.get("name"), None, "no releases published"

    return mod.get("name"), releases[0], None


def check_for_update(
    mod: LocalMod,
) -> tuple[LocalMod, Release | None, str | None]:
    """Look up the latest release for one local mod, for update-checking mode."""
    if mod.error:
        return mod, None, mod.error

    _, release, error = fetch_latest_release(mod.modid)
    return mod, release, error


def download_bytes(url: str) -> bytes:
    """Download a URL's full contents into memory."""
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return response.read()


def download_release(release: Release, mods_dir: str) -> str:
    """Download a release's mainfile into the mods folder and return its path."""
    filename = release.get("filename") or os.path.basename(
        release["mainfile"].split("?")[0]
    )
    content = download_bytes(release["mainfile"])

    dest_path = os.path.join(mods_dir, filename)
    with open(dest_path, "wb") as f:
        f.write(content)
    return dest_path


def remove_local_mod(mod: LocalMod, mods_dir: str) -> None:
    """Delete a previously-installed mod's zip file or unpacked folder."""
    full_path = os.path.join(mods_dir, mod.source)
    if os.path.isdir(full_path):
        shutil.rmtree(full_path)
    elif os.path.isfile(full_path):
        os.remove(full_path)


def apply_update(mod: LocalMod, release: Release, mods_dir: str, output_dir: str) -> str:
    """Download a mod's new release into output_dir, removing the copy it replaces.

    If output_dir is the same as mods_dir (the default), the mod's old
    file/folder in mods_dir is removed in place. Otherwise mods_dir is left
    untouched, and instead any older copy of this modid already sitting in
    output_dir (e.g. from a previous run) is removed so output_dir never
    accumulates duplicate versions of the same mod.
    """
    same_dir = os.path.abspath(output_dir) == os.path.abspath(mods_dir)
    if same_dir:
        stale, stale_dir = [mod], mods_dir
    else:
        stale = (
            [
                m
                for m in scan_local_mods(output_dir)
                if m.modid and mod.modid and m.modid.lower() == mod.modid.lower()
            ]
            if os.path.isdir(output_dir)
            else []
        )
        stale_dir = output_dir

    new_path = download_release(release, output_dir)

    for old in stale:
        old_path = os.path.join(stale_dir, old.source)
        if os.path.abspath(old_path) != os.path.abspath(new_path):
            remove_local_mod(old, stale_dir)

    return new_path


def resolve_mods_dir(path: str) -> str | None:
    """Resolve a user-supplied path to the actual Mods folder to scan."""
    expanded = os.path.expanduser(path)
    candidate = os.path.join(expanded, "Mods")
    if os.path.isdir(candidate):
        return candidate
    if os.path.isdir(expanded):
        return expanded
    return None


RequestedMod = Union[str, tuple[str, "str | None"]]


def resolve_install_plan(
    requested: list[RequestedMod],
    mods_dir: str,
    installed_index: dict[str, list[LocalMod]] | None = None,
) -> tuple[list[InstallPlanEntry], list[tuple[str, str]]]:
    """Resolve requested mods plus their full dependency tree into an install plan.

    `requested` is a list of modids, or (modid, required_by) tuples for
    seeding the plan with mods that are already known to be dependencies
    (required_by names what needs them; None means directly requested).

    Walks each mod's dependencies (read from its modinfo.json, which the
    moddb API doesn't expose) breadth-first, skipping anything already
    installed at the latest version. Returns (plan, errors); plan entries
    describe what would change without writing anything to disk yet, so the
    caller can show a preview before applying it.
    """
    if installed_index is None:
        installed_index = build_installed_index(scan_local_mods(mods_dir))

    plan: list[InstallPlanEntry] = []
    seen: set[str] = set()
    errors: list[tuple[str, str]] = []
    queue: list[tuple[str, str | None]] = [
        item if isinstance(item, tuple) else (item, None) for item in requested
    ]

    while queue:
        modid, required_by = queue.pop(0)
        key = modid.lower()
        if key in seen:
            continue
        seen.add(key)

        name, release, error = fetch_latest_release(modid)
        if error or release is None:
            errors.append((modid, error or "no releases published"))
            continue

        latest_version = release.get("modversion")
        existing_list = installed_index.get(key, [])
        current = next(
            (m for m in existing_list if m.version == latest_version), None
        )

        dependencies: Dependencies
        data: bytes | None

        if current is not None:
            data = None
            dependencies = current.dependencies
        else:
            try:
                data = download_bytes(release["mainfile"])
            except (HTTPError, URLError) as e:
                errors.append((modid, f"download failed: {e}"))
                continue
            try:
                raw = modinfo_from_zip_source(io.BytesIO(data))
                info = parse_modinfo(raw) if raw else {}
            except (zipfile.BadZipFile, json.JSONDecodeError):
                info = {}
            dependencies = get_dependencies(info)

        plan.append(
            InstallPlanEntry(
                modid=key,
                name=name or key,
                version=latest_version,
                release=release,
                data=data,
                existing=existing_list,
                requested=required_by is None,
                required_by=required_by,
            )
        )

        for dep_modid in dependencies:
            queue.append((dep_modid, modid))

    return plan, errors


def apply_install_plan(plan: list[InstallPlanEntry], mods_dir: str, output_dir: str) -> None:
    """Write each plan entry's downloaded bytes to output_dir, replacing old copies.

    If output_dir is the same as mods_dir (the default), each entry's
    `existing` copies (found in mods_dir while resolving the plan) are
    removed in place. Otherwise mods_dir is left untouched, and any older
    copy of the same modid already in output_dir is removed instead, so
    output_dir never accumulates duplicate versions of the same mod.
    """
    same_dir = os.path.abspath(output_dir) == os.path.abspath(mods_dir)
    output_index: dict[str, list[LocalMod]] = {}
    if not same_dir and os.path.isdir(output_dir):
        output_index = build_installed_index(scan_local_mods(output_dir))

    for entry in plan:
        if entry["data"] is None:
            continue

        if same_dir:
            stale, stale_dir = entry["existing"], mods_dir
        else:
            stale, stale_dir = output_index.get(entry["modid"], []), output_dir

        filename = entry["release"].get("filename") or f"{entry['modid']}.zip"
        dest_path = os.path.join(output_dir, filename)
        with open(dest_path, "wb") as f:
            f.write(entry["data"])

        for existing_mod in stale:
            old_path = os.path.join(stale_dir, existing_mod.source)
            if os.path.abspath(old_path) != os.path.abspath(dest_path):
                remove_local_mod(existing_mod, stale_dir)

        action = "Updated" if entry["existing"] else "Installed"
        print(f"[+] {action} {entry['name']} -> {entry['version']}")


def run_install(
    mods_dir: str, output_dir: str, requested_modids: list[str], auto_yes: bool
) -> None:
    """Resolve and (after confirmation) install the requested mods and dependencies."""
    print(f"Resolving {', '.join(requested_modids)} and dependencies...")
    if os.path.abspath(output_dir) != os.path.abspath(mods_dir):
        print(f"Output directory: {output_dir}")
    print()
    plan, errors = resolve_install_plan(list(requested_modids), mods_dir)

    for entry in plan:
        if entry["data"] is None:
            marker, status = "[=]", "already installed"
        elif entry["existing"]:
            old_versions = ", ".join(m.version or "?" for m in entry["existing"])
            marker, status = "[~]", f"{old_versions} -> {entry['version']}"
        else:
            marker, status = "[+]", f"install {entry['version']}"

        reason = "requested" if entry["requested"] else f"dependency of {entry['required_by']}"
        print(f"{marker} {entry['name']:<30} {status:<24} ({reason})")

    for modid, error in errors:
        print(f"[x] {modid:<30} {error}")

    to_apply = [e for e in plan if e["data"] is not None]
    if not to_apply:
        print("\nNothing to install; everything requested is already up to date.")
        return

    if auto_yes:
        proceed = True
    else:
        try:
            answer = input(f"\nInstall/update {len(to_apply)} mod(s)? [y/N] ")
        except EOFError:
            answer = ""
        proceed = answer.strip().lower() in ("y", "yes")

    if not proceed:
        print("No changes made.")
        return

    print()
    apply_install_plan(plan, mods_dir, output_dir)


def find_missing_dependencies(
    local_mods: list[LocalMod], installed_index: dict[str, list[LocalMod]]
) -> list[tuple[str, str]]:
    """Return (modid, required_by_name) pairs for dependencies nothing local satisfies."""
    missing: list[tuple[str, str]] = []
    seen: set[str] = set()
    for mod in local_mods:
        if mod.error:
            continue
        for dep_modid in mod.dependencies:
            key = dep_modid.lower()
            if key in installed_index or key in seen:
                continue
            seen.add(key)
            missing.append((dep_modid, mod.name))
    return missing


def run_check_updates(mods_dir: str, output_dir: str, auto_yes: bool) -> None:
    """Scan local mods, report available updates and missing dependencies, then apply."""
    local_mods = scan_local_mods(mods_dir)
    installed_index = build_installed_index(local_mods)

    print(f"Scanning mods in: {mods_dir} ({len(local_mods)} found)")
    if os.path.abspath(output_dir) != os.path.abspath(mods_dir):
        print(f"Output directory: {output_dir}")
    print()

    results: list[tuple[LocalMod, Release | None, str | None]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for mod, release, error in pool.map(check_for_update, local_mods):
            results.append((mod, release, error))

    pending_updates: list[tuple[LocalMod, Release]] = []
    up_to_date = 0
    problems = 0

    for mod, release, error in results:
        if error:
            print(f"[?] {mod.name:<30} {mod.version or 'n/a':<15} -- {error}")
            problems += 1
        elif release and release.get("modversion") != mod.version:
            print(f"[!] {mod.name:<30} {mod.version:<15} -> {release.get('modversion')}")
            pending_updates.append((mod, release))
        else:
            print(f"[=] {mod.name:<30} {mod.version:<15} (up to date)")
            up_to_date += 1

    print(
        f"\n{len(local_mods)} mods scanned: "
        f"{len(pending_updates)} update(s) available, {up_to_date} up to date, {problems} could not be checked"
    )

    missing = find_missing_dependencies(local_mods, installed_index)
    dep_plan: list[InstallPlanEntry] = []
    dep_errors: list[tuple[str, str]] = []
    if missing:
        dep_plan, dep_errors = resolve_install_plan(
            list(missing), mods_dir, installed_index
        )

    if dep_plan or dep_errors:
        print("\nDependency check:")
        for entry in dep_plan:
            marker, status = "[+]", f"install {entry['version']}"
            print(
                f"{marker} {entry['name']:<30} {status:<24} "
                f"(dependency of {entry['required_by']})"
            )
        for modid, error in dep_errors:
            print(f"[x] {modid:<30} {error}")

    to_install = [e for e in dep_plan if e["data"] is not None]

    total_changes = len(pending_updates) + len(to_install)
    if total_changes == 0:
        return

    if auto_yes:
        proceed = True
    else:
        try:
            answer = input(f"\nApply {total_changes} change(s)? [y/N] ")
        except EOFError:
            answer = ""
        proceed = answer.strip().lower() in ("y", "yes")

    if not proceed:
        print("No changes made.")
        return

    print()
    for mod, release in pending_updates:
        try:
            new_path = apply_update(mod, release, mods_dir, output_dir)
        except (HTTPError, URLError, OSError) as e:
            print(f"[x] Failed to update {mod.name}: {e}", file=sys.stderr)
            continue
        print(f"[+] Updated {mod.name} -> {release.get('modversion')} ({os.path.basename(new_path)})")

    apply_install_plan(dep_plan, mods_dir, output_dir)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the updater CLI."""
    parser = argparse.ArgumentParser(
        description="Check installed Vintage Story mods for updates, or install new ones."
    )
    parser.add_argument(
        "-p",
        "--path",
        default=DEFAULT_DATA_PATH,
        help=f"Path to VintagestoryData or its Mods folder (default: {DEFAULT_DATA_PATH})",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="DIR",
        help="Directory to write downloaded/updated mod files to "
        "(default: same folder as --path)",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Don't prompt for confirmation before applying changes",
    )
    parser.add_argument(
        "-i",
        "--install",
        nargs="+",
        metavar="MODID",
        help="Install one or more mods (and their dependencies) by mod id, "
        "instead of checking for updates",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point: dispatch to install mode or update-checking mode."""
    args = parse_args()

    mods_dir = resolve_mods_dir(args.path)
    if mods_dir is None:
        print(f"Could not find a mods folder at: {args.path}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_dir = os.path.expanduser(args.output)
        os.makedirs(output_dir, exist_ok=True)
    else:
        output_dir = mods_dir

    if args.install:
        run_install(mods_dir, output_dir, args.install, args.yes)
    else:
        run_check_updates(mods_dir, output_dir, args.yes)


if __name__ == "__main__":
    main()
