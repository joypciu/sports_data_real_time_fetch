# Future Implementations — Realtime Data Fetch

Track of all planned improvements to the stats/cache API system.
Each feature is committed and tested live before moving to the next.

---

## Status Key
- ✅ Done — live and tested
- 🔄 In Progress
- ⏳ Pending

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

## Backlog (Future Consideration)

| Feature | Description |
|---|---|
| Player props tracking | Track player prop lines (LeBron pts o/u, etc.) via sportsbooks |
| Betting trends | Public betting % per side — sharp vs public money |
| WebSocket / SSE push | Stream score + odds updates in real time instead of polling |
| CSV export | `GET /events/export?format=csv` — download event data as spreadsheet |
| Win probability chart | Historical win probability over the course of a game |

---

*Updated automatically as features ship.*
