#!/bin/sh
# GIT_ASKPASS helper — outputs $GIT_TOKEN to stdout.
# Git calls this script when credentials are required.
# The token is passed via the environment, never in argv.
printf '%s\n' "$GIT_TOKEN"
