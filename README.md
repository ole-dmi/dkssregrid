# dkssregrid

Tools for working with DKSS storm-surge forecast fields from the Danish
Meteorological Institute (DMI).

- **`dkss_download.py`** — fetch operational DKSS forecast GRIB files from the DMI
  open data API.
- **`dkss_to_geotiff.py`** — merge a downloaded run into a single GeoTIFF on a
  regular grid, one band per forecast hour, ready for QGIS.

## Setup

```bash
git clone https://github.com/ole-dmi/dkssregrid.git
cd dkssregrid
```

Requirements differ between the two scripts:

| script | needs |
|---|---|
| `dkss_download.py` | Python 3.10+ — **standard library only**, nothing to install |
| `dkss_to_geotiff.py` | Python 3.10+, plus the **GDAL Python bindings** and **NumPy** |

The DMI open data API needs no key or account, so `dkss_download.py` is usable
immediately after cloning:

```bash
./dkss_download.py --list-runs
```

### Installing GDAL for `dkss_to_geotiff.py`

GDAL's Python bindings must match the GDAL C library they are built against, which
makes them the one genuinely awkward dependency. Pick whichever route fits your
platform — the tools were developed against GDAL 3.8 and should work on any GDAL 3.x.

**Conda / mamba — works the same on Linux, macOS and Windows, and is the most
reliable option if you have no system GDAL:**

```bash
conda create -n dkssregrid -c conda-forge python=3.12 gdal numpy
conda activate dkssregrid
python dkss_to_geotiff.py --help
```

**System GDAL, Linux (Debian/Ubuntu):** if your distribution already packages the
bindings, a venv with `--system-site-packages` reuses them, so nothing is compiled
and the venv costs almost no disk:

```bash
sudo apt install gdal-bin python3-gdal python3-numpy
python3 -m venv --system-site-packages .venv
./.venv/bin/python dkss_to_geotiff.py --help
```

Substitute your distribution's equivalent packages elsewhere — `python3-gdal` on
Fedora/RHEL, `gdal` via Homebrew on macOS.

**pip:** only if a GDAL C library is already installed and its development headers
are available. The pip package version must match the system library exactly:

```bash
pip install numpy "gdal==$(gdal-config --version)"
```

> **A note on which interpreter you run.** The GDAL bindings live in one specific
> Python installation. If the `python3` first on your `PATH` is a different one — a
> pyenv build, a `~/.local` install, a newer version than your distribution's — the
> script exits with an `ImportError` message. Invoke the interpreter that has GDAL
> explicitly (`./.venv/bin/python …`, or activate the conda environment) rather than
> relying on `python3`.

Verify the bindings are visible before going further:

```bash
python -c "from osgeo import gdal; print(gdal.__version__)"
```

## `dkss_download.py`

Downloads the operational DKSS forecast GRIB files from the DMI open data API
(`https://opendataapi.dmi.dk/v1/forecastdata`) into `data/`.

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
`--collections` and `--max-lead-hours` for smaller pulls. Make sure the target
filesystem has room.

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
coastline stays sharp instead of bleeding into the sea. A full 121-hour run of all
six domains produces a ~62 MB file, and takes on the order of half a minute on a
typical desktop.

Run it with the interpreter that has GDAL — `./.venv/bin/python` in the venv setup
above, or plain `python` inside an activated conda environment. The examples below
use `python`.

### Usage

```bash
# whole latest run in data/, sea level, default extent
python dkss_to_geotiff.py -o dkss_sealevel.tif

# add a final band with the per-pixel maximum over the run (peak surge)
python dkss_to_geotiff.py -o surge.tif --with-max

# first 24 hours, inner Danish waters only
python dkss_to_geotiff.py --collections dkss_idw --max-lead-hours 24 -o idw.tif

# a different field, or a depth level
python dkss_to_geotiff.py --field WTMP -o sst.tif
python dkss_to_geotiff.py --field SALTY --level 25-DBSL -o salt25m.tif

# 500 m grid over a custom extent, and one GRIB file on its own
python dkss_to_geotiff.py --res 500 --bbox "500000 6100000 700000 6300000" -o zoom.tif
python dkss_to_geotiff.py /path/to/DKSS_NSBS_SF_*.grib -o one.tif
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
