import json, os, sys

# Resolve project root (parent of tests/)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIVE_STATE = os.path.join(_ROOT, "live", "live_state.json")

d = json.load(open(_LIVE_STATE))
print('Live games    :', d.get('live_count', d.get('live_games', '?')))
print('Pregame games :', d.get('pregame_count', '?'))
print('Finished today:', d.get('finished_count', '?'))
print('Updated       :', d['updated_at'])
print()
all_games = d.get('live', []) + d.get('pregame', []) + d.get('finished', [])
for g in all_games[:40]:
    h = g.get('home') or {}
    a = g.get('away') or {}
    lk = g.get('league_key', '?')
    st = g.get('status', '?')
    per = g.get('period', 0)
    clk = g.get('clock', '')
    ha = a.get('team_abbr', '?')
    hs = a.get('score', '-')
    hb = h.get('team_abbr', '?')
    hsc = h.get('score', '-')
    print(f'  [{lk:>10}] {ha:>4} {str(hs):>3} - {str(hsc):<3} {hb:<4}  [{st}]  P{per} {clk}')
