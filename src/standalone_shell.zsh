# Standalone vendor commands. No Jello runtime is needed.
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
