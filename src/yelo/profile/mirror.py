"""Keep each Claude account home equal to the shared home except for local state."""

import json
import os
import shutil


EXCLUDED = {".profiles", ".claude.json", "email"}
LOCAL_PREFIXES = (".usage-cache", ".usage-api-cache")


def excluded(name):
    return name in EXCLUDED or name.startswith(LOCAL_PREFIXES)


def directory(name, home):
    return os.path.join(home, ".claude", ".profiles", name)


def shared_names(home):
    root = os.path.join(home, ".claude")
    try:
        with os.scandir(root) as entries:
            return sorted(entry.name for entry in entries if not excluded(entry.name))
    except FileNotFoundError:
        return []


def issues(name, home):
    """Return ``(kind, path)`` rows without changing the profile."""
    profile = directory(name, home)
    if not os.path.isdir(profile):
        return [("missing", profile)]

    names = set(shared_names(home))
    found = []
    for item in sorted(names):
        path = os.path.join(profile, item)
        target = os.path.join("..", "..", item)
        if not os.path.lexists(path):
            found.append(("missing", path))
        elif os.path.islink(path):
            if os.readlink(path) != target:
                found.append(("stale", path))
        else:
            found.append(("drift", path))

    for item in sorted(os.listdir(profile)):
        if excluded(item) or item in names:
            continue
        found.append(("drift", os.path.join(profile, item)))

    config = os.path.join(profile, ".claude.json")
    if not os.path.lexists(config):
        found.append(("missing", config))
    elif os.path.islink(config) or not os.path.isfile(config):
        found.append(("drift", config))
    return found


def sync(name, home):
    """Create shared links and the account config, and return paths with data drift."""
    profile = directory(name, home)
    existed = os.path.isdir(profile)
    os.makedirs(profile, mode=0o700, exist_ok=True)
    if not existed:
        os.chmod(profile, 0o700)

    names = shared_names(home)
    drift = []
    for item in names:
        path = os.path.join(profile, item)
        target = os.path.join("..", "..", item)
        if os.path.islink(path):
            if os.readlink(path) == target:
                continue
            os.unlink(path)
        elif os.path.lexists(path):
            drift.append(path)
            continue
        os.symlink(target, path)

    for item in sorted(os.listdir(profile)):
        if excluded(item) or item in names:
            continue
        drift.append(os.path.join(profile, item))

    config = os.path.join(profile, ".claude.json")
    source = os.path.join(home, ".claude.json")
    if not os.path.lexists(config) and os.path.isfile(source):
        try:
            with open(source, encoding="utf-8") as handle:
                data = json.load(handle)
        except (UnicodeError, ValueError):
            shutil.copyfile(source, config)
        else:
            if isinstance(data, dict):
                data.pop("oauthAccount", None)
            with open(config, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
                handle.write("\n")
        os.chmod(config, 0o600)
    return sorted(set(drift))
