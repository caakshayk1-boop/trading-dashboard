"""Publish the validated screen immediately; no newspaper generation or messaging.
The four projections share one payload and one git commit. Older builds never
replace newer published prices. Run again after rebasing to recheck that guard.
"""
import json
from pathlib import Path
import stock_screen

FILES = ('screen.json', 'screen-detail.json', 'screen-lite.json', 'swot.json')

def projections(data):
    if not data.get('ok') or len(data.get('rows', [])) < 450:
        raise ValueError('Refusing an incomplete screen')
    if data.get('coverage', {}).get('statements_pct', 0) < 70:
        raise ValueError('Refusing incomplete statements')
    day = data.get('price_date')
    if not day or data.get('breadth', {}).get('as_of') != day:
        raise ValueError('Screen and breadth must carry the same real price date')
    table, detail = stock_screen.split_payload(data)
    swot = {k: {q: (v.get('swot') or {}).get(q, [])[:3] for q in ('s', 'w', 'o', 't')}
            for k, v in detail.get('detail', {}).items() if v.get('swot')}
    return dict(zip(FILES, (table, detail, stock_screen.lite_payload(table),
                           {'built_on': detail.get('built_on'), 'swot': swot})))

def superseded(data, directory):
    """True when the published screen is already this build or a newer one.

    The newspaper job (generate.py) rebuilds from its own cache and, on the first
    run of a new ISO week, serves the previous week's key as a "fallback" build.
    Writing that over the directly published screen replaced a clean file with
    the same data stamped is_fallback=true, and every consumer that refuses a
    fallback (Rulebook's feed gate, the hub) stopped building until the next
    21:00 IST publish. The same two rules as publish() apply, in one place.
    """
    old_path = Path(directory) / 'screen.json'
    if not old_path.exists():
        return False
    try:
        old = json.loads(old_path.read_text())
    except ValueError:
        return False
    day, built = data.get('price_date') or '', data.get('generated_at') or ''
    if (old.get('price_date') or '') > day:
        return True
    return (old.get('price_date') or '') == day and (old.get('generated_at') or '') >= built

def publish(data, directory):
    outputs = projections(data)  # Validate all before touching published files.
    directory = Path(directory)
    old_path = directory / 'screen.json'
    old = json.loads(old_path.read_text()) if old_path.exists() else {}
    # A newer build timestamp cannot compensate for older market prices.
    if old.get('price_date', '') > data['price_date']:
        raise ValueError('Refusing to move published prices backwards')
    if old.get('price_date') == data['price_date'] and old.get('generated_at', '') >= data.get('generated_at', ''):
        return False
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in outputs.items():
        (directory / name).write_text(json.dumps(value, default=str, separators=(',', ':')), encoding='utf-8')
    return True

if __name__ == '__main__':
    import sys
    changed = publish(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2])
    print('Published screen projections' if changed else 'Already published this build or a newer one')
