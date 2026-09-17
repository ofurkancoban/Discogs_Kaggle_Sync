# Discogs Kaggle Sync

Automates what [ofurkancoban](https://github.com/ofurkancoban) was previously doing by
hand each month: download the latest [Discogs data dump](https://data.discogs.com/),
convert it from XML.GZ to CSV, and publish it as a new Kaggle dataset (e.g.
[Discogs Data Dumps (April 2025)](https://www.kaggle.com/datasets/ofurkancoban/discogs-data-dumps-april-2025)).

## How it works

1. **Check for a new month** - scrapes `data.discogs.com` for the most recent month with
   `artists`/`labels`/`masters`/`releases` dumps.
2. **Skip if already published** - `state/published_months.json` tracks what's already on
   Kaggle, so re-running (or an overlapping cron run) is always safe.
3. **Download** each of the 4 files with resume-on-reconnect (`discogs_kaggle_sync/downloader.py`).
4. **Convert** each `.xml.gz` straight to CSV (`discogs_kaggle_sync/converter.py`) - the
   two-pass column-discovery-then-write approach from the DiscogsGUI project, but chunking
   reads directly off the gzip stream instead of a fully-decompressed `.xml` file, since the
   `releases` dump alone is ~10GB compressed and tens of GB unpacked. The compressed file is
   deleted the moment its CSV exists, so peak disk usage stays as low as this format allows.
5. **Publish to Kaggle** as a new dataset (`discogs_kaggle_sync/kaggle_publish.py`) - one
   dataset per month, matching the existing manually-published naming pattern. Column
   descriptions come from `discogs_kaggle_sync/column_descriptions/*.json`, written once per
   content type and reused every month (Discogs' XML schema barely changes month to month).
   Any column not in that file gets a generic placeholder description and a warning in the
   log, so schema drift is visible instead of silently under-documented.

## Requirements

- **Disk**: at least ~100GB free scratch space. `releases.xml.gz` is ~10GB compressed and
  unpacks to tens of GB; even with the streaming approach above, the chunked intermediate
  files plus the final CSVs need real headroom.
- **A machine that can run unattended for hours**: a VPS with cron, not GitHub Actions -
  GitHub-hosted runners only have ~14GB of free disk, nowhere near enough for `releases`.
- A [Kaggle API token](https://www.kaggle.com/settings) at `~/.kaggle/kaggle.json`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Kaggle API credentials (from https://www.kaggle.com/settings -> Create New Token)
mkdir -p ~/.kaggle
mv ~/Downloads/kaggle.json ~/.kaggle/kaggle.json
chmod 600 ~/.kaggle/kaggle.json
```

## Running

```bash
python run_monthly_sync.py --kaggle-owner YOUR_KAGGLE_USERNAME
```

Add `--force` to re-process the latest month even if it's already marked published - useful
if Discogs re-uploads a corrected dump.

## Scheduling

Runs **daily**, not on a fixed day of the month - `state/published_months.json` makes a
run a no-op (exits in seconds) once the current month is already published, so a daily
check costs almost nothing and catches whatever day Discogs actually publishes on, instead
of guessing a fixed date and possibly missing it by a day or two.

Two ways to schedule it, pick one:

- **crontab** - see [`crontab.example`](crontab.example), installed via `crontab -e`.
- **pm2** - see [`ecosystem.config.js`](ecosystem.config.js) if the VPS already uses pm2
  for other processes (`pm2 start ecosystem.config.js`); gives you `pm2 logs`/`pm2 status`
  for this job alongside everything else instead of a separate crontab entry.

## Column descriptions

`discogs_kaggle_sync/column_descriptions/{artists,labels,masters,releases}.json` map a CSV
column name to its Kaggle-facing description. These were written once against a real
generated schema and should only need updates if Discogs changes its XML structure - the
sync script's log output will flag any column it doesn't recognize (`... column(s) have no
description on file`) so you know when that's happened.
