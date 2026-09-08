#!/usr/bin/env python3

import argparse
import copy
import fcntl
import json
import os
import re
import stat
import subprocess
import tempfile

DEFAULT_ROOTS = (
    "/private/tmp/councils",
    f"/private/tmp/claude-{os.getuid()}",
    os.path.expanduser("~/.herdr/runs"),
)


def atomic_write(path, content):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o600
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def canonical_directory(path):
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise ValueError(f"not a directory: {path}")
    return real


def configured_roots():
    override = os.environ.get("AGENT_TRUST_ROOTS")
    values = override.split(os.pathsep) if override is not None else DEFAULT_ROOTS
    return tuple(os.path.realpath(os.path.expanduser(value)) for value in values if value)


def containing_root(path, roots):
    for root in roots:
        try:
            inside = os.path.commonpath((root, path)) == root
        except ValueError:
            inside = False
        if inside and path != root:
            return root
    return None


def validate_generated_path(root, path):
    relative = os.path.relpath(path, root)
    current = root
    candidates = [root]
    for part in relative.split(os.sep):
        current = os.path.join(current, part)
        candidates.append(current)
    for candidate in candidates:
        info = os.stat(candidate)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError(
                "generated trust paths must be user-owned and not group/world-writable: "
                f"{candidate}"
            )


def codex_trust_target(workspace, root):
    workspace = canonical_directory(workspace)
    root = os.path.realpath(root)
    result = subprocess.run(
        ["git", "-C", workspace, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    target = canonical_directory(result.stdout.strip()) if result.returncode == 0 else workspace
    if containing_root(target, (root,)) != root:
        raise ValueError(f"Codex Git root is outside the approved generated root: {target}")
    validate_generated_path(root, target)
    return target


def trust_codex(path, workspace):
    try:
        with open(path) as handle:
            content = handle.read()
    except FileNotFoundError:
        content = ""
    header = f"[projects.{json.dumps(workspace)}]"
    section = re.search(rf"(?m)^{re.escape(header)}[ \t]*$", content)
    if not section:
        content = content.rstrip() + f'\n\n{header}\ntrust_level = "trusted"\n'
    else:
        next_section = re.search(r"(?m)^\[", content[section.end():])
        end = section.end() + next_section.start() if next_section else len(content)
        body = content[section.end():end]
        trust = re.search(r"(?m)^trust_level[ \t]*=[^\n]*$", body)
        if trust and re.fullmatch(
            r'trust_level[ \t]*=[ \t]*"trusted"[ \t]*(?:#.*)?', trust.group()
        ):
            return False
        if trust:
            body = body[:trust.start()] + 'trust_level = "trusted"' + body[trust.end():]
        else:
            body = "\ntrust_level = \"trusted\"" + body
        content = content[:section.end()] + body + content[end:]
    atomic_write(path, content)
    return True


def load_json(path):
    try:
        with open(path) as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def merge_baseline(runtime, baseline, keys=()):
    for key, baseline_value in baseline.items():
        if not keys and key == "trustedWorkspaces":
            continue
        runtime_value = runtime.get(key)
        if isinstance(baseline_value, dict) and isinstance(runtime_value, dict):
            merge_baseline(runtime_value, baseline_value, (*keys, key))
        elif (*keys, key) == ("permissions", "allow") and isinstance(runtime_value, list):
            runtime[key] = list(dict.fromkeys([*baseline_value, *runtime_value]))
        else:
            runtime[key] = copy.deepcopy(baseline_value)


def trust_agy(path, workspace, baseline_path=None):
    settings = load_json(path)
    before = copy.deepcopy(settings)
    if baseline_path and os.path.exists(baseline_path):
        merge_baseline(settings, load_json(baseline_path))
    if workspace is not None:
        trusted = settings.setdefault("trustedWorkspaces", [])
        if not isinstance(trusted, list):
            raise ValueError(f"trustedWorkspaces must be a list: {path}")
        if workspace not in trusted:
            trusted.append(workspace)
    if settings == before:
        return False
    atomic_write(path, json.dumps(settings, indent=2) + "\n")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Pre-trust safe generated work directories for Codex or AGY."
    )
    parser.add_argument("--provider", required=True, choices=("codex", "agy"))
    parser.add_argument("--cwd", required=True)
    args = parser.parse_args()

    try:
        workspace = canonical_directory(args.cwd)
        root = containing_root(workspace, configured_roots())
        if root:
            validate_generated_path(root, workspace)
    except ValueError as error:
        parser.error(str(error))

    codex_config = os.path.expanduser(
        os.environ.get("CODEX_CONFIG_PATH", "~/.codex/config.toml")
    )
    agy_settings = os.path.expanduser(
        os.environ.get("AGY_SETTINGS_PATH", "~/.gemini/antigravity-cli/settings.json")
    )
    agy_baseline = os.path.expanduser(
        os.environ.get(
            "AGY_SETTINGS_BASELINE",
            "~/dotfiles/home/.gemini/antigravity-cli/settings.json",
        )
    )
    lock_path = os.path.expanduser(
        os.environ.get("AGENT_CWD_TRUST_LOCK", "~/.cache/ensure-agent-cwd-trust.lock")
    )
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)

    try:
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.provider == "agy":
                changed = trust_agy(agy_settings, workspace if root else None, agy_baseline)
                target = workspace if root else None
            elif root:
                target = codex_trust_target(workspace, root)
                changed = trust_codex(codex_config, target)
            else:
                target = None
                changed = False
    except ValueError as error:
        parser.error(str(error))

    print(json.dumps({
        "provider": args.provider,
        "managed": target is not None,
        "target": target,
        "changed": changed,
    }))


if __name__ == "__main__":
    main()
