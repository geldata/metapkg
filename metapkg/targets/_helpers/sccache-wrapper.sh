#!/usr/bin/env bash

set -e -E -u -o pipefail -o noclobber -o noglob +o braceexpand || exit 1
trap 'printf "[sccache-wrapper] [ee]  failed:  %s\\n" "${BASH_COMMAND}" >&2 ; exit 1' ERR || exit 1

if test "${#}" -eq 0 ; then
  printf '[sccache-wrapper] [ee]  expected arguments;  aborting!\n' >&2
  exit 99
fi

_sccache_bin="$SCCACHE"
if [ -z "$_sccache_bin" ]; then
    printf 'SCCACHE is not set in the environment;  aborting!\n' >&2
     exit 99
fi

test -f "${_sccache_bin}"
test -x "${_sccache_bin}"

case "${0}" in

  ( sccache | */sccache | sccache-wrapper | */sccache-wrapper )
    test "${#}" -ge 1
    case "${1}" in
      ( sccache | */sccache | sccache-wrapper | */sccache-wrapper )
        shift 1
        exec "${0}" "${@}"
      ;;
    esac
    _delegate="${1}"
    shift 1
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

if test "/proc/${PPID}/exe" -ef "${_sccache_bin}" ; then
  IFS=":" read -ra _paths <<< "${PATH:-/usr/bin}"

  _delegate_bin=''
  for _path in "${_paths[@]}" ; do
    if test -z "${_path}" ; then
      continue
    fi
    _delegate_bin_0="${_path}/${_delegate}"
    if test ! -f "${_delegate_bin_0}" -o ! -x "${_delegate_bin_0}" ; then
      continue
    fi
    if test "${0}" -ef "${_delegate_bin_0}" ; then
      continue
    fi
    _delegate_bin="${_delegate_bin_0}"
    break
  done

  if test -z "${_delegate_bin}" ; then
    printf '[%08d] [sccache-wrapper] [ee]  failed to resolve tool "%s";  aborting!\n' "${$}" "${_delegate}" >&2
    exit 99
  fi

  exec "${_delegate_bin}" "${@}"
fi

exec "${_sccache_bin}" "${_delegate}" "${@}"
