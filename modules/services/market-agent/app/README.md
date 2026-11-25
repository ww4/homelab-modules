# market-agent

Searches Facebook Marketplace for what a written **brief** asks for. It reads
every listing page, checks the brief's hard limits, looks up what can be looked
up, and has Claude score the survivors (photos included) against the brief.
The output is a ranked list and a shareable HTML report.

It began as a Python port of the Facebook adapter in
[secondhand-mcp](https://github.com/jlsookiki/secondhand-mcp) (MIT). The
evaluation around it is new.

## Why the evaluation is built this way

The first version of this search lost to a ChatGPT run of the same task. The
reason was the evaluation, not the data access:

| Gap | What market-agent does |
|---|---|
| Requirements never written down | A **brief** (TOML) holds hard limits, preferences, home and destinations |
| Mileage, title and transmission thrown away | Reads Facebook's `vehicle_*` fields (odometer, title status, transmission, listing date) and says where each fact came from |
| Model-year facts from memory (one was wrong) | Facts come from `data/vehicles.toml` (sourced, with a `verified` flag), NHTSA recalls, and NHTSA VIN decodes. The judge may not add claims of its own |
| Radius ignored by Facebook | Distance measured from home *and* each destination |
| No photo review | The judge looks at up to N photos per listing |
| Unknowns treated as passes | Every check is `pass` / `fail` / `unknown`; unknowns become seller questions |
| Near misses invisible | Listings just past the distance or price limit are judged and shown separately |

It also keeps what already beat ChatGPT: dealer and flipper templates,
duplicated scam text, rebuilt titles, and listings that claim AWD but aren't.

## Use

```sh
nix develop                       # or: pip install -e '.[test]'
market-agent brief new winter-commuter --example winter-commuter
$EDITOR "$(market-agent brief path winter-commuter)"
market-agent brief check winter-commuter
market-agent run winter-commuter
market-agent page out/watch.html            # every brief on one page, from the store
```

`run` exits non-zero when any search or judge call failed. When **every**
search fails it exits 2: Facebook is refusing us or the GraphQL doc_ids
changed. That is a broken scraper, not a quiet market.

Repeat runs are cheap. A listing page is re-read only when its price changes
(or with `--refresh`), and a listing is re-judged only when its price, text,
checks or the brief change.

### Briefs

See [`src/market_agent/examples/winter-commuter.toml`](src/market_agent/examples/winter-commuter.toml)
(vehicle) and [`general.toml`](src/market_agent/examples/general.toml). Unknown
keys are an error, so a typo can't silently drop a limit.

- **Hard limits** exclude a listing: `location.max_miles`, `price`,
  `search.exclude_words`, and for vehicles `[vehicle]` drivetrain / title /
  transmission / mileage / years. A limit the listing doesn't address is
  `unknown`, not a pass.
- **Preferences** and **notes** only steer the judge's score.
- `kind = "vehicle"` turns on the vehicle facts, the facts table and NHTSA.

### Configuration

Environment variables or a `.env` file; see [`.env.example`](.env.example).
Nothing secret is required. The default judge uses the `claude` CLI's
existing login (`MARKET_AGENT_JUDGE=cli`). `api` uses the Anthropic SDK and
needs `ANTHROPIC_API_KEY`; that path is written but **not yet tested**.

## What leaves the machine

- **Facebook**: logged-out search and listing requests, about one every 1.5 s.
  This goes against Facebook's terms. At personal volume the realistic risk is
  being throttled or blocked.
- **NHTSA**: make, model, year, and VINs that sellers posted publicly.
- **Claude**: each candidate's listing text, our checks and its photos. The
  brief's notes and home town are included, so keep notes to what the judge
  needs.
- **OSRM** (optional): listing and home coordinates. Point it at a server you
  run.

## Layout

```
src/market_agent/
  facebook/      transport, queries, parse, locations, client  (port of secondhand-mcp)
  vehicles/      extract (fields + text), knowledge (+ data/vehicles.toml), nhtsa, checks
  brief.py       brief schema, examples
  checks.py      price / distance / availability / dealer / shared-text signals
  judge.py       Claude scoring: claude CLI or Anthropic SDK, JSON schema output
  pipeline.py    one run: search -> read -> check -> look up -> judge -> store
  store.py       SQLite: listings, price history, runs, lookup cache
  report.py      the watch page: cards with photo, score, price, grouped by brief
```

## Maintaining the facts table

**What each model year was sold as** comes from `data/vehicle_configs.json`:
every US engine × transmission × drive configuration since 2000, from EPA's
public-domain fueleconomy.gov dataset. A fact is implied only when every
configuration agrees (no manual was offered on a 2008 CR-V; every Murano is a
CVT; no Mazda5 was AWD), and the options are recorded otherwise (CR-V: FWD or
AWD, so the listing has to say). EPA doesn't rate vehicles over 8,500 lb GVWR,
so heavy-duty pickups and the Hummer H2 stay with the listing. Refresh it once
a year or so:

```
curl -LO https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip && unzip vehicles.csv.zip
python3 tools/build_vehicle_configs.py vehicles.csv src/market_agent/data/vehicle_configs.json
```

`data/vehicles.toml` holds two kinds of data. **Generations** (drivetrain and
transmission type by year range) are the fallback for models the EPA list
doesn't cover; check them against the manufacturer before adding. **Issues** need a source URL that was actually
read before they get `verified = true`. Unverified entries reach the judge as
leads only, and it turns them into seller questions.
