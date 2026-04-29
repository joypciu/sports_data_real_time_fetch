# Future Implementations — Realtime Data Fetch

Track of all planned improvements to the stats/cache API system.
Each feature is committed and tested live before moving to the next.

---

## Status Key
- ✅ Done — live and tested
- 🔄 In Progress
- ⏳ Pending

---

---

## All Features Shipped ✅ (2026-04-29)

---

## Feature 1 — Game Timeline  ✅
**Endpoint:** `GET /game/timeline?event_id=X`

Returns every recorded event for a single game in chronological order —
score updates, line moves, period changes, win probability shifts, and the
final result — all in one call.

**Why:** Previously you had to filter `/events?event_id=X` and piece together
the timeline manually. This endpoint does it for you sorted oldest → newest.

**Params:**
| Param | Required | Description |
|---|---|---|
| `event_id` | yes | ESPN event ID |

**Example:**
```
GET /game/timeline?event_id=401869397
```

---

## Feature 2 — Head-to-Head History  ✅
**Endpoint:** `GET /matchups?team=Lakers&opponent=Celtics&sport=basketball`

Returns all historical games between two teams — scores, dates, winner,
and current odds if one is scheduled soon.

**Why:** No way to query past matchups before. Useful for betting research,
pre-game analysis, and head-to-head trends.

**Params:**
| Param | Required | Description |
|---|---|---|
| `team` | yes | One team name or abbreviation |
| `opponent` | yes | Opposing team name or abbreviation |
| `sport` | no | Filter by sport |
| `limit` | no | Max results (default 10) |

**Example:**
```
GET /matchups?team=Lakers&opponent=Celtics&sport=basketball
GET /matchups?team=Arsenal&opponent=Chelsea&sport=soccer
```

---

## Feature 3 — Search / Autocomplete  ✅
**Endpoint:** `GET /search?q=lebr&type=player`

Fuzzy search across players and teams by partial name. Returns matched
names, sport, league, and team. Works with abbreviations and nicknames.

**Why:** Previously you had to know the exact name. Search lets you discover
names, fix spelling, or build an autocomplete dropdown.

**Params:**
| Param | Required | Description |
|---|---|---|
| `q` | yes | Search query (min 2 chars) |
| `type` | no | `player`, `team`, or `all` (default `all`) |
| `sport` | no | Filter by sport |
| `limit` | no | Max results (default 10) |

**Example:**
```
GET /search?q=lebr
GET /search?q=lak&type=team&sport=basketball
GET /search?q=messi&type=player&sport=soccer
```

---

## Feature 4 — Odds History for a Game  ✅
**Endpoint:** `GET /game/odds-history?event_id=X`

Returns the full timeline of how the odds moved for one specific game —
moneyline shifts, total line changes, spread updates — from opening line
to close, in chronological order.

**Why:** LINE_MOVE events existed but required manual filtering and sorting.
This endpoint surfaces the complete odds movement story for a game cleanly.

**Params:**
| Param | Required | Description |
|---|---|---|
| `event_id` | yes | ESPN event ID |

**Example:**
```
GET /game/odds-history?event_id=401869397
```

---

## Feature 5 — Injury / Player Status Feed  ✅
**Endpoint:** `GET /injuries?sport=basketball&team=Lakers&player=LeBron`

Returns current injury and availability status for players — active,
injured, questionable, out, day-to-day. Data is fetched from ESPN's
injury feed and refreshed every polling cycle (~30 s).

**Why:** Stats, live data, and market checks all become more meaningful
when you know who is actually playing. This was the most requested missing
piece.

**Params:**
| Param | Required | Description |
|---|---|---|
| `sport` | no | Filter by sport |
| `team` | no | Filter by team |
| `player` | no | Filter by player name |

**Example:**
```
GET /injuries
GET /injuries?sport=basketball
GET /injuries?team=Lakers
GET /injuries?player=LeBron+James
```

---

## Feature 6 — Bet Tracking & Settlement  ✅
**Endpoints:** `POST /bets`, `GET /bets`, `GET /bets/summary`, `GET /bets/{id}`, `POST /bets/{id}/settle`, `DELETE /bets/{id}`

Persistent bet records backed by SQLite with automatic settlement against historical and live ESPN data.

**Key design:** No event ID or opponent required — team name + date is sufficient to place and settle a bet. Settlement runs immediately on create; pending bets auto-settle on retrieval once the game finishes.

**Single-team market check:** `/stats/market-check` now accepts `date + one team` (opponent optional). The resolver searches both home and away sides, enabling settlement even when only one team in the matchup is known.

**Example:**
```
POST /bets
{ "team": "OKC", "date": "2026-04-28", "market": "moneyline", "pick": "home" }
→ status: "loss", settled: true, source: "historical"
```

---

## Feature 7 — Team Trends (ATS / O-U / Home-Away Splits)  ✅
**Endpoints:** `GET /trends?team=X&sport=Y` (Cache API), `GET /stats/trends` (Stats API)

Computes ATS (against the spread), over/under, and home/away performance splits
for any team directly from the historical DuckDB database.

**Why:** Before placing a bet on /bets, there was no way to research how a team
historically performs against the spread or on totals. This endpoint closes that gap.

**Params:**
| Param | Required | Description |
|---|---|---|
| `team` | yes | Team name or abbreviation |
| `sport` | no | Filter by sport |
| `league` | no | Filter by league key |
| `limit` | no | Recent games to analyse (5–200, default 50) |

**Response includes:**
- `overall`, `home`, `away`: ATS record (cover %, covers/losses/pushes), O/U record (over %, overs/unders), SU record (W-L-D)
- `recent_form`: last 10 completed games with date, side, score, SU, ATS result, O/U result

**Example:**
```
GET /trends?team=OKC&sport=basketball
GET /trends?team=Arsenal&sport=soccer&limit=20
```

---

## Feature 8 — Bet P&L Analytics  ✅
**Endpoint:** `GET /bets/analytics`

Extended analytics across all tracked bets: win rate, ROI, average American odds,
net profit, and breakdowns by market (moneyline/spread/total) and by sport.

**Why:** The existing `/bets/summary` only gave raw counts. Users wanted to know
whether their betting strategy is profitable, which markets perform best, and their
overall ROI — without doing the math themselves.

**Response includes:**
- `win_rate_pct`, `roi_pct`, `avg_odds`, `net_profit`, `total_staked`, `total_returned`
- `by_status`: win/loss/push counts
- `by_market`: per-market wins, losses, staked, returned, net, win_rate
- `by_sport`: same breakdown by sport

**Example:**
```
GET /bets/analytics
→ { "win_rate_pct": 58.3, "roi_pct": 6.2, "net_profit": 74.40, ... }
```

---

## Backlog (Future Consideration)

| Feature | Description |
|---|---|
| Player props tracking | Track player prop lines (LeBron pts o/u, etc.) via sportsbooks |
| Public betting % | Betting trends — sharp vs public money % per side |
| Closing Line Value (CLV) | Compare bet-time odds to closing odds to measure line value |
| WebSocket / SSE push | Stream score + odds updates in real time instead of polling |
| CSV export | `GET /events/export?format=csv` — download event data as spreadsheet |
| Win probability chart | Historical win probability over the course of a game |

---

*Updated automatically as features ship.*
