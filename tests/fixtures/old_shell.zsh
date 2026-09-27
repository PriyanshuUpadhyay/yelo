# Installed by jello simple setup
if ! alias claude >/dev/null 2>&1 &&
   { ! typeset -f claude >/dev/null 2>&1 ||
      [[ "$(typeset -f claude)" == *"jello-agent --shell claude"* ]]; }; then
    eval 'claude() {
    local jello_launch
    jello_launch="$(command jello-agent --shell claude "$@")" || return
    ( eval "$jello_launch" )
}'
else
    printf '%s\n' 'jello: kept existing claude alias/function. Use jello-agent claude alongside it, or eval "$(jello shell-init --replace)" to save and replace this shell definition.' >&2
fi
if ! alias codex >/dev/null 2>&1 &&
   { ! typeset -f codex >/dev/null 2>&1 ||
      [[ "$(typeset -f codex)" == *"jello-agent --shell codex"* ]]; }; then
    eval 'codex() {
    local jello_launch
    jello_launch="$(command jello-agent --shell codex "$@")" || return
    ( eval "$jello_launch" )
}'
else
    printf '%s\n' 'jello: kept existing codex alias/function. Use jello-agent codex alongside it, or eval "$(jello shell-init --replace)" to save and replace this shell definition.' >&2
fi
