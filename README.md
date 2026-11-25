# Southwest PL explorer

A GitHub Pages site that compares the **Southwest Area Preparedness Level** with FEMS daily values from every
RAWS station in the Southwest PSAs, the workbook PSA ERC(Y)/BI percentiles, fire starts and daily acres
burned. It gives a 7-day PL outlook built only from observed data and the archive.

| Page | What it shows |
|---|---|
| **Analysis** (`index.html`) | Which values track the SW PL, which move first, values you may not be watching. For the whole Southwest or any single PSA. Mar–Oct / pre-monsoon / monsoon (split Jul 1, 8 or 15) / full year. |
| **Explore** (`explore.html`) | Build your own fit: pick the area, days (period, monsoon split, custom dates, years, today's PL), up to 4 values with smoothing, lead 0–30 days, match measure. Shows match, r², year-by-year consistency, PL breakpoints, the coordinate plane and match vs lead. "Find best settings" scans every smoothing × lead. Settings are saved in the link, and the days can be downloaded as CSV. |
| **Best fit** (`bestfit.html`) | The best correlation the program finds (searched daily): every value × smoothing × lead (0–21 d) × period, plus combinations of up to 4 values chosen by forward selection and scored on held-out years. Shows the winner, its formula, PL breakpoints and accuracy, and a leaderboard that links into Explore. |
| **PL forecast** (`forecast.html`) | Chance of each SW PL for the next 7 days, for the Southwest and for each PSA from that PSA's data alone, with a year-by-year track record against "PL stays the same". |
| **Station map** (`map.html`) | Zoomable map of every station with that day's FEMS values (colored against each station's own history), PSAs filled by ERC(Y)/BI class, and fire starts. A calendar picks the date. |
| **Briefing** (`briefing.html`) | One printable page for any date: SW PL, 7-day outlook, PSA table, fires, acres, top indicators. Print/Save as PDF, or download CSV. |

## Set up (about 5 minutes)

1. Create a GitHub repository and upload everything in this folder, **including the hidden `.github` folder**.
2. **Settings → Actions → General → Workflow permissions:** select **Read and write permissions**, then Save.
3. **Settings → Pages → Build and deployment → Source:** select **GitHub Actions**.
4. **Actions** tab → **Update data and publish** → **Run workflow**. After that it runs every 15 minutes.
5. Site: `https://<your-user>.github.io/<repo-name>/`

The history fills in over the first day. Every run loads a slice, newest first. Rough timing: FEMS about 10–15 hours,
IMSR reports about a day. The page header shows progress.

## Updating the breakpoints

Replace **`raw/breakpoints.xlsx`** with the new workbook (same layout) using GitHub's "Add file → Upload files". Keep the file name.
The next run picks it up. Tabs are found by name:

- a tab whose name contains **ERC** and **BREAK**: ERC percentile table (row per PSA: `PSA 1`, `PSA 6N`, …; columns `10th` … `97th`)
- a tab whose name contains **BI** and **BREAK**: BI percentile table, same layout
- a tab whose name contains **Calc PSA**: key stations (Station id | Name | PSA)
- (optional) **START HERE** with the FEMS data-pull URL. If present, only stations in its `stationIds` list are used, like the workbook.

PSA averages follow the workbook: ERC(Y) = ROUNDUP(sum)/stations, BI = sum/stations. The class is the highest percentile reached
(<50, 50th, 75th, 85th (ERC only), 90th, 97th).

## How storage works

- `main` holds the code, the PL sheet and the breakpoints workbook. Nothing is committed to it every 15 minutes.
- The `data` branch holds the station-level data. It's overwritten each run (one commit, no history), so the repo stays small.
  **Don't delete it**, or the backfill starts over.
- The site is published straight from each run.

## Data sources

| Data | Source | Notes |
|---|---|---|
| SW PL | `raw/PL_Levels.xlsx` (2004–) + live SWCC Fire Environment Report | Feb 29 filled from Feb 28/Mar 1 (same, or average rounded up); marked `leap_fill` |
| FEMS NFDRS + weather | `fems.fs2c.usda.gov/api/climatology` daily summaries (observations only) | All RAWS in AZ/NM inside a current SW PSA, plus the workbook's key stations |
| PSA boundaries | National PSA layer, `GACCUnitID = USNMSWC` | Refreshed weekly |
| Excluded areas | NPS boundary, Grand Canyon NP (`GRCA`) | Great Basin GACC; stations and fires there are left out |
| Fire starts | WFIGS Incident Locations (IRWIN), wildfires in AZ/NM inside a SW PSA | Sparse before ~2015 |
| Acres burned per day | NICC IMSR PDFs: famprod archive (1990–2022) + nifc.gov (2013–) | Change in SW year-to-date acres; check `imsr/parsed.csv` on the data branch if numbers look odd |

## Settings (`config.json`)

- `start_year`: how far back (default 2015)
- `fems.auto.exclude_station_ids` / `include_station_ids`, `exclude_areas`: station selection
- `analysis.season_start` / `season_end` / `monsoon_split_options` / `default_monsoon_split`
- `analysis.lags_days`, `analysis.psa_analyses_per_run`
- `forecast.horizons` (7)

## Known limits

- Correlation is not cause. The PL also reflects resource commitments, which this project deliberately leaves out.
- The forecast learns from history and is checked against "PL stays the same". Treat it as a heads-up, not a decision.
- Before the 2021 boundary change, the IMSR "Southwest" row included west Texas.
- GitHub may delay scheduled runs at busy times, and pauses schedules after 60 days without repo activity.
  If that happens, re-enable the workflow on the Actions tab.
