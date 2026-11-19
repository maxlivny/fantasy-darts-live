"""Read-only Grand Slam Wikipedia end-to-end scoring probe. No repository writes."""
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
groups, knockout = parsed['groups'], parsed['knockout']
print(f'Completed groups: {len(groups)}/16', flush=True)
if len(groups) != 16:
    raise SystemExit('FAIL: not all 16 groups were parsed')

positions = {}
for group_id in sorted(groups):
    places = groups[group_id]
    if sorted(places.values()) != [1, 2, 3]:
        raise SystemExit(f'FAIL: invalid group {group_id}: {places}')
    for name, place in places.items():
        key = mod.normalize_name(name)
        if key in positions:
            raise SystemExit(f'FAIL: player appears in multiple groups: {name}')
        positions[key] = (name, place)
    print(f'Group {group_id}: ' + '; '.join(f'{place}. {name}' for name, place in sorted(places.items(), key=lambda x:x[1])), flush=True)
if len(positions) != 48:
    raise SystemExit(f'FAIL: expected 48 unique players, got {len(positions)}')

counts = {}
losers = set()
for match in knockout:
    round_no = int(match['round'])
    if round_no not in (1, 2, 3, 4):
        raise SystemExit(f'FAIL: invalid round {round_no}')
    counts[round_no] = counts.get(round_no, 0) + 1
    w, l = mod.normalize_name(match['winner']), mod.normalize_name(match['loser'])
    if w not in positions or l not in positions:
        raise SystemExit(f'FAIL: knockout name not in groups: {match}')
    if positions[w][1] != 1 or positions[l][1] != 1:
        raise SystemExit(f'FAIL: knockout player was not a group winner: {match}')
    if w in losers:
        raise SystemExit(f'FAIL: previously eliminated player wins: {match}')
    losers.add(l)
    print(f"Round {round_no}: {match['winner']} def. {match['loser']} ({match.get('score', '?')})", flush=True)
for round_no, limit in ((1, 8), (2, 4), (3, 2), (4, 1)):
    if counts.get(round_no, 0) > limit:
        raise SystemExit(f'FAIL: too many round {round_no} matches')
print('Matches by round:', counts, flush=True)
if not knockout:
    raise SystemExit('FAIL: knockout matches missing')

# Test full scoring on the real fetched data, with one fantasy roster per real player.
participants = [{'name': f'Test {i:02d}', 'players': [name], 'prices': [1]}
                for i, (name, place) in enumerate(positions.values(), 1)]
project = {'participants': participants,
           'manual_score_overrides': {}, 'manual_status_overrides': {},
           'settings': {'tournament_name': 'Grand Slam test', 'rating_participants': []}}
output = mod.calculate_grand_slam(project, parsed)
actual = {mod.normalize_name(row['players'][0]['name']): row['players'][0]
          for row in output['standings']}
expected = {key: {1:3, 2:1, 3:0}[place] for key, (_, place) in positions.items()}
eliminated = {key for key, (_, place) in positions.items() if place != 1}
for match in knockout:
    w, l = mod.normalize_name(match['winner']), mod.normalize_name(match['loser'])
    expected[w] = max(expected[w], {1:6, 2:10, 3:14, 4:18}[int(match['round'])])
    eliminated.add(l)
finished = bool(counts.get(4))
for key, points in expected.items():
    if actual[key]['points'] != points:
        raise SystemExit(f'FAIL: points mismatch for {positions[key][0]}: {actual[key]["points"]} != {points}')
    if actual[key]['alive'] != (not finished and key not in eliminated):
        raise SystemExit(f'FAIL: status mismatch for {positions[key][0]}')
print('Points distribution:', {p:sum(v == p for v in expected.values()) for p in sorted(set(expected.values()))}, flush=True)
print(f'PASS: {len(positions)} players, {len(groups)} groups, {len(knockout)} knockout matches; all points and statuses verified.', flush=True)
