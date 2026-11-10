"""Isolate room navigation failures using the same app, fixtures and browser.

Run after a failed UI smoke test. Every probe starts a fresh browser/context;
the original full smoke flow and its assertions remain in ui_smoke.py.
"""
import functools
import json
from http.server import ThreadingHTTPServer
import threading
from playwright.sync_api import sync_playwright, expect
from ui_smoke import Handler, ROOT, OUT, fixtures, browser_evidence


def probe(engine, base, mode):
    browser = engine.launch()
    context = browser.new_context(viewport={'width': 320, 'height': 740})
    tag = f'{engine.name}-{mode}'
    events = browser_evidence(context, tag)
    fixtures(context)
    page = context.new_page()
    result = dict(browser=engine.name, scenario=mode, passed=False)
    try:
        if mode != 'direct-room':
            page.goto(base)
            expect(page.locator('.site-card')).to_have_count(3)
            if mode != 'home':
                page.locator('.card-site-link').first.click()
                expect(page.locator('#game-player')).to_be_visible()
                if mode == 'loaded-player':
                    expect(page.frame_locator('#player-frame').get_by_role('heading', name='UI test game')).to_be_visible()
                if mode == 'closed-player':
                    page.locator('#player-close').click()
                    expect(page.locator('#game-player')).not_to_be_visible()
                    expect(page.locator('#player-frame')).to_have_count(0)
        print('PROBE', tag, 'navigate to room', flush=True)
        page.goto(base + 'room.html?room=PIZZA')
        expect(page.locator('#frame-host iframe')).to_be_visible()
        expect(page.frame_locator('#frame-host iframe').get_by_role('heading', name='UI test game')).to_be_visible()
        result['passed'] = True
    except Exception as error:
        result['error'] = str(error)
    finally:
        (OUT / f'{tag}-events.json').write_text(json.dumps(events, indent=2))
        context.close()
        browser.close()
    print('PROBE RESULT', json.dumps(result), flush=True)
    return result


def run():
    OUT.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(ROOT / 'site')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}/'
    results = []
    try:
        with sync_playwright() as playwright:
            for mode in ['direct-room', 'home', 'active-player', 'loaded-player', 'closed-player']:
                results.append(probe(playwright.webkit, base, mode))
    finally:
        (OUT / 'navigation-probes.json').write_text(json.dumps(results, indent=2))
        server.shutdown()
    if not all(result['passed'] for result in results):
        raise AssertionError('One or more WebKit navigation probes failed; see navigation-probes.json.')


if __name__ == '__main__':
    run()
