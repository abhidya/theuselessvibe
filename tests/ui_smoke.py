"""Rendered UI regression checks. Synthetic games are test fixtures, never published.

Run with playwright==1.56.0 and its Chromium/WebKit browsers installed.
The real Bootstrap CDN stylesheet must load; do not replace it with fake CSS.
"""
import functools
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'ui-artifacts'
DATE = '2026-11-07T00:00:00Z'
SITES = [dict(id=str(n), repo=f'fixture/game-{n}', name=f'Test game {n}' + (' with a deliberately long name' if n == 1 else ''),
              description='Synthetic UI test fixture, not a published catalog entry.',
              url=f'https://fixture.github.io/game-{n}/', repoUrl=f'https://github.com/fixture/game-{n}',
              stars=0, license='MIT', createdAt=DATE, updatedAt=DATE, checkedAt=DATE,
              agents=[], signals=[], agentEvidence=[], attributionStatus='checked') for n in range(1, 4)]
INDEX = [dict(index=i, id=s['id'], repo=s['repo'], name=s['name'], description=s['description'],
              agents=[], terms=[], pageTerms=[], readmeTerms=[]) for i, s in enumerate(SITES)]
MANIFEST = dict(schemaVersion=1, generation='ui-test', query='fixture', searchUrl='https://github.com/search?q=game',
                generatedAt=DATE, matchedRepositories=3, scannedRepositories=3, eligibleSites=3, complete=True,
                shardSize=256, indexKey='ui-test/index.json', shards=[dict(key='ui-test/0000.json', count=3)],
                agentCounts={}, termCounts={}, terms=[])
FILES = {'manifest.json': MANIFEST, 'ui-test/index.json': INDEX, 'ui-test/0000.json': SITES}

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_): pass

def fixtures(context):
    def catalog(route):
        key = urlsplit(route.request.url).path.split('/data/', 1)[1]
        if key in FILES: route.fulfill(status=200, content_type='application/json', body=json.dumps(FILES[key]))
        else: route.fulfill(status=404, body='Missing test fixture')
    context.route('**/data/**', catalog)
    context.route('https://fixture.github.io/**', lambda route: route.fulfill(status=200, content_type='text/html', body='''<!doctype html><html><head><style>body{margin:0;font:18px system-ui;background:#edf2f5;display:grid;place-items:center;height:100vh;color:#263544}section{text-align:center;padding:12px}button{padding:12px}</style></head><body><section><h1>UI test game</h1><p>This is a synthetic fixture.</p><button>Play</button></section></body></html>'''))

def geometry(page, ids, frame_id):
    viewport = page.viewport_size
    boxes = []
    for id in ids:
        loc = page.locator('#' + id); expect(loc).to_be_visible()
        box = loc.bounding_box(); assert box is not None
        assert box['width'] >= 44 and box['height'] >= 43, (id, box)
        assert box['x'] >= 0 and box['x'] + box['width'] <= viewport['width'] + 1, (id, box, viewport)
        assert box['y'] >= 0 and box['y'] + box['height'] <= viewport['height'] + 1, (id, box, viewport)
        # Bootstrap intentionally disables pointer events on disabled buttons.
        # Their geometry must still fit; active controls must also be hittable.
        if loc.is_enabled():
            assert loc.evaluate('(el) => {const r=el.getBoundingClientRect();const top=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return el===top||el.contains(top)}'), f'{id} obscured'
        boxes.append(box)
    for a, b in zip(boxes, boxes[1:]): assert a['x'] + a['width'] <= b['x'] + 1, (a, b)
    # Long titles must not widen the implicit grid track or hide header actions.
    shell = page.locator('.room-shell').bounding_box()
    assert shell and shell['width'] <= viewport['width'], ('Player wider than viewport', shell)
    for control in page.locator('.room-header button, .room-header a').all():
        if not control.is_visible(): continue
        box = control.bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= viewport['width'] + 1, ('Header control clipped', box)
    frame = page.locator(frame_id).bounding_box(); assert frame and frame['height'] > 80
    assert frame['y'] + frame['height'] <= min(b['y'] for b in boxes) + 1, 'Navigation overlays the game'
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Horizontal overflow'

def exercise(browser, name, base, width, height):
    context = browser.new_context(viewport={'width': width, 'height': height})
    fixtures(context)
    page = context.new_page(); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    tag = f'{name}-{width}x{height}'
    try:
        page.goto(base)
        expect(page.locator('.site-card')).to_have_count(3)
        # A successful layout must use the actual framework, not unstyled HTML.
        expect(page.locator('#random-button')).to_have_css('background-color', 'rgb(220, 53, 69)')
        page.screenshot(path=str(OUT / f'{tag}-collection.png'), full_page=True)
        page.locator('.card-site-link').nth(0).click()
        expect(page.locator('#game-player')).to_be_visible()
        geometry(page, ['player-previous', 'player-history', 'player-next'], '#player-frame')
        page.screenshot(path=str(OUT / f'{tag}-player.png'))
        page.locator('#player-more').click()
        expect(page.locator('#player-direct')).to_be_visible()
        assert page.locator('#player-direct').get_attribute('href') == SITES[0]['url']
        page.screenshot(path=str(OUT / f'{tag}-more.png'))
        page.keyboard.press('Escape'); expect(page.locator('#player-more-dialog')).not_to_be_visible()
        expect(page.locator('#game-player')).to_be_visible()
        page.locator('#player-close').click()
        page.locator('.card-site-link').nth(1).click()
        page.locator('#player-previous').click()
        expect(page.locator('#player-name')).to_have_text(SITES[0]['name'])
        page.locator('#player-next').click()
        expect(page.locator('#player-name')).to_have_text(SITES[1]['name'])
        page.reload(); expect(page.locator('#player-name')).to_have_text(SITES[1]['name'])
        page.locator('#player-history').click()
        expect(page.locator('#player-history-list button')).to_have_count(2)
        page.screenshot(path=str(OUT / f'{tag}-history.png'))
        page.locator('#player-history-list button').nth(1).click()
        expect(page.locator('#player-name')).to_have_text(SITES[0]['name'])
        page.go_back(); expect(page.locator('#player-name')).to_have_text(SITES[1]['name'])
        page.go_forward(); expect(page.locator('#player-name')).to_have_text(SITES[0]['name'])
        page.locator('#player-close').click()
        page.locator('#catalog-query').fill('no-such-fixture')
        expect(page.locator('#random-button')).to_be_disabled()
        expect(page.locator('#directory-state-title')).to_have_text('No games found.')
        page.locator('#clear-filters').click(); expect(page.locator('.site-card')).to_have_count(3)
        page.locator('#random-button').click(); expect(page.locator('#game-player')).to_be_visible()
        geometry(page, ['player-previous', 'player-history', 'player-next'], '#player-frame')
        # The room implementation is preserved and still navigates independently.
        page.goto(base + 'room.html?room=PIZZA')
        expect(page.locator('#frame-host iframe')).to_be_visible()
        first = page.locator('#game-title').inner_text()
        geometry(page, ['previous-game', 'show-history', 'next-game'], '#frame-host iframe')
        page.screenshot(path=str(OUT / f'{tag}-room.png'))
        other = context.new_page(); other.goto(base + 'room.html?room=PIZZA')
        expect(other.locator('#game-title')).to_have_text(first)
        page.locator('#next-game').click(); expect(page.locator('#game-title')).not_to_have_text(first)
        expect(other.locator('#game-title')).to_have_text(first)
        page.locator('#room-menu').click(); expect(page.locator('#auto-rotate')).not_to_be_checked()
        assert not errors, errors
    except Exception:
        page.screenshot(path=str(OUT / f'{tag}-failure.png'), full_page=True)
        raise
    finally: context.close()

def run():
    OUT.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(ROOT / 'site')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}/'
    report = []
    try:
        with sync_playwright() as p:
            for engine in [p.chromium, p.webkit]:
                browser = engine.launch()
                try:
                    for width, height in [(320, 740), (390, 844), (568, 320), (1280, 900)]:
                        exercise(browser, engine.name, base, width, height)
                        report.append({'browser': engine.name, 'width': width, 'height': height, 'passed': True})
                        print('PASS', report[-1], flush=True)
                    context = browser.new_context(viewport={'width': 390, 'height': 844}); fixtures(context)
                    context.add_init_script("Object.defineProperty(window, 'localStorage', {get(){throw new DOMException('Disabled','SecurityError')}})")
                    page = context.new_page(); page.goto(base)
                    expect(page.locator('#random-button')).to_be_enabled()
                    page.locator('#random-button').click(); expect(page.locator('#game-player')).to_be_visible()
                    context.close()
                finally: browser.close()
    finally:
        (OUT / 'report.json').write_text(json.dumps(report, indent=2))
        server.shutdown()

if __name__ == '__main__': run()
