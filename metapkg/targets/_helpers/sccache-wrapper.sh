#!/usr/bin/env bash

set -e -E -u -o pipefail -o noclobber -o noglob +o braceexpand || exit 1
trap 'printf "[sccache-wrapper] [ee]  failed:  %s\\n" "${BASH_COMMAND}" >&2 ; exit 1' ERR || exit 1

if test "${#}" -eq 0 ; then
  printf '[sccache-wrapper] [ee]  expected arguments;  aborting!\n' >&2
  exit 99
fi

_sccache_bin="$SCCACHE"
if [ -z "$_sccache_bin" ]; then
    printf '[sccache-wrapper] [ee]  SCCACHE is not set in the environment;  aborting!\n' >&2
    exit 99
fi

test -f "${_sccache_bin}"
test -x "${_sccache_bin}"

case "${0}" in

  ( sccache | */sccache )
    export SCCACHE_WRAPPER=1
    exec "$_sccache_bin" "${@}"
  ;;

  ( cc | c++ | gcc | g++ | clang | clang++ | rustc )
    _delegate="${0}"
  ;;

  ( */cc | */c++ | */gcc | */g++ | */clang | */clang++ | */rustc )
    _delegate="${0##*/}"
  ;;

  ( * )
    printf '[%08d] [sccache-wrapper] [ee]  invalid tool "%s";  aborting!\n' "${$}" "${0}" >&2
    exit 99
  ;;
esac

function canonicalize_dir() {
    if [ -d "$1" ]; then
        (cd "$1"; echo "$PWD")
    else
        echo $1
    fi
}

CMD_PATH="$0"
[[ "$CMD_PATH" != /* ]] && CMD_PATH="$PWD/$CMD_PATH"
CMD_DIRNAME=$(canonicalize_dir "$(dirname "$CMD_PATH")")

IFS=":" read -ra cur_path <<< "${PATH:-/usr/bin}"
filtered_path=()

for item in "${cur_path[@]}"; do
    if [[ "$(canonicalize_dir "$item")" != "$CMD_DIRNAME" ]]; then
        filtered_path+=("$item")
    fi
done

export PATH=$(IFS=:; echo "${filtered_path[*]}")

exec "${_delegate}" "${@}"
