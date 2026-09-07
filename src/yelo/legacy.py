"""Migrate the old installed shell bridge without a runtime dependency on Yelo."""

import os


def old_shell():
    body = "# Installed by jello simple setup\n"
    for cli in ("claude", "codex"):
        body += f'''if ! alias {cli} >/dev/null 2>&1 &&
   {{ ! typeset -f {cli} >/dev/null 2>&1 ||
      [[ "$(typeset -f {cli})" == *"jello-agent --shell {cli}"* ]]; }}; then
    eval '{cli}() {{
    local jello_launch
    jello_launch="$(command jello-agent --shell {cli} "$@")" || return
    ( eval "$jello_launch" )
}}'
else
    printf '%s\\n' 'jello: kept existing {cli} alias/function. Use jello-agent {cli} alongside it, or eval "$(jello shell-init --replace)" to save and replace this shell definition.' >&2
fi
'''
    return body


STANDALONE_SHELL = '''# Standalone vendor commands. No Jello runtime is needed.
# Clear only the old Jello functions when this file is sourced in an existing shell.
if [[ "$(typeset -f claude)" == *"jello-agent --shell claude"* ]]; then
  unfunction claude
fi
if [[ "$(typeset -f codex)" == *"jello-agent --shell codex"* ]]; then
  unfunction codex
fi

# Host policy stays in the user's shell configuration. Preserve its launch check.
if typeset -f _codex_host_guard >/dev/null 2>&1 &&
   ! alias codex >/dev/null 2>&1 && ! typeset -f codex >/dev/null 2>&1; then
  function codex {
    local subcommand="" arg
    local -i index=1
    while (( index <= $# )); do
      arg="${@[index]}"
      case "$arg" in
        --)
          (( index++ ))
          subcommand="${@[index]}"
          break ;;
        -c|--config|--enable|--disable|-i|--image|-m|--model|--local-provider|-p|--profile|-s|--sandbox|-a|--ask-for-approval|-C|--cd|--add-dir)
          (( index += 2 )) ;;
        -*) (( index++ )) ;;
        *) subcommand="$arg"; break ;;
      esac
    done
    [[ "$subcommand" == e ]] && subcommand=exec
    _codex_host_guard "$subcommand" "$@" || return
    command codex "$@"
  }
fi
'''


def shell_path(home):
    root = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(root, "yelo", "shell.sh")
