#!/bin/sh
# Check that a built image carries every Debian package that the Dockerfile pins to a fixed
# version (`ARG FIXED_<PACKAGE>=<version>`, package name upper-case with `-` written as `_`)
# in at least that version, on one platform. dpkg inside the image reads the installed
# version and compares it, so Debian's own version ordering decides.
#
# Usage: check-fixed-packages.sh <Dockerfile> <image> <linux/amd64|linux/arm64>
# Exit: 0 every pin is met, 1 a package is older or missing, 2 bad arguments or pins.
# CHECK_CONTAINER_NAME, when set, names the short-lived container.
set -eu

usage() {
    echo "usage: $0 <Dockerfile> <image> <linux/amd64|linux/arm64>" >&2
    exit 2
}

[ "$#" -eq 3 ] || usage
dockerfile=$1
image=$2
platform=$3

case "$platform" in
    linux/amd64 | linux/arm64) ;;
    *) usage ;;
esac
case "$image" in
    "" | -* | *[!A-Za-z0-9._/:@-]*)
        echo "$0: invalid image reference" >&2
        exit 2
        ;;
esac
if [ ! -f "$dockerfile" ]; then
    echo "$0: $dockerfile: no such file" >&2
    exit 2
fi

pins=$(sed -n 's/^ARG FIXED_//p' "$dockerfile")
if [ -z "$pins" ]; then
    echo "$0: $dockerfile pins no package (ARG FIXED_<PACKAGE>=<version>)" >&2
    exit 2
fi

# Build "package=version" arguments for the container, refusing anything that is not a
# plain Debian package name and version.
set --
newline='
'
old_ifs=$IFS
IFS=$newline
for pin in $pins; do
    IFS=$old_ifs
    name=${pin%%=*}
    version=${pin#*=}
    case "$name" in
        "" | *[!A-Z0-9_]* | [!A-Z0-9]*)
            echo "$0: invalid pin name in: ARG FIXED_$pin" >&2
            exit 2
            ;;
    esac
    case "$version" in
        "" | *[!A-Za-z0-9.+:~-]* | [!0-9]*)
            echo "$0: invalid pin version in: ARG FIXED_$pin" >&2
            exit 2
            ;;
    esac
    package=$(printf '%s' "$name" | tr 'A-Z_' 'a-z-')
    set -- "$@" "$package=$version"
done
IFS=$old_ifs

# Runs inside the image as its own user.
# shellcheck disable=SC2016  # expanded by the shell inside the container
check='
status=0
for pin; do
    package=${pin%%=*}
    fixed=${pin#*=}
    state=$(dpkg-query -W -f="\${db:Status-Status} \${Version}" "$package" 2>/dev/null) || state=
    installed=${state#* }
    if [ "${state%% *}" != "installed" ]; then
        echo "$package: not installed (fixed in $fixed)"
        status=1
    elif dpkg --compare-versions "$installed" ge "$fixed"; then
        echo "$package $installed (fixed in $fixed): ok"
    else
        echo "$package $installed is older than the fixed $fixed"
        status=1
    fi
done
exit "$status"
'

echo "fixed Debian packages in $image ($platform):"
exec docker run --rm ${CHECK_CONTAINER_NAME:+--name "$CHECK_CONTAINER_NAME"} \
    --platform "$platform" --network none --read-only \
    --entrypoint sh "$image" -c "$check" check-fixed-packages "$@"
