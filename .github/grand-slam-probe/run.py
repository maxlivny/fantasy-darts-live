"""Read-only live Grand Slam Wikipedia parser diagnostic. Never writes repository files."""
import importlib.util
from pathlib import Path
import sys

base = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('grand_slam_probe_parser', base / 'parser.py')
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
url = 'https://en.wikipedia.org/wiki/2026_Grand_Slam_of_Darts'
print('Source:', url, flush=True)
parsed = mod.fetch_grand_slam(url, {})
groups = parsed['groups']
knockout = parsed['knockout']
print(f'Completed groups: {len(groups)}/16', flush=True)
for key in sorted(groups):
    print(f'Group {key}: ' + '; '.join(f'{position}. {name}' for name, position in sorted(groups[key].items(), key=lambda pair: pair[1])), flush=True)
print(f'Knockout matches: {len(knockout)}', flush=True)
counts = {}
for match in knockout:
    rnd = int(match['round'])
    counts[rnd] = counts.get(rnd, 0) + 1
    print(f"Round {rnd}: {match['winner']} def. {match['loser']} ({match.get('score', 'score unknown')})", flush=True)
print('Matches by round:', counts, flush=True)
if len(groups) != 16:
    raise SystemExit('FAIL: expected 16 completed groups; inspect Wikipedia HTML and parser')
if not knockout:
    raise SystemExit('FAIL: knockout has started, but parser recognized no finished matches')
print('PASS: all groups and at least one knockout match parsed. Check listed names/scores manually.', flush=True)
