# dkssregrid

Tools for working with DKSS storm-surge forecast fields.

## `dkss_download.py`

Downloads the operational DKSS forecast GRIB files from the DMI open data API
(`https://opendataapi.dmi.dk/v1/forecastdata`) into `data/`. No API key is
needed, and the script uses only the Python standard library (3.10+).

Each collection publishes **121 hourly GRIB files per model run** (+0 h … +120 h),
one per forecast valid time:

| collection  | domain                      | per file | per run |
|-------------|-----------------------------|---------:|--------:|
| `dkss_nsbs` | North Sea / Baltic Sea      |   8.9 MB | 1.08 GB |
| `dkss_idw`  | inner Danish waters         |  15.2 MB | 1.84 GB |
| `dkss_lf`   | Lillebælt / Fyn             |   5.7 MB | 0.68 GB |
| `dkss_lb`   | Lillebælt                   |   1.6 MB | 0.19 GB |
| `dkss_ws`   | Wadden Sea                  |   1.1 MB | 0.13 GB |
| `dkss_if`   | Isefjord                    |   0.4 MB | 0.05 GB |

The default (all six, latest run) is **~4 GB per invocation** — use
`--collections` and `--max-lead-hours` for smaller pulls.

### Usage

```bash
# what runs are on the API right now
./dkss_download.py --list-runs

# all six collections, latest model run (~4 GB)
./dkss_download.py

# one collection, first 24 forecast hours only
./dkss_download.py --collections dkss_nsbs --max-lead-hours 24

# a specific model run
./dkss_download.py --collections dkss_nsbs,dkss_idw --run 2026-08-21T00

# see what would be fetched without downloading
./dkss_download.py --collections dkss_if --dry-run
```

Useful flags: `--data-dir` (default `./data`), `--jobs N` (parallel downloads,
default 4), `--force` (re-download existing files), `--quiet`.

### Layout

```
data/<collection>/<model run>/<original filename>.grib
data/<collection>/<model run>/manifest.json
```

for example

```
data/dkss_nsbs/2026-08-21T000000Z/DKSS_NSBS_SF_2026-08-21T000000Z_2026-08-26T000000Z.grib
```

The API's own file names are kept — they encode both the model run and the
forecast valid time. `manifest.json` lists every item of the run (id, valid
time, source URL, creation time) so downstream code can enumerate a run without
querying the API again.

### Re-running

Files are streamed to `*.part` and renamed on completion, so an interrupted run
never leaves a truncated `.grib` behind. A file whose size already matches the
server's `Content-Length` is skipped, which makes repeated runs cheap and the
script safe to put in cron. Failed downloads are reported and the exit status is
non-zero, but they do not abort the rest of the run.

`data/` is gitignored.

## `dkss_to_geotiff.py`

Turns a downloaded forecast run into **one GeoTIFF on a regular 1 km UTM32
(EPSG:25832) grid**, with one band per forecast hour. Default field is sea level.

The six collections are nested domains over the same waters, so the converter
warps them onto the target grid **coarsest first** and lets the finer domains
overwrite the coarse ones wherever they have water:

| collection  | grid spacing | resampling to 1 km |
|-------------|-------------:|--------------------|
| `dkss_nsbs` |      ~5025 m | bilinear (upsample) |
| `dkss_ws`   |      ~1797 m | average |
| `dkss_idw`  |       ~874 m | average |
| `dkss_lb`   |       ~175 m | average |
| `dkss_if`   |       ~174 m | average |
| `dkss_lf`   |       ~169 m | average |

Land is carried through as nodata (`-9999`), taken from the GRIB bitmap, so the
coastline stays sharp instead of bleeding into the sea. A full 121-hour run of
all six domains takes about 30 s and produces a ~62 MB file.

### Setup

The script needs the GDAL Python bindings. The system ones work, so the venv
costs no disk and compiles nothing:

```bash
/usr/bin/python3.12 -m venv --system-site-packages .venv
```

Use `./.venv/bin/python` to run it — the default `python3` on this machine is
3.14 from `~/.local`, which has no GDAL.

### Usage

```bash
# whole latest run in data/, sea level, default extent
./.venv/bin/python dkss_to_geotiff.py -o dkss_sealevel.tif

# add a final band with the per-pixel maximum over the run (peak surge)
./.venv/bin/python dkss_to_geotiff.py -o surge.tif --with-max

# first 24 hours, inner Danish waters only
./.venv/bin/python dkss_to_geotiff.py --collections dkss_idw --max-lead-hours 24 -o idw.tif

# a different field, or a depth level
./.venv/bin/python dkss_to_geotiff.py --field WTMP -o sst.tif
./.venv/bin/python dkss_to_geotiff.py --field SALTY --level 25-DBSL -o salt25m.tif

# 500 m grid over a custom extent, and one GRIB file on its own
./.venv/bin/python dkss_to_geotiff.py --res 500 --bbox "500000 6100000 700000 6300000" -o zoom.tif
./.venv/bin/python dkss_to_geotiff.py ~/Downloads/DKSS_NSBS_SF_*.grib -o one.tif
```

Fields are named by GDAL's decoding of the GRIB parameters, not by eccodes —
`grib_ls` mislabels these files because DMI uses local table 128. The surface
fields are `DSLM` (sea level, m), `UGRD`/`VGRD` (10 m wind), `UOGRD`/`VOGRD`
(current), `WTMP`, `SALTY`, `ICETK`, `ICEC`; currents, temperature and salinity
also exist on 50 `<depth>-DBSL` levels.

Two decoding quirks are corrected on the way out, and noted in the output
metadata. GDAL assumes `WTMP` is Kelvin and converts it to Celsius, but DMI
already stores Celsius — so raw values of 7.5–21.7 °C arrive as −265.6…−251.4,
and 273.15 is added back. `SALTY` is labelled kg/kg when the values are PSU;
only the unit string is corrected. `--no-unit-fix` disables both.

Useful flags: `--run` (pick a model run when several are on disk), `--res`,
`--bbox`, `--epsg`, `--resample`, `--max-lead-hours`, `--overwrite`, `--quiet`.

### Extent

The default extent is `301000 5927000 964000 6562000` (6–16 °E, 53.5–59 °N),
663 × 635 px at 1 km. It stops well short of the NSBS domain's eastern edge at
30 °E on purpose: that is far outside the usable range of UTM zone 32, where the
projection distortion becomes severe. `--bbox` is snapped outward to whole
pixels so grids stay aligned between runs.

### In QGIS

A `.qml` sidecar is written next to the GeoTIFF, so just open the `.tif`:

- the blue–white–red ramp is applied, centred on zero and widened to cover the
  run's actual range, so a surge is never flattened against the end of the ramp;
- **Temporal** is preconfigured as *fixed range per band*, one hour per band, so
  the Temporal Controller steps and animates the forecast with no setup.

Per-band temporal ranges need **QGIS ≥ 3.38**. For older versions pass
`--legacy-project`, which additionally writes `<name>.qgs` plus a `<name>_bands/`
folder of one-band VRTs — views onto the same GeoTIFF, no pixels duplicated —
each layer carrying a fixed temporal range. Open that project instead.
