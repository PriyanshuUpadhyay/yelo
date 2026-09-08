# Installed by yelo profile integration.
# Selection runs from a self-contained archive, independent of the Yelo installation.
_cprofile_valid_name() {
  case "$1" in ''|[![:alnum:]]*|*[![:alnum:]_.-]*) return 1 ;; esac
}

# Both CLIs resolve names, aliases, emails, and unique substrings through the same code.
_yelo_profiles() {
  if [[ "$1" == create ]]; then
    command yelo profile "$@"
  else
    command python3 -I __RUNTIME_PATH__ "$@"
  fi
}

# Prints "<canonical name>\t<home dir>"; the name is empty for a codex home with no label yet.
_yelo_profile_resolve() { # $1 = claude|codex  $2 = query
  _yelo_profiles resolve --cli "$1" -- "$2"
}

# Numbered menu on stderr, choice read from stdin, same output as _yelo_profile_resolve.
# Callers must check both TTYs first: the menu is unreadable in a pipe and the read would hang.
_yelo_profile_pick() { # $1 = claude|codex
  local cli="$1" menu reply n line model
  shift
  local -a names dirs texts fields model_args=()
  if [[ "$cli" == claude ]]; then
    model="$(_yelo_profiles model "$@")" || return 1
    model_args=(--model "$model")
  fi
  menu="$(_yelo_profiles menu --cli "$cli" "${model_args[@]}")" || return 1
  # (ps:\t:) keeps empty fields, which `read` would collapse — a codex home with no label
  # yet has an empty name and would otherwise shift its dir into the name.
  for line in "${(f)menu}"; do
    fields=("${(@ps:\t:)line}")
    names+=("${fields[2]}"); dirs+=("${fields[3]}"); texts+=("${fields[4]}")
  done
  (( $#texts )) || { print -u2 "$cli: no profiles found"; return 1; }
  print -u2 -r -- "$cli: select an account"
  for n in {1..$#texts}; do print -u2 -r -- "  $n) ${texts[$n]}"; done
  print -n -u2 "choice: "
  IFS= read -r reply || { print -u2; return 1; }
  case "$reply" in ''|*[!0-9]*) print -u2 "$cli: no account selected"; return 1 ;; esac
  (( reply >= 1 && reply <= $#texts )) || { print -u2 "$cli: invalid choice: $reply"; return 1; }
  print -r -- "${names[$reply]}"$'\t'"${dirs[$reply]}"
}

_cprofile_manage() {
  local action="${1:-}" name
  [ "$#" -gt 0 ] && shift
  case "$action" in
    list)
      [ "$#" -eq 0 ] || { print -u2 "usage: claude profile list"; return 2; }
      _yelo_profiles list --cli claude --usage
      ;;
    create)
      # Creation stays with the installer; put the name after -- so it cannot become a flag.
      if [ "$#" -eq 0 ]; then
        _yelo_profiles create --cli claude
      else
        name="$1"; shift
        _yelo_profiles create --cli claude "$@" -- "$name"
      fi
      ;;
    *)
      print -u2 "usage: claude profile {list|create NAME [--email ADDR] [--yes]}"
      return 2
      ;;
  esac
}

_claude_launch_kind() {
  local x
  while [ "$#" -gt 0 ]; do
    x="$1"; shift
    case "$x" in
      --) print session; return ;;
      -h|--help|-v|--version) print plumbing; return ;;
      --model|--effort|--add-dir|--session-id|-n|--name|--resume|-r|--continue|-c|\
      --permission-mode|--allowedTools|--disallowedTools|--mcp-config|--system-prompt|\
      --append-system-prompt|--output-format|--input-format)
        shift 2>/dev/null || break
        ;;
      --*=*|-*) ;;
      auth) print bound; return ;;
      update|upgrade|install|doctor|mcp|plugin|help) print plumbing; return ;;
      *) print session; return ;;
    esac
  done
  print session
}

_cprofile() {
  local profile= x seen_profile=0 after_dd=0 index=1
  local resume_id= expect_resume=0
  local -a args tokens
  if [ "${1:-}" = profile ]; then
    shift
    _cprofile_manage "$@"
    return
  fi
  tokens=("$@")
  while (( index <= $#tokens )); do
    x="${tokens[$index]}"; (( index++ ))
    if (( after_dd )); then
      args+=("$x")
      continue
    fi
    # A token that is a flag (or absent) is not a resume id — `--resume` alone opens the
    # picker. Ids are only captured here, before `--`, so positional args are never scanned.
    # Each `--resume` clears the previous id (below), so a later idless one wins as "no id".
    if (( expect_resume )); then
      expect_resume=0
      case "$x" in
        -*) ;;
        ''|*[!A-Za-z0-9-]*) ;;
        *) resume_id="$x" ;;
      esac
    fi
    case "$x" in
      --) after_dd=1; args+=("$x") ;;
      --profile)
        (( seen_profile )) && { print -u2 "claude: --profile specified more than once"; return 2; }
        seen_profile=1
        # A following flag (or nothing at all) is not a value: `--profile` on its own means
        # "let me choose", so the next token stays in the loop and is parsed normally.
        if (( index <= $#tokens )) && [[ "${tokens[$index]}" != -* ]]; then
          profile="${tokens[$index]}"; (( index++ ))
        fi
        ;;
      --profile=*)
        (( seen_profile )) && { print -u2 "claude: --profile specified more than once"; return 2; }
        profile="${x#*=}"; seen_profile=1
        ;;
      --resume) resume_id=; expect_resume=1; args+=("$x") ;;
      --resume=*)
        resume_id=
        case "${x#*=}" in ''|*[!A-Za-z0-9-]*) ;; *) resume_id="${x#*=}" ;; esac
        args+=("$x")
        ;;
      *) args+=("$x") ;;
    esac
  done
  # Every source below goes through the resolver, so an old name kept by a session map or a
  # running pane still reaches its renamed account, and an email or unique substring works too.
  local resolved= kind resuming=0
  for x in "${args[@]}"; do
    case "$x" in --) break ;; --resume|--resume=*|-r|--continue|-c) resuming=1 ;; esac
  done
  kind="$(_claude_launch_kind "${args[@]}")"
  if [ -n "$profile" ]; then
    resolved="$(_yelo_profile_resolve claude "$profile")" || return 2
  elif (( seen_profile )); then
    # Valueless --profile: an explicit request to choose, so no other source applies.
    [ -t 0 ] && [ -t 1 ] || { print -u2 "claude: --profile requires a name"; return 2; }
    resolved="$(_yelo_profile_pick claude "${args[@]}")" || return 2
  fi
  # Existing session maps retain the owning account across terminal restores.
  # They outrank the ambient account; an invalid map falls through to an explicit choice.
  if [ -z "$resolved" ] && [ -n "$resume_id" ]; then
    local map_file="$HOME/.claude/.profiles/.session-map/$resume_id" mapped=
    if [ -f "$map_file" ] && [ ! -L "$map_file" ]; then
      IFS= read -r mapped < "$map_file" 2>/dev/null || :
      if _cprofile_valid_name "$mapped"; then
        resolved="$(_yelo_profile_resolve claude "$mapped" 2>/dev/null)"
        [ -z "$resolved" ] || touch "$map_file" 2>/dev/null  # keeps the prune off a live session
      fi
    fi
  fi
  if [ -z "$resolved" ] && [[ -n "${CLAUDE_PROFILE_DIR:-}${CLAUDE_CONFIG_DIR:-}" ]]; then
    _claude_binary "${args[@]}"
    return
  fi
  # Still nothing → inherit the spawning session's profile (agent panes/subshells carry this env).
  if [ -z "$resolved" ] && [ -n "${AGENT_PROFILE_LABEL:-}" ]; then
    resolved="$(_yelo_profile_resolve claude "$AGENT_PROFILE_LABEL")" || return 2
  fi
  if [ -z "$resolved" ] && [ "$kind" = plumbing ]; then
    _claude_binary "${args[@]}"
    return
  fi
  if [ -z "$resolved" ] && [ "$kind" = bound ]; then
    [ -t 0 ] && [ -t 1 ] || {
      print -u2 "claude: auth needs --profile NAME; run 'claude profile list' to see accounts"
      return 2
    }
    resolved="$(_yelo_profile_pick claude "${args[@]}")" || return 2
  fi
  if [ -z "$resolved" ] && (( resuming )); then
    [[ -t 0 && -t 1 ]] || { print -u2 "claude: resume needs --profile NAME"; return 2; }
    resolved="$(_yelo_profile_pick claude "${args[@]}")" || return 2
  fi
  # Nothing named this account, so spend the one about to waste the most usage rather than always
  # the same one. A pick that cannot be judged is an error, never a default profile.
  local autopick=
  if [ -z "$resolved" ]; then
    local startup_model
    startup_model="$(_yelo_profiles model "${args[@]}")" || return 2
    resolved="$(_yelo_profiles pick --cli claude --model "$startup_model")" || {
      print -u2 "claude: --profile NAME is required; run 'claude profile list' to see available profiles"
      return 2
    }
    autopick=1
  fi

  local profile_dir= fields
  fields=("${(@ps:\t:)resolved}")
  profile="${fields[1]}"; profile_dir="${fields[2]}"
  _cprofile_valid_name "$profile" || {
    print -u2 "claude: invalid profile name: $profile"
    return 2
  }
  [ -z "$autopick" ] || print -u2 "claude: using profile '$profile' (expiring usage first; --profile overrides)"

  # Every profile names its Keychain identity by path string; the path need not exist. No
  # account rides the bare default service, so no rename can silently swap one account's
  # credentials for another's.
  AGENT_PROFILE_LABEL="$profile" CLAUDE_PROFILE_DIR="$profile_dir" \
    CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude-$profile" \
    _claude_binary "${args[@]}"
}

_claude_binary() { command claude "$@"; }

if ! alias claude >/dev/null 2>&1 && { ! typeset -f claude >/dev/null 2>&1 ||
     [[ "$(typeset -f claude)" == *"jello-agent --shell claude"* || "$(typeset -f claude)" == *'_cprofile "$@"'* ]]; }; then
  function claude { _cprofile "$@"; }
fi

# Codex account profiles: --profile NAME swaps CODEX_HOME (auth + sessions). The base account
# at ~/.codex is named by its profile-label file; every other account lives at ~/.codex-NAME
# The profile helpers discover this layout. Names, emails, and unique substrings all resolve
# through the installed selector — no account is the hardcoded default. Upstream --profile (config
# overlay) is shadowed by this; use -p for the upstream flag.
# Sessions live inside the home, so the Codex picker only ever sees one account's; `codex profile
# sessions` is the listing that spans them.
_codexprofile_manage() {
  local action="${1:-}" name
  [ "$#" -gt 0 ] && shift
  case "$action" in
    list)
      [ "$#" -eq 0 ] || { print -u2 "usage: codex profile list"; return 2; }
      _yelo_profiles list --cli codex --usage
      ;;
    create)
      if [ "$#" -eq 0 ]; then
        _yelo_profiles create --cli codex
      else
        name="$1"; shift
        _yelo_profiles create --cli codex "$@" -- "$name"
      fi
      ;;
    sessions)
      local -a opts=()
      while [ "$#" -gt 0 ]; do
        case "$1" in
          --all|--json) opts+=("$1"); shift ;;
          --limit)
            case "${2:-}" in
              ''|*[!0-9]*) print -u2 "codex: --limit needs a number"; return 2 ;;
            esac
            opts+=(--limit "$2"); shift 2
            ;;
          *)
            print -u2 "usage: codex profile sessions [--all] [--limit N] [--json]"
            return 2
            ;;
        esac
      done
      _yelo_profiles sessions --cli codex "${opts[@]}"
      ;;
    *)
      print -u2 "usage: codex profile {list|create NAME [--yes]|sessions [--all] [--limit N] [--json]}"
      return 2
      ;;
  esac
}

# The subcommand is the first token that is not a global option or the value of one — `codex -C
# /tmp resume` is a resume, not a session start. Options are skipped by name because only the
# binary knows which ones take a value; an unknown flag is assumed to take none, and `--flag=x`
# never consumes the next token. `-h`/`--help`/`-V`/`--version` are reported as themselves so the
# caller can treat them like the plumbing subcommands they behave as.
_codex_subcommand() {
  local x
  while [ "$#" -gt 0 ]; do
    x="$1"; shift
    case "$x" in
      --) break ;;
      -h|--help|-V|--version) print -r -- "$x"; return ;;
      -c|--config|-i|--image|-m|--model|-p|--profile|-s|--sandbox|-a|--ask-for-approval|\
      -C|--cd|--add-dir|--enable|--disable|--remote|--remote-auth-token-env|--local-provider)
        shift 2>/dev/null || break
        ;;
      -*) ;;
      *) print -r -- "$x"; return ;;
    esac
  done
}

# The session a `resume`/`fork`/`archive`/`unarchive`/`delete` names — the second positional
# when it is a UUID, or `--last` — resolved to the account whose home holds it, in the resolve
# format. Empty output with success means nothing was named, so the caller has to ask. Options
# are skipped the way _codex_subcommand skips them; a session *name* is left to Codex.
_codex_session_owner() {
  local x positional=0 session= last= all=
  while [ "$#" -gt 0 ]; do
    x="$1"; shift
    case "$x" in
      --) break ;;
      --last) last=1 ;;
      --all) all=1 ;;
      -c|--config|-i|--image|-m|--model|-p|--profile|-s|--sandbox|-a|--ask-for-approval|\
      -C|--cd|--add-dir|--enable|--disable|--remote|--remote-auth-token-env|--local-provider)
        shift 2>/dev/null || break
        ;;
      -*) ;;
      *) (( ++positional == 2 )) && session="$x" ;;
    esac
  done
  if [[ "$session" =~ '^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$' ]]; then
    _yelo_profiles owner --cli codex -- "$session"
  elif [[ -n "$last" ]]; then
    _yelo_profiles owner --cli codex ${all:+--all} --last
  fi
}

# The wrapper shadows the binary's own name, so the lookup has to search PATH only:
# `command -v codex` would answer with this function and the fallback could never be
# reached. `whence -p` is that PATH-only lookup.
_codex_binary() {
  local bin
  bin="$(whence -p codex 2>/dev/null)"
  command "${bin:-/opt/homebrew/bin/codex}" "$@"
}

if ! alias codex >/dev/null 2>&1 && { ! typeset -f codex >/dev/null 2>&1 ||
     [[ "$(typeset -f codex)" == *"jello-agent --shell codex"* || "$(typeset -f codex)" == *'_codex_host_guard "$subcommand" "$@"'* || "$(typeset -f codex)" == *'_codex_binary "${args[@]}"'* ]]; }; then
function codex {
  if [[ "${1:-}" == "profile" ]]; then
    shift
    _codexprofile_manage "$@"
    return
  fi

  local x profile= seen_profile=0 after_dd=0 index=1
  local -a args tokens=("$@")
  while (( index <= $#tokens )); do
    x="${tokens[$index]}"; (( index++ ))
    if (( after_dd )); then
      args+=("$x")
      continue
    fi
    case "$x" in
      --) after_dd=1; args+=("$x") ;;
      --profile)
        (( seen_profile )) && { print -u2 "codex: --profile specified more than once"; return 2; }
        seen_profile=1
        # A following flag (or nothing at all) is not a value: `--profile` on its own means
        # "let me choose", so the next token stays in the loop and is parsed normally.
        if (( index <= $#tokens )) && [[ "${tokens[$index]}" != -* ]]; then
          profile="${tokens[$index]}"; (( index++ ))
        fi
        ;;
      --profile=*)
        (( seen_profile )) && { print -u2 "codex: --profile specified more than once"; return 2; }
        profile="${x#*=}"
        seen_profile=1
        ;;
      *) args+=("$x") ;;
    esac
  done

  # Which account this invocation belongs to. An explicit --profile always wins and always
  # exports the home, base included; a valueless --profile means "let me choose". Without the
  # flag a session-starting run auto-picks the account about to waste the most usage — nothing
  # silently lands on the base account any more. Account-bound subcommands read or write
  # sessions and credentials that belong to one home: a session named on the command line
  # selects the home that holds it, and everything else asks. Plumbing subcommands keep
  # running on the binary's own default home. A caller-set CODEX_HOME stays untouched.
  local resolved="" autopick="" subcommand=""
  subcommand="$(_codex_subcommand "${args[@]}")"
  if [[ -n "$profile" ]]; then
    resolved="$(_yelo_profile_resolve codex "$profile")" || return 2
  elif (( seen_profile )); then
    [[ -t 0 && -t 1 ]] || { print -u2 "codex: --profile requires a name"; return 2; }
    resolved="$(_yelo_profile_pick codex)" || return 2
  elif [[ -z "${CODEX_HOME:-}" ]]; then
    case "$subcommand" in
      --help|-h|--version|-V|mcp|app-server|completion|debug|cloud|features|apply|update|doctor|remote-control|exec-server) ;;
      login|logout|resume|fork|archive|unarchive|delete)
        resolved="$(_codex_session_owner "${args[@]}")" || return 2
        if [[ -n "$resolved" ]]; then
          autopick="owns the session; --profile overrides"
        else
          [[ -t 0 && -t 1 ]] || {
            print -u2 "codex: $subcommand needs --profile NAME; run 'codex profile list' to see accounts"
            return 2
          }
          resolved="$(_yelo_profile_pick codex)" || return 2
        fi
        ;;
      *)
        resolved="$(_yelo_profiles pick --cli codex)" || {
          print -u2 "codex: no account could be picked; pass --profile NAME"
          return 2
        }
        autopick="expiring usage first; --profile overrides"
        ;;
    esac
  fi

  if [[ -n "$resolved" ]]; then
    # (ps:\t:) keeps the empty name field a codex home with no profile-label yet produces.
    local profile_name profile_dir
    local -a fields=("${(@ps:\t:)resolved}")
    profile_name="${fields[1]}"; profile_dir="${fields[2]}"
    local -x CODEX_HOME="$profile_dir" CODEX_CONFIG_PATH="$profile_dir/config.toml"
    [[ -n "$autopick" ]] && print -u2 "codex: using profile '${profile_name:-${profile_dir:t}}' ($autopick)"
  fi

  # Launch policy belongs to the host, not to yelo: the Herdr checks and the Codex config
  # baseline the dotfiles wrapper ran here are whatever defines _codex_host_guard. yelo
  # never defines it, and without it the binary runs directly. The guard classifies by the
  # scanned subcommand, not args[1], so a global option in front of it (`codex -C /tmp
  # exec …`) cannot walk past the check.
  if (( $+functions[_codex_host_guard] )); then
    _codex_host_guard "$subcommand" "${args[@]}" || return
  fi

  _codex_binary "${args[@]}"
}
fi
