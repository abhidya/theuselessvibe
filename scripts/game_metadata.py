"""Deterministic, conservative game tags. No model calls, installs or source execution.

A tag means "the author declared it" or "this source signature was observed".
It never means that gameplay, networking, input devices or mobile support worked.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import re
from urllib.parse import quote

VERSION = 'game-tags-v1'
FIELDS = ('genres', 'modes', 'engines', 'libraries', 'languages', 'controls', 'contentLanguages')
MAX_FILE_BYTES = 65_536
MAX_FILES = 6
MAX_EVIDENCE = 64

GENRES = {
    'puzzle': ('puzzle', 'puzzle-game'), 'platformer': ('platformer', 'platform-game'),
    'arcade': ('arcade', 'arcade-game'), 'strategy': ('strategy-game', 'strategy'),
    'racing': ('racing', 'racing-game'), 'rpg': ('rpg', 'role-playing-game'),
    'roguelike': ('roguelike',), 'roguelite': ('roguelite',), 'shooter': ('shooter', 'shoot-em-up', 'fps-game'),
    'tower-defense': ('tower-defense',), 'card': ('card-game',), 'board': ('board-game',),
    'simulation': ('simulation-game',), 'rhythm': ('rhythm-game',),
    'adventure': ('adventure-game',), 'idle': ('idle-game', 'incremental-game'),
    'sports': ('sports-game',),
}
MODES = {
    'solo': ('single-player', 'singleplayer', 'single-player-game'),
    'local-multiplayer': ('local-multiplayer', 'couch-coop', 'split-screen'),
    'online-multiplayer': ('online-multiplayer',),
    'multiplayer-unspecified': ('multiplayer', 'multiplayer-game'),
}
ENGINES = {'phaser': 'phaser', 'kaboom': 'kaboom', 'kaplay': 'kaplay',
           'melonjs': 'melonjs', 'excalibur': 'excalibur', 'playcanvas': 'playcanvas'}
LIBRARIES = {'pixi.js': 'pixijs', 'three': 'threejs', '@babylonjs/core': 'babylonjs', 'babylonjs': 'babylonjs'}
# Exact, current-feature phrases, not arbitrary keyword bags.
MODE_PHRASES = {
    'solo': r'\b(?:single[- ]player|solo play|solo mode)\b',
    'local-multiplayer': r'\b(?:local multiplayer|couch co[- ]?op|split[- ]screen multiplayer)\b',
    'online-multiplayer': r'\bonline multiplayer\b',
    'multiplayer-unspecified': r'\bmultiplayer\b',
}
CONTROL_PHRASES = {
    'keyboard': r'\b(?:arrow keys|WASD|keyboard controls|use (?:your |the )?keyboard)\b',
    'mouse': r'\b(?:mouse controls|use (?:your |the )?mouse)\b',
    'touch': r'\b(?:touch controls|touchscreen controls|swipe to (?:move|play)|tap to (?:move|play))\b',
    'gamepad': r'\b(?:gamepad support|controller support|use (?:your |a )?(?:gamepad|controller))\b',
}
NEGATIVE = re.compile(r"\b(?:no|not|without|unsupported|planned|planning|upcoming|future|todo|coming|eventually|roadmap|will|wish|unimplemented|unavailable|disabled|removed|deprecated)\b|n't\b|\b(?:to be|yet to be|not yet)\b", re.I)
BLOCKED_HEADING = re.compile(r'roadmap|todo|future|planned|credits|acknowledg|references|examples|inspiration|wishlist|changelog|history', re.I)
SKIP_DIRS = {'node_modules', 'vendor', 'third_party', '.git', 'test', 'tests', '__tests__', 'examples', 'docs', 'coverage'}
MANIFEST_NAMES = {'package.json', 'project.godot', 'game.json', 'project.json'}


def source_url(repo: dict, path: str, revision: str | None = None) -> str:
    name = repo['full_name']
    if path == '$readme':
        return f'https://github.com/{name}#readme'
    if path == '$repository':
        return f'https://github.com/{name}'
    ref = revision or repo.get('default_branch') or 'HEAD'
    return f'https://github.com/{name}/blob/{quote(ref, safe="")}/{quote(path, safe="/")}'


def candidate_files(tree: list[dict]) -> list[dict]:
    """Bounded immutable blobs. Ignore symlinks, vendored code, assets and bundles."""
    candidates = []
    for entry in tree:
        if not isinstance(entry, dict):
            continue
        path = entry.get('path', '')
        if not isinstance(path, str) or len(path) > 512 or '..' in path.split('/'):
            continue
        parts = path.lower().split('/')
        size = entry.get('size')
        if (entry.get('type') != 'blob' or entry.get('mode') not in ('100644', '100755')
                or not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES
                or any(part in SKIP_DIRS for part in parts[:-1])):
            continue
        name = parts[-1]
        if name in ('readme.md', 'readme.rst', 'readme.txt', 'readme'):
            rank = 0
        elif name in MANIFEST_NAMES or path.lower() == 'projectsettings/projectversion.txt':
            rank = 1
        elif name == 'index.html' and '.min.' not in name:
            rank = 2
        else:
            continue
        candidates.append((rank, len(parts), path, entry))
    return [e for _, _, _, e in sorted(candidates, key=lambda e: e[:3])[:MAX_FILES]]


def current_lines(text: str, *, include_negative: bool = False):
    """Only current prose outside fenced code, comments, quotes, tasks and future sections."""
    blocked_level = None
    fence = None
    comment = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if '<!--' in line:
            comment = True
        if comment:
            if '-->' in line:
                comment = False
            continue
        if line.startswith(('```', '~~~')):
            marker = line[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence or line.startswith(('>', '- [ ]', '* [ ]')):
            continue
        heading = re.match(r'^(#{1,6})\s+(.+)', line)
        if heading:
            level = len(heading[1])
            if blocked_level is not None and level <= blocked_level:
                blocked_level = None
            if BLOCKED_HEADING.search(heading[2]):
                blocked_level = level
        # Do not use URLs as classification prose, nor Markdown markup around a value.
        line = re.sub(r'!\[[^]]*\]\([^)]*\)', '', line)
        line = re.sub(r'\[([^]]*)\]\([^)]*\)', r'\1', line)
        line = re.sub(r'https?://\S+', '', line)
        line = re.sub(r'[*_`]', '', line)
        if blocked_level is None and line and (include_negative or not NEGATIVE.search(line)):
            yield number, line


class PageInfo(HTMLParser):
    def __init__(self):
        super().__init__()
        self.language = None

    def handle_starttag(self, tag, attrs):
        if tag == 'html':
            value = dict(attrs).get('lang', '')
            if re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', value):
                self.language = value.lower()


def classify(repo: dict, readme: str = '', *, readme_path: str = '$readme',
             files: dict[str, str] | None = None, revision: str | None = None,
             readme_revision: str | None = None, observed_at: str | None = None,
             source_status: str = 'pending', warnings: list[str] | None = None) -> dict:
    """Same inputs yield byte-equivalent tags/evidence. Acquisition time is supplied, not read."""
    files = files or {}
    result = {field: [] for field in FIELDS}
    evidence = []
    counts = set()
    warnings = list(warnings or [])

    def add(field, value, rule, path, excerpt, *, strength='declared', line=None, rev=None):
        if len(evidence) >= MAX_EVIDENCE:
            return
        if field != 'playerCount' and value not in result[field]:
            result[field].append(value)
        record = dict(field=field, value=value, rule=rule, strength=strength,
                      path=path, revision=rev, url=source_url(repo, path, rev),
                      excerpt=excerpt[:180])
        if line is not None:
            record['line'] = line
        key = (field, json.dumps(value, sort_keys=True), rule, path)
        if any((e['field'], json.dumps(e['value'], sort_keys=True), e['rule'], e['path']) == key for e in evidence):
            return
        evidence.append(record)

    # GitHub's primary programming language is not the game's spoken language.
    language = repo.get('language')
    if isinstance(language, str) and 0 < len(language) <= 64:
        add('languages', language, 'github.primary-language', '$repository', language)
    topics = sorted({str(t).strip().lower() for t in repo.get('topics') or []})
    for field, mapping in (('genres', GENRES), ('modes', MODES)):
        for value, aliases in mapping.items():
            for topic in topics:
                if topic in aliases:
                    add(field, value, 'github.exact-topic', '$repository', topic)

    for line_no, line in current_lines(readme[:MAX_FILE_BYTES]):
        genre_line = re.match(r'^(?:[-*]\s*)?genres?\s*[:|]\s*(.+)', line, re.I)
        if genre_line:
            tokens = [t.strip().lower().replace(' ', '-') for t in re.split(r'[,/|;]', genre_line[1])]
            for value, aliases in GENRES.items():
                if value in tokens or any(alias in tokens for alias in aliases):
                    add('genres', value, 'readme.genre-field', readme_path, line, line=line_no, rev=readme_revision)
        for field, mapping in (('modes', MODE_PHRASES), ('controls', CONTROL_PHRASES)):
            for value, pattern in mapping.items():
                if re.search(pattern, line, re.I):
                    add(field, value, f'readme.{field}-declaration', readme_path, line, line=line_no, rev=readme_revision)
        player_match = re.search(r'\b(?:players?\s*:\s*|supports?\s+)(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?\s*(?:players?\b|$)', line, re.I)
        if player_match:
            low, high = int(player_match[1]), int(player_match[2] or player_match[1])
            if 1 <= low <= high <= 64:
                counts.add((low, high))
                add('playerCount', {'min': low, 'max': high}, 'readme.player-count', readme_path, line, line=line_no, rev=readme_revision)

    for path, content in sorted(files.items()):
        if len(content.encode('utf-8')) > MAX_FILE_BYTES:
            continue
        name = path.lower().split('/')[-1]
        if name == 'package.json':
            try:
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError()
                # Development-only packages and transitive lockfile entries are not runtime dependencies.
                dependencies = data.get('dependencies', {})
                if not isinstance(dependencies, dict):
                    raise ValueError()
            except (ValueError, TypeError):
                warnings.append(f'invalid-json:{path}')
                continue
            for dependency, version in sorted(dependencies.items()):
                if not isinstance(version, str):
                    continue
                if dependency in ENGINES:
                    add('engines', ENGINES[dependency], 'manifest.runtime-engine-dependency', path,
                        f'{dependency}: {version}', strength='detected', rev=revision)
                if dependency in LIBRARIES:
                    add('libraries', LIBRARIES[dependency], 'manifest.render-library-dependency', path,
                        f'{dependency}: {version}', strength='detected', rev=revision)
        elif name == 'project.godot' and re.search(r'^config_version\s*=\s*\d+', content, re.M) and '[application]' in content:
            add('engines', 'godot', 'manifest.godot-project', path, 'config_version; [application]', strength='detected', rev=revision)
        elif path.lower() == 'projectsettings/projectversion.txt' and re.search(r'^m_EditorVersion:\s*\d+', content, re.M):
            add('engines', 'unity', 'manifest.unity-project', path, content.splitlines()[0], strength='detected', rev=revision)
        elif name in ('game.json', 'project.json'):
            try:
                data = json.loads(content)
                if isinstance(data, dict) and isinstance(data.get('gdVersion'), dict) and isinstance(data.get('layouts'), list):
                    add('engines', 'gdevelop', 'manifest.gdevelop-project', path, 'gdVersion and layouts fields', strength='detected', rev=revision)
            except ValueError:
                warnings.append(f'invalid-json:{path}')
        elif name.endswith('.html'):
            parser = PageInfo()
            parser.feed(content)
            if parser.language:
                add('contentLanguages', parser.language, 'html.lang-declaration', path,
                    f'html lang={parser.language}', rev=revision)
        # Unstructured JS/TS is intentionally not used as proof: strings, comments,
        # dead paths, examples and bundled dependencies make substring detection noisy.
        # Dependency manifests and engine project files remain reproducible source evidence.

    # Conflicting current declarations do not leave a positive topic-based tag.
    for _, line in current_lines(readme[:MAX_FILE_BYTES], include_negative=True):
        if not NEGATIVE.search(line):
            continue
        for field, mapping in (("modes", MODE_PHRASES), ("controls", CONTROL_PHRASES)):
            for value, pattern in mapping.items():
                if value in result[field] and re.search(pattern, line, re.I):
                    result[field].remove(value)
                    evidence = [e for e in evidence if not (e['field'] == field and e['value'] == value)]
                    warnings.append(f'conflicting-declaration:{field}:{value}')

    # Specific multiplayer declarations supersede a redundant generic label.
    if any(v in result['modes'] for v in ('online-multiplayer', 'local-multiplayer')):
        result['modes'] = [v for v in result['modes'] if v != 'multiplayer-unspecified']
        evidence = [e for e in evidence if not (e['field'] == 'modes' and e['value'] == 'multiplayer-unspecified')]
    if len(counts) > 1:
        warnings.append('conflicting-player-counts')
    result['playerCount'] = dict(zip(('min', 'max'), next(iter(counts)))) if len(counts) == 1 else None
    result['unknownFields'] = [f for f in FIELDS if not result[f]] + ([] if result['playerCount'] else ['playerCount'])
    for field in FIELDS:
        result[field] = sorted(result[field])
    result.update(inputDigest=hashlib.sha256(json.dumps({'topics':topics,'language':repo.get('language'),'readme':readme,'files':files,'rules':VERSION},sort_keys=True,separators=(',',':')).encode()).hexdigest(), schemaVersion=1, rulesVersion=VERSION, observedAt=observed_at, sourceRevision=revision,
                  sourceStatus=source_status, evidence=evidence, warnings=sorted(set(warnings)),
                  mobileCompatibility='unknown', iframeCompatibility='unknown', playability='unverified')
    return result
