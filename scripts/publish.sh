#!/usr/bin/env bash
# Run from the extracted project folder with an authenticated GitHub CLI.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v gh >/dev/null || { echo 'Install the GitHub CLI, then run gh auth login.' >&2; exit 1; }
command -v git >/dev/null || { echo 'Git is required.' >&2; exit 1; }
gh auth status >/dev/null
owner=$(gh api user --jq .login)
repository="${1:-$owner/theuselessvibe}"
if [[ ! "$repository" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]]; then
  echo 'Use an owner/repository name.' >&2
  exit 1
fi
if gh repo view "$repository" >/dev/null 2>&1; then
  echo "Repository $repository already exists; refusing to replace or modify it." >&2
  exit 1
fi
if [ -d .git ]; then
  echo 'This script expects the fresh extracted project, without an existing .git directory.' >&2
  exit 1
fi
git init -b main
git add README.md LICENSE package.json .gitignore .github config scripts site tests docs
git commit -m 'Create The Useless Vibe [skip ci]'
gh repo create "$repository" --public --source=. --remote=origin --push --description 'A random door into tiny GitHub Pages games, with phrase and agent attribution filters.'
gh api --method POST "repos/$repository/pages" -f build_type=workflow >/dev/null
gh workflow run catalog.yml --repo "$repository"
echo "Repository: https://github.com/$repository"
echo "Follow the initial deployment: gh run list --repo $repository"
echo 'The Actions run publishes the URL shown in its GitHub Pages deployment.'
