# ESPN API Documentation

> Comprehensive reference for the unofficial ESPN API — endpoints, parameters, league slugs, response schemas, and a working Django service.

---

## 📁 File Index

### Root
| File | Description |
|------|-------------|
| [README.md](../README.md) | Full documentation — base URLs, endpoint patterns, fantasy, betting, specialized endpoints |
| [CHANGELOG.md](../CHANGELOG.md) | History of all documented changes |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | How to contribute endpoints, fixes, and code |

### Sports Reference (`docs/sports/`)

Each file covers leagues & competitions, API endpoints, Site API resources, and curl examples for that sport.

| File | Sport | Key Leagues |
|------|-------|-------------|
| [_global.md](sports/_global.md) | All Sports | Every v2 endpoint — full WADL listing |
| [football.md](sports/football.md) | 🏈 Football | NFL, NCAAF, CFL, UFL, XFL |
| [basketball.md](sports/basketball.md) | 🏀 Basketball | NBA, WNBA, NCAAM, NCAAW, NBL, FIBA |
| [soccer.md](sports/soccer.md) | ⚽ Soccer | EPL, La Liga, Bundesliga, MLS, UCL, 260+ leagues |
| [baseball.md](sports/baseball.md) | ⚾ Baseball | MLB, NCAAB, WBC, Caribbean/Winter Leagues |
| [hockey.md](sports/hockey.md) | 🏒 Hockey | NHL, NCAAH, Olympics |
| [golf.md](sports/golf.md) | ⛳ Golf | PGA TOUR, LPGA, LIV, DP World Tour, TGL |
| [racing.md](sports/racing.md) | 🏎️ Racing | Formula 1, IndyCar, NASCAR Cup/Xfinity/Truck |
| [tennis.md](sports/tennis.md) | 🎾 Tennis | ATP, WTA |
| [mma.md](sports/mma.md) | 🥊 MMA | UFC, Bellator, LFA, and 50+ promotions |
| [rugby.md](sports/rugby.md) | 🏉 Rugby Union | World Cup, Six Nations, Premiership, Super Rugby |
| [rugby_league.md](sports/rugby_league.md) | 🏉 Rugby League | NRL, Super League |
| [lacrosse.md](sports/lacrosse.md) | 🥍 Lacrosse | PLL, NLL, NCAA Men's/Women's |
| [cricket.md](sports/cricket.md) | 🏏 Cricket | ICC T20, ICC ODI, IPL |
| [volleyball.md](sports/volleyball.md) | 🏐 Volleyball | FIVB Men/Women, NCAA Men's/Women's |
| [water_polo.md](sports/water_polo.md) | 🤽 Water Polo | FINA Men/Women, NCAA Men's/Women's |
| [field_hockey.md](sports/field_hockey.md) | 🏑 Field Hockey | FIH Men/Women, NCAA Women's |
| [australian_football.md](sports/australian_football.md) | 🦘 Australian Football | AFL |

### API Reference
| File | Description |
|------|-------------|
| [response_schemas.md](response_schemas.md) | Example JSON responses for scoreboard, teams, roster, injuries, game summary, athlete, odds, standings, Now API |

---

## 🚀 Quick Links

- **Scoreboard:** `https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard`
- **Teams:** `https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams`
- **Core API:** `https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/...`
- **Fantasy:** `https://fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{id}`
- **Now/News:** `https://now.core.api.espn.com/v1/sports/news`
