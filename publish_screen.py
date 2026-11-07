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
