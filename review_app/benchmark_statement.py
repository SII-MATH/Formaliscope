"""Repeatable Statement browser workload using an isolated database.

Requires optional Playwright/Chromium. --app-root selects the implementation
under test; use the same script, snapshot, and network settings for both runs.
No snapshot, source text, cookies, or personal records are written to results.
"""

import argparse
import gzip
import hashlib
import io
import json
import platform
import statistics
import sys
import tempfile
import threading
from pathlib import Path


PROBE = r"""
(() => {
  const probe=window.statementBenchmark={target:null,start:0,body:null,ready:null,longTasks:[],parses:[]};
  const fetch=window.fetch.bind(window);
  window.fetch=async(...args)=>{
    const response=await fetch(...args);
    response.json=async()=>{
      const text=await response.text(),start=performance.now(),data=JSON.parse(text);
      probe.parses.push({path:new URL(response.url).pathname,start,duration:performance.now()-start});
      return data;
    };
    return response;
  };
  new PerformanceObserver(list=>{
    for(const entry of list.getEntries())probe.longTasks.push({start:entry.startTime,duration:entry.duration});
  }).observe({type:'longtask',buffered:true});
  const check=()=>{
    const card=document.getElementById('review-card'),meta=document.getElementById('meta');
    if(!card||card.hidden||!meta?.textContent.includes(probe.target)||!document.getElementById('lean-code')?.textContent)return;
    if(probe.body===null){
      probe.body=-1;
      requestAnimationFrame(()=>requestAnimationFrame(()=>{probe.body=performance.now()-probe.start;}));
    }
    if(probe.ready===null&&!document.getElementById('rationale').disabled)probe.ready=performance.now()-probe.start;
  };
  new MutationObserver(check).observe(document,{subtree:true,childList:true,attributes:true,characterData:true});
  probe.begin=(target,start=performance.now())=>{
    probe.target=target;probe.start=start;probe.body=null;probe.ready=null;
  };
  probe.begin(decodeURIComponent(location.hash.slice(1)).replace(/^statement::/,''),0);
})();
"""


def summary(values):
    ordered = sorted(values)
    return {"median": round(statistics.median(ordered), 2),
            "p95": round(ordered[round((len(ordered) - 1) * .95)], 2),
            "samples": len(ordered)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--rtt-ms", type=float, default=80)
    parser.add_argument("--mbps", type=float, default=5)
    parser.add_argument("--proxy-gzip", action="store_true", help="Model a gzip reverse proxy for BOTH versions")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="working-tree", help="Identify the implementation in the report")
    args = parser.parse_args()
    if args.repeat < 1 or args.rtt_ms < 0 or args.mbps <= 0:
        parser.error("repeat and mbps must be positive; rtt-ms must be nonnegative")
    sys.path.insert(0, str(args.app_root.resolve()))
    from playwright.sync_api import sync_playwright
    from review_app.preview import PreviewAuthStore
    from review_app.server import ReviewHTTPServer, initialize, make_handler

    snapshot = json.loads(args.snapshot.read_text())
    if snapshot.get("review_mode") != "statement":
        parser.error("a Statement snapshot is required")
    cards = {c['declaration']: c for c in snapshot['cards']}
    names = ['KIP126.Interface.Axiom.challenge1', 'KIP126.Interface.Geometry.standardH6Square',
             'KIP126.Interface.Survival.NonzeroSurvival']
    # Resolve names by their suffix so a project's namespace layout can vary.
    names = [next((n for n in cards if n.split('.')[-1] == name.split('.')[-1]), None) for name in names]
    names = [name for name in names if name]
    if len(names) != 3:
        parser.error("snapshot must include challenge1, standardH6Square and NonzeroSurvival")
    largest = max(snapshot['cards'], key=lambda c: len((c.get('lean') or {}).get('source', '')))['declaration']
    trials = []
    with tempfile.TemporaryDirectory(prefix='formaliscope-browser-') as directory:
        db = Path(directory) / 'judgments.sqlite3'
        initialize(db)
        auth = PreviewAuthStore(db)
        token, _ = auth.create_reviewer('Synthetic benchmark')
        handler = make_handler(snapshot, db, args.app_root / 'review_app/static', auth, preview=True)
        handler.log_message = lambda *unused: None

        class ProxyHandler(handler):
            def do_GET(self):
                if not args.proxy_gzip:
                    return super().do_GET()
                # Apply the same proxy compression after either implementation.
                original = self.wfile
                self.wfile = io.BytesIO()
                try:
                    super().do_GET()
                    headers, body = self.wfile.getvalue().split(b'\r\n\r\n', 1)
                finally:
                    self.wfile = original
                if (len(body) >= 1024 and 'gzip' in self.headers.get('Accept-Encoding', '')
                        and b'Content-Encoding:' not in headers
                        and (b'application/json' in headers or b'text/' in headers)):
                    body = gzip.compress(body, compresslevel=5, mtime=0)
                    headers = b'\r\n'.join(line for line in headers.split(b'\r\n')
                                            if not line.lower().startswith((b'content-length:', b'vary:')))
                    headers += b'\r\nContent-Encoding: gzip\r\nVary: Accept-Encoding\r\nContent-Length: ' + str(len(body)).encode()
                self.wfile.write(headers + b'\r\n\r\n' + body)

        server = ReviewHTTPServer(('127.0.0.1', 0), ProxyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
                browser_version = browser.version
                for trial in range(args.repeat):
                    context = browser.new_context(viewport={'width': 1440, 'height': 900})
                    context.add_cookies([{'name': 'kip126_review_session', 'value': token, 'url': base}])
                    context.add_init_script(PROBE)
                    page = context.new_page()
                    failures = []
                    page.on('pageerror', lambda error: failures.append(str(error)))
                    page.on('response', lambda response: failures.append(f'HTTP {response.status}: {Path(response.url).name}') if response.status >= 400 else None)
                    cdp = context.new_cdp_session(page)
                    cdp.send('Network.enable')
                    cdp.send('Network.emulateNetworkConditions', {'offline': False, 'latency': args.rtt_ms,
                             'downloadThroughput': args.mbps * 1_000_000 / 8,
                             'uploadThroughput': args.mbps * 1_000_000 / 8})

                    def result():
                        page.wait_for_function('()=>statementBenchmark.body>0 && statementBenchmark.ready!==null', timeout=60000)
                        return page.evaluate("""() => ({body_ms:statementBenchmark.body,ready_ms:statementBenchmark.ready,
                          long_task_ms:statementBenchmark.longTasks.filter(e=>e.start>=statementBenchmark.start).reduce((s,e)=>s+e.duration,0),
                          json_parse_ms:statementBenchmark.parses.filter(e=>e.start>=statementBenchmark.start).reduce((s,e)=>s+e.duration,0),
                          parses:statementBenchmark.parses.filter(e=>e.start>=statementBenchmark.start),
                          resources:performance.getEntriesByType('resource').filter(e=>e.name.includes('/api/')).map(e=>({
                            path:new URL(e.name).pathname,ttfb_ms:e.responseStart-e.requestStart,
                            download_ms:e.responseEnd-e.responseStart,duration_ms:e.duration,
                            encoded_bytes:e.encodedBodySize,decoded_bytes:e.decodedBodySize}))})""")

                    page.goto(base + '/#' + cards[names[0]]['id'], wait_until='domcontentloaded')
                    cold = result()
                    page.reload(wait_until='domcontentloaded')
                    reload = result()
                    # Let the deferred formula renderer finish before warm switches.
                    page.wait_for_timeout(700 + args.rtt_ms)

                    def select(name):
                        page.locator('#search').fill(name)
                        row = page.locator('.list-row').filter(has=page.get_by_text(name, exact=True))
                        row.wait_for()
                        row.evaluate('(el,name)=>{statementBenchmark.begin(name);el.click();}', name)
                        return result()

                    warm = [select(name) for name in names[1:]]
                    cached = select(names[0])
                    large = select(largest)
                    trials.append({'cold': cold, 'reload': reload, 'warm': warm, 'cached': cached,
                                   'large': large, 'errors': failures})
                    print(f'trial {trial+1}/{args.repeat}: cold={cold["body_ms"]:.1f}ms reload={reload["body_ms"]:.1f}ms', flush=True)
                    context.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    metrics = {}
    for scenario in ['cold', 'reload', 'warm', 'cached', 'large']:
        samples = [item for trial in trials for item in (trial[scenario] if scenario == 'warm' else [trial[scenario]])]
        metrics[scenario] = {key: summary([item[key] for item in samples])
                             for key in ['body_ms', 'ready_ms', 'long_task_ms', 'json_parse_ms']}
    catalog_samples = [next(r for r in trial['cold']['resources'] if r['path'] == '/api/catalog') for trial in trials]
    metrics['catalog'] = {key: summary([r[key] for r in catalog_samples]) for key in catalog_samples[0] if key != 'path'}
    metrics['catalog']['json_parse_ms'] = summary([next(p['duration'] for p in trial['cold']['parses'] if p['path']=='/api/catalog') for trial in trials])
    module_paths = ['review_app/server.py', 'review_app/static/statement.js',
                    'review_app/static/statement-api.js', 'review_app/static/statement-identity.js']
    output = {'environment': {'python': platform.python_version(), 'chromium': browser_version,
              'implementation': args.label, 'module_sha256': {path: hashlib.sha256((args.app_root/path).read_bytes()).hexdigest() for path in module_paths},
              'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'rtt_ms': args.rtt_ms, 'mbps': args.mbps, 'proxy_gzip': args.proxy_gzip, 'repeat': args.repeat,
              'card_count': len(cards), 'snapshot_digest': snapshot['digest'], 'source_commit': snapshot['source_commit'],
              'largest_source_bytes': len((cards[largest].get('lean') or {}).get('source', '').encode())},
              'metrics': metrics, 'trials': trials}
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    if any(trial['errors'] for trial in trials):
        raise RuntimeError('Browser or HTTP errors occurred; inspect the saved results')


if __name__ == '__main__':
    main()
