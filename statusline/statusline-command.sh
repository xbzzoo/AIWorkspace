#!/usr/bin/env bash
# Claude Code statusLine command — mirrors Starship Gruvbox Dark theme
# Segments: username | directory | git branch | model | time

input=$(cat)

# Colors (Gruvbox Dark palette — dimmed for status line context)
RESET='\033[0m'
# orange bg  → bold yellow text (username)
C_USER='\033[33m'
# yellow bg  → white text (directory)
C_DIR='\033[97m'
# aqua bg    → cyan text (git)
C_GIT='\033[36m'
# blue bg    → blue text (model)
C_MODEL='\033[34m'
# dark bg    → dim white (time)
C_TIME='\033[2;37m'
SEP='\033[2;37m'

user=$(whoami)
cwd=$(echo "$input" | jq -r '.cwd // .workspace.current_dir // empty')
[ -z "$cwd" ] && cwd=$(pwd)

# Shorten path: replace $HOME with ~, keep last 3 segments
short_dir=$(echo "$cwd" | sed "s|^$HOME|~|")
short_dir=$(echo "$short_dir" | awk -F'/' '{
  n=NF; if(n<=3){print $0} else {
    printf "…/"
    for(i=n-2;i<=n;i++){printf "%s", $i; if(i<n) printf "/"}
    print ""
  }
}')

# Git branch from workspace data
branch=$(echo "$input" | jq -r '.workspace.repo | if . then .owner + "/" + .name else empty end')
# If no repo info, try worktree branch
[ -z "$branch" ] && branch=$(echo "$input" | jq -r '.worktree.branch // empty')
# Fall back to git directly (skip optional locks)
if [ -z "$branch" ] && [ -n "$cwd" ]; then
  branch=$(GIT_OPTIONAL_LOCKS=0 git -C "$cwd" symbolic-ref --short HEAD 2>/dev/null)
fi

model=$(echo "$input" | jq -r '.model.display_name // empty')

time_str=$(date +%H:%M)

# Build status line
out=""

# OS icon + username
out="${out}$(printf "${C_USER} ${user} ${RESET}")"

# Separator + directory
out="${out}$(printf "${SEP} ${RESET}${C_DIR} ${short_dir} ${RESET}")"

# Git branch (only if present)
if [ -n "$branch" ]; then
  out="${out}$(printf "${SEP} ${RESET}${C_GIT}  ${branch} ${RESET}")"
fi

# Model (only if present)
if [ -n "$model" ]; then
  out="${out}$(printf "${SEP} ${RESET}${C_MODEL} ${model}${RESET}")"
fi

# Time
out="${out}$(printf "  ${C_TIME}  ${time_str}${RESET}")"

printf "%b" "$out"
