# ESPN API Response Schemas

> Example JSON response structures for the most commonly used endpoints.  
> All responses are truncated for brevity — actual responses contain more fields.

---

## Scoreboard (`/apis/site/v2/sports/{sport}/{league}/scoreboard`)

```json
{
  "leagues": [
    {
      "id": "46",
      "name": "National Basketball Association",
      "abbreviation": "NBA",
      "slug": "nba",
      "season": {
        "year": 2025,
        "type": 2,
        "slug": "regular-season"
      },
      "logos": [{ "href": "https://...", "width": 500, "height": 500 }]
    }
  ],
  "events": [
    {
      "id": "401765432",
      "uid": "s:40~l:46~e:401765432",
      "date": "2025-03-15T00:00Z",
      "name": "Boston Celtics at Golden State Warriors",
      "shortName": "BOS @ GSW",
      "season": { "year": 2025, "type": 2, "slug": "regular-season" },
      "week": { "number": 18 },
      "status": {
        "clock": "0.0",
        "displayClock": "0.0",
        "period": 4,
        "type": {
          "id": "3",
          "name": "STATUS_FINAL",
          "state": "post",
          "completed": true,
          "description": "Final",
          "detail": "Final",
          "shortDetail": "Final"
        }
      },
      "competitions": [
        {
          "id": "401765432",
          "attendance": 18064,
          "venue": {
            "id": "1234",
            "fullName": "Chase Center",
            "address": { "city": "San Francisco", "state": "CA" },
            "capacity": 18064,
            "indoor": true
          },
          "broadcasts": [
            {
              "market": { "id": "1", "type": "National" },
              "media": { "shortName": "ESPN" },
              "type": { "id": "1", "shortName": "TV" }
            }
          ],
          "competitors": [
            {
              "id": "17",
              "homeAway": "home",
              "team": {
                "id": "9",
                "uid": "s:40~l:46~t:9",
                "abbreviation": "GSW",
                "displayName": "Golden State Warriors",
                "shortDisplayName": "Warriors",
                "color": "006BB6",
                "alternateColor": "FDB927",
                "logo": "https://..."
              },
              "score": "121",
              "winner": true,
              "records": [{ "name": "overall", "summary": "42-24" }],
              "leaders": [
                {
                  "name": "points",
                  "displayName": "Points Leader",
                  "leaders": [
                    {
                      "displayValue": "32",
                      "athlete": { "id": "3136776", "displayName": "Stephen Curry" }
                    }
                  ]
                }
              ]
            },
            {
              "id": "2",
              "homeAway": "away",
              "team": {
                "id": "2",
                "abbreviation": "BOS",
                "displayName": "Boston Celtics",
                "score": "115",
                "winner": false
              }
            }
          ]
        }
      ]
    }
  ]
}
```

---

## Teams (`/apis/site/v2/sports/{sport}/{league}/teams`)

```json
{
  "sports": [
    {
      "id": "46",
      "name": "Basketball",
      "leagues": [
        {
          "id": "46",
          "name": "NBA",
          "teams": [
            {
              "team": {
                "id": "9",
                "uid": "s:40~l:46~t:9",
                "slug": "golden-state-warriors",
                "abbreviation": "GSW",
                "displayName": "Golden State Warriors",
                "shortDisplayName": "Warriors",
                "name": "Warriors",
                "nickname": "Warriors",
                "location": "Golden State",
                "color": "006BB6",
                "alternateColor": "FDB927",
                "isActive": true,
                "isAllStar": false,
                "logos": [
                  {
                    "href": "https://...",
                    "width": 500,
                    "height": 500,
                    "rel": ["full", "default"]
                  }
                ],
                "links": [
                  {
                    "rel": ["clubhouse"],
                    "href": "https://www.espn.com/nba/team/_/id/9/golden-state-warriors",
                    "text": "Clubhouse"
                  }
                ]
              }
            }
          ]
        }
      ]
    }
  ],
  "count": 30,
  "pageIndex": 1,
  "pageSize": 100
}
```

---

## Team Roster (`/apis/site/v2/sports/{sport}/{league}/teams/{id}/roster`)

```json
{
  "team": {
    "id": "9",
    "abbreviation": "GSW",
    "displayName": "Golden State Warriors"
  },
  "athletes": [
    {
      "position": "G",
      "items": [
        {
          "id": "3136776",
          "uid": "s:40~l:46~a:3136776",
          "guid": "...",
          "firstName": "Stephen",
          "lastName": "Curry",
          "displayName": "Stephen Curry",
          "shortName": "S. Curry",
          "jersey": "30",
          "position": {
            "id": "2",
            "name": "Shooting Guard",
            "displayName": "Shooting Guard",
            "abbreviation": "SG"
          },
          "age": 36,
          "height": 74,
          "weight": 185,
          "birthDate": "1988-03-14",
          "experience": { "years": 15 },
          "status": { "id": "1", "name": "Active", "type": "active" },
          "headshot": { "href": "https://..." }
        }
      ]
    }
  ],
  "coach": [
    {
      "id": "6010",
      "firstName": "Steve",
      "lastName": "Kerr",
      "experience": 10
    }
  ]
}
```

---

## Team Injuries (`/apis/site/v2/sports/{sport}/{league}/teams/{id}/injuries`)

```json
{
  "team": {
    "id": "9",
    "abbreviation": "GSW"
  },
  "injuries": [
    {
      "id": "12345",
      "athlete": {
        "id": "3136776",
        "displayName": "Stephen Curry",
        "position": { "abbreviation": "SG" },
        "headshot": { "href": "https://..." }
      },
      "type": {
        "id": "1",
        "name": "knee",
        "description": "Knee",
        "abbreviation": "KNEE"
      },
      "location": "left knee",
      "detail": "Left knee soreness",
      "side": "left",
      "fantasy": { "status": "doubtful", "injuryType": "KNEE" },
      "status": "Doubtful",
      "date": "2025-03-10T00:00Z"
    }
  ]
}
```

---

## Game Summary (`/apis/site/v2/sports/{sport}/{league}/summary?event={id}`)

```json
{
  "boxscore": {
    "teams": [
      {
        "team": { "id": "9", "displayName": "Golden State Warriors" },
        "statistics": [
          { "name": "assists", "displayValue": "28", "label": "Assists" },
          { "name": "rebounds", "displayValue": "41", "label": "Rebounds" },
          { "name": "fieldGoalPct", "displayValue": "48.5", "label": "FG%" }
        ],
        "players": [
          {
            "team": { "id": "9" },
            "position": { "displayName": "Guard" },
            "statistics": [
              {
                "names": ["MIN", "FG", "3PT", "FT", "OREB", "DREB", "REB", "AST", "STL", "BLK", "TO", "PF", "+/-", "PTS"],
                "athletes": [
                  {
                    "athlete": { "id": "3136776", "displayName": "Stephen Curry" },
                    "didNotPlay": false,
                    "stats": ["36", "12-24", "4-10", "4-4", "0", "5", "5", "7", "1", "0", "2", "2", "+8", "32"]
                  }
                ]
              }
            ]
          }
        ]
      }
    ]
  },
  "plays": [
    {
      "id": "4017654340001",
      "sequenceNumber": "1",
      "text": "S. Curry makes 2-pt jump shot from 14 ft",
      "clock": { "displayValue": "11:42" },
      "period": { "number": 1 },
      "team": { "id": "9" },
      "scoreValue": 2,
      "scoringPlay": true
    }
  ],
  "leaders": [
    {
      "name": "points",
      "displayName": "Points Leaders",
      "leaders": [
        {
          "displayValue": "32",
          "team": { "id": "9" },
          "athlete": { "id": "3136776", "displayName": "Stephen Curry" }
        }
      ]
    }
  ],
  "broadcasts": [
    { "market": "national", "names": ["ESPN"] }
  ],
  "predictor": {
    "header": "ESPN BPI Win Probability",
    "homeTeam": {
      "team": { "id": "9" },
      "gameProjection": "63.4",
      "teamChanceLoss": "36.6"
    }
  }
}
```

---

## Athlete (Core API v2)

`GET https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/athletes/{id}`

```json
{
  "id": "3136776",
  "uid": "s:40~l:46~a:3136776",
  "guid": "...",
  "firstName": "Stephen",
  "lastName": "Curry",
  "displayName": "Stephen Curry",
  "shortName": "S. Curry",
  "weight": 185,
  "displayWeight": "185 lbs",
  "height": 74,
  "displayHeight": "6'2\"",
  "age": 36,
  "dateOfBirth": "1988-03-14T00:00Z",
  "birthPlace": { "city": "Charlotte", "state": "NC", "country": "USA" },
  "citizenship": "United States",
  "jersey": "30",
  "active": true,
  "position": {
    "id": "2",
    "name": "Shooting Guard",
    "abbreviation": "SG"
  },
  "linked": true,
  "team": {
    "$ref": "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/teams/9"
  },
  "experience": { "years": 15 },
  "college": {
    "guid": "...",
    "mascot": "Bulldogs",
    "name": "Davidson"
  },
  "draft": {
    "year": 2009,
    "round": 1,
    "selection": 7
  },
  "headshot": {
    "href": "https://a.espncdn.com/...",
    "alt": "Stephen Curry"
  },
  "statistics": {
    "$ref": "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/3136776/statistics"
  }
}
```

---

## Betting Odds (Core API v2)

`GET https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/events/{id}/competitions/{id}/odds`

```json
{
  "count": 3,
  "items": [
    {
      "provider": {
        "id": "41",
        "name": "DraftKings",
        "priority": 1
      },
      "details": "-3.5",
      "overUnder": 222.5,
      "spread": -3.5,
      "overOdds": -110,
      "underOdds": -110,
      "awayTeamOdds": {
        "favorite": false,
        "underdog": true,
        "moneyLine": 140,
        "spreadOdds": -110
      },
      "homeTeamOdds": {
        "favorite": true,
        "underdog": false,
        "moneyLine": -165,
        "spreadOdds": -110
      },
      "open": {
        "over": { "value": 220.0 },
        "under": { "value": 220.0 },
        "spread": { "home": { "line": -4.5 } }
      }
    }
  ]
}
```

---

## Win Probabilities (Core API v2)

`GET .../events/{id}/competitions/{id}/probabilities`

```json
{
  "count": 1,
  "items": [
    {
      "homeWinPercentage": 0.634,
      "awayWinPercentage": 0.366,
      "tiePercentage": 0.0,
      "lastModified": "2025-03-15T02:14:00Z",
      "play": {
        "$ref": "https://sports.core.api.espn.com/v2/..."
      }
    }
  ]
}
```

---

## Standings (`/apis/site/v2/sports/{sport}/{league}/standings`)

```json
{
  "uid": "s:40~l:46",
  "season": { "year": 2025, "displayName": "2024-25" },
  "fullViewLink": { "href": "https://www.espn.com/nba/standings" },
  "children": [
    {
      "name": "Eastern Conference",
      "abbreviation": "EAST",
      "standings": {
        "entries": [
          {
            "team": {
              "id": "2",
              "uid": "s:40~l:46~t:2",
              "displayName": "Boston Celtics",
              "abbreviation": "BOS",
              "logo": "https://..."
            },
            "note": { "color": "03A653", "description": "Clinched Playoffs" },
            "stats": [
              { "name": "wins", "displayName": "Wins", "displayValue": "52" },
              { "name": "losses", "displayName": "Losses", "displayValue": "14" },
              { "name": "winPercent", "displayName": "PCT", "displayValue": ".788" },
              { "name": "gamesBehind", "displayName": "GB", "displayValue": "-" },
              { "name": "streak", "displayName": "Strk", "displayValue": "W3" }
            ]
          }
        ]
      }
    }
  ]
}
```

---

## Now API News (`now.core.api.espn.com/v1/sports/news`)

```json
{
  "count": 25,
  "items": [
    {
      "dataSourceIdentifier": "espn_wire_12345",
      "description": "Stephen Curry scores 32 points as Golden State Warriors beat Boston Celtics 121-115",
      "nowId": "11-12345",
      "premium": false,
      "published": "2025-03-15T02:00:00Z",
      "lastModified": "2025-03-15T02:30:00Z",
      "type": "HeadlineNews",
      "headline": "Curry scores 32, Warriors top Celtics",
      "links": {
        "web": { "href": "https://www.espn.com/nba/story/_/id/12345" },
        "mobile": { "href": "https://m.espn.com/nba/story?id=12345" },
        "api": { "href": "https://api.espn.com/v1/sports/news/12345" }
      },
      "images": [
        {
          "id": 98765,
          "name": "stephen-curry.jpg",
          "caption": "Stephen Curry",
          "url": "https://a.espncdn.com/photo/...",
          "width": 576,
          "height": 324
        }
      ],
      "categories": [
        { "type": "league", "id": 46, "description": "NBA" },
        { "type": "team", "id": 9, "description": "Golden State Warriors" },
        { "type": "athlete", "id": 3136776, "description": "Stephen Curry" }
      ]
    }
  ]
}
```
