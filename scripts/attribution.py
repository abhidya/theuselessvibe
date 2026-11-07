"""Find explicit, linkable agent attribution; never infer from a model mention.

These are observed credits, not proof that an entire project was AI-generated.
Review comments and repository topics are deliberately not inputs.
"""

import re
from urllib.parse import urlsplit

BOT_LOGINS = {
    "chatgpt-codex-connector[bot]": "codex",
    "claude[bot]": "claude",
}
APP_SLUGS = {
    "chatgpt-codex-connector": "codex",
    "claude": "claude",
    "claude-code": "claude",
}
TRAILER = re.compile(r"^\s*Co-authored-by:\s*(.+?)\s*<([^<>]+)>\s*$", re.I | re.M)
GENERATED = {
    "claude": re.compile(
        r"(?:^|\n)\s*(?:🤖\s*)?Generated\s+with\s+\[Claude Code\]"
        r"\(https://(?:claude\.ai/code|claude\.com/claude-code)/?\)\s*(?:$|\n)",
        re.I,
    ),
    "codex": re.compile(
        r"(?:^|\n)\s*(?:🤖\s*)?Generated\s+with\s+\[Codex\]"
        r"\(https://(?:chatgpt\.com/codex|openai\.com/codex)/?\)\s*(?:$|\n)",
        re.I,
    ),
}


def evidence_url(value, kind):
    """Only expose actual GitHub commit/PR links, not user-supplied redirects."""
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "https" and url.hostname == "github.com"
            and not url.username and not url.password and url.port in (None, 443)
        )
        pattern = r"/[^/]+/[^/]+/commit/[a-f0-9]{7,40}" if kind == "commit" else r"/[^/]+/[^/]+/pull/[0-9]+"
        return value if valid and re.fullmatch(pattern, url.path, re.I) else None
    except ValueError:
        return None


def detect_attribution(commits, pulls):
    """Return compact evidence from GitHub REST commits and PR issue records."""
    evidence = []
    seen = set()

    def add(agent, kind, url, label):
        identity = (agent, kind, url)
        if url and identity not in seen and sum(e["agent"] == agent for e in evidence) < 6:
            seen.add(identity)
            evidence.append({"agent": agent, "kind": kind, "url": url, "label": label})

    for item in commits or []:
        if not isinstance(item, dict):
            continue
        url = evidence_url(item.get("html_url"), "commit")
        if not url:
            continue
        for role in ("author", "committer"):
            actor = item.get(role) or {}
            agent = BOT_LOGINS.get(str(actor.get("login", "")).lower())
            if agent and actor.get("type") == "Bot":
                add(agent, "commit-author", url, f"{agent.title()} bot commit {role}")
        message = (item.get("commit") or {}).get("message") or ""
        for name, email in TRAILER.findall(message):
            name, email = name.strip(), email.strip().lower()
            if re.match(r"^Claude\b", name, re.I) and email == "noreply@anthropic.com":
                add("claude", "commit-trailer", url, "Claude co-author credit")
            if re.match(r"^Codex\b", name, re.I) and email == "noreply@openai.com":
                add("codex", "commit-trailer", url, "Codex co-author credit")
        for agent, pattern in GENERATED.items():
            if pattern.search(message):
                add(agent, "commit-attribution", url, f"Generated with {agent.title()} footer")

    for item in pulls or []:
        if not isinstance(item, dict):
            continue
        # Repository /issues responses contain both issue types. Ignore issues.
        if "pull_request" not in item and "head" not in item:
            continue
        url = evidence_url(item.get("html_url"), "pull")
        if not url:
            continue
        actor = item.get("user") or {}
        agent = BOT_LOGINS.get(str(actor.get("login", "")).lower())
        if agent and actor.get("type") == "Bot":
            add(agent, "pr-author", url, f"Pull request opened by {agent.title()} bot")
        app = item.get("performed_via_github_app") or {}
        agent = APP_SLUGS.get(str(app.get("slug", "")).lower())
        if agent:
            label = "Opened with ChatGPT Codex Connector" if agent == "codex" else "Opened with Claude GitHub app"
            add(agent, "pr-app", url, label)
        body = item.get("body") or ""
        for agent, pattern in GENERATED.items():
            if pattern.search(body):
                add(agent, "pr-attribution", url, f"Generated with {agent.title()} footer")

    return {"agents": sorted({item["agent"] for item in evidence}), "agentEvidence": evidence}
