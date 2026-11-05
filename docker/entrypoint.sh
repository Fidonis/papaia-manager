#!/bin/sh
# Give the running user a name. The core starts this image as ${UID}:${GID} of the host, and
# OpenSSH refuses to run for a user id it cannot look up. nss_wrapper adds the entry for this
# process tree without making /etc/passwd writable. A known user is left alone.
set -eu

if ! id -un >/dev/null 2>&1; then
    passwd_file="$(mktemp)"
    cat /etc/passwd >"$passwd_file"
    printf 'papaia:x:%s:%s:papaia-manager:/tmp:/bin/sh\n' "$(id -u)" "$(id -g)" >>"$passwd_file"
    NSS_WRAPPER_PASSWD="$passwd_file"
    NSS_WRAPPER_GROUP=/etc/group
    LD_PRELOAD=libnss_wrapper.so
    HOME=/tmp
    export NSS_WRAPPER_PASSWD NSS_WRAPPER_GROUP LD_PRELOAD HOME
fi

exec "$@"
