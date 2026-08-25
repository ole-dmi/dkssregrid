#!/usr/bin/env python3
"""Merge DKSS forecast GRIB files into one UTM32 GeoTIFF at 1 km resolution.

The six DKSS collections are nested domains covering the same waters at
increasing resolution (dkss_nsbs ~5.5 km ... dkss_if a few hundred metres).  This
reads a whole forecast run, warps every domain onto one regular EPSG:25832 grid
-- coarsest first, so the finer nested domains overwrite the coarse ones where
they have water -- and writes a single GeoTIFF with one band per forecast hour.

Each band is described by its valid time, so the result can be stepped through
in QGIS; a .qml sidecar sets up the colour ramp and the temporal controller.

Needs the GDAL Python bindings, which the repository venv provides:

    /usr/bin/python3.12 -m venv --system-site-packages .venv
    .venv/bin/python dkss_to_geotiff.py --help
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

try:
    import numpy as np
    from osgeo import gdal, osr
except ImportError:  # pragma: no cover - depends on the interpreter in use
    sys.exit(
        "dkss_to_geotiff.py needs the GDAL Python bindings, which are not in this\n"
        "interpreter.  Create the repository venv once:\n\n"
        "    /usr/bin/python3.12 -m venv --system-site-packages .venv\n\n"
        "and run the script with .venv/bin/python."
    )

# EPSG:25832, snapped to whole kilometres, covering 6-16 E / 53.5-59 N: Danish
# waters, the German Bight, Skagerrak/Kattegat and the western Baltic.  The NSBS
# domain reaches 30 E, which is far outside the usable range of UTM zone 32, so
# the default deliberately clips well short of it.
DEFAULT_BBOX = (301000.0, 5927000.0, 964000.0, 6562000.0)
DEFAULT_EPSG = 25832
DEFAULT_RES = 1000.0

# GDAL decodes the DMI GRIB1 parameters via the international table and gets them
# right; eccodes applies the ECMWF local table 128 and mislabels them.  So bands
# are identified by GDAL's own metadata, never by message order.
DEFAULT_FIELD = "DSLM"  # deviation of sea level from mean [m]
DEFAULT_LEVEL = "0-SFC"

GRIB_NODATA = 9999.0
OUT_NODATA = -9999.0

# Fallback display range per element, used for the QGIS colour ramp when the
# data itself does not suggest something better.
RAMP_RANGES = {"DSLM": (-1.5, 1.5)}

# Two places where GDAL's decoding of these files is off, as (offset, units):
#   WTMP  - degrib assumes water temperature is Kelvin and converts to Celsius,
#           but DMI already stores Celsius, so the values arrive 273.15 too low.
#           Checked against grib_ls on the raw messages: 7.53..21.71 in the file,
#           -265.62..-251.44 out of GDAL.  The offset is added back.
#   SALTY - labelled kg/kg, but the values are practical salinity units.  Only
#           the unit string is wrong, so nothing is added.
# --no-unit-fix turns both off.
UNIT_FIXUPS = {"WTMP": (273.15, "C"), "SALTY": (0.0, "PSU")}

CREATE_OPTIONS = [
    "TILED=YES",
    "COMPRESS=DEFLATE",
    "PREDICTOR=3",
    "ZLEVEL=6",
    "BIGTIFF=IF_SAFER",
]

RUN_FMT = "%Y-%m-%dT%H:%M:%SZ"


class Source(NamedTuple):
    """One GRIB file, and where the requested field sits inside it."""

    path: Path
    collection: str
    band: int
    ref: datetime
    valid: datetime
    res_m: float
    units: str
    comment: str

    @property
    def lead_hours(self) -> float:
        return (self.valid - self.ref).total_seconds() / 3600.0


def log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, flush=True)


def iso(when: datetime) -> str:
    return when.strftime(RUN_FMT)


def collection_of(path: Path) -> str:
    """dkss_nsbs from DKSS_NSBS_SF_...grib, else the containing directory."""
    match = re.match(r"DKSS_([A-Za-z]+)_", path.name)
    if match:
        return f"dkss_{match.group(1).lower()}"
    for part in (path.parent.name, path.parent.parent.name):
        if part.startswith("dkss_"):
            return part
    return path.parent.name


def normalise_run(text: str) -> str:
    """Accept 2026-08-21T00, ...T00:00, ...T00:00:00Z, 2026-08-21 00 etc."""
    s = text.strip().replace(" ", "T").rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime(RUN_FMT)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"cannot parse model run {text!r}; use e.g. 2026-08-21T00 or 2026-08-21T00:00:00Z"
    )


def grib_files(paths: list[Path]) -> list[Path]:
    """Expand the given files and directories into a sorted list of GRIB files."""
    found: set[Path] = set()
    for path in paths:
        if path.is_dir():
            found.update(p for p in path.rglob("*.grib") if p.is_file())
        elif path.is_file():
            found.add(path)
        else:
            raise SystemExit(f"no such file or directory: {path}")
    return sorted(found)


def source_resolution_m(dataset: gdal.Dataset) -> float:
    """Approximate grid spacing in metres, for ordering the nested domains."""
    gt = dataset.GetGeoTransform()
    mean_lat = gt[3] + 0.5 * gt[5] * dataset.RasterYSize
    dx = abs(gt[1]) * 111320.0 * math.cos(math.radians(mean_lat))
    dy = abs(gt[5]) * 110574.0
    return min(dx, dy)


def scan(path: Path, element: str, level: str) -> Source | None:
    """Locate the requested field in one GRIB file.  None if it is not there."""
    try:
        dataset = gdal.Open(str(path))
    except RuntimeError:
        return None
    if dataset is None:
        return None
    for index in range(1, dataset.RasterCount + 1):
        md = dataset.GetRasterBand(index).GetMetadata()
        if md.get("GRIB_ELEMENT") != element or md.get("GRIB_SHORT_NAME") != level:
            continue
        return Source(
            path=path,
            collection=collection_of(path),
            band=index,
            ref=datetime.fromtimestamp(int(md["GRIB_REF_TIME"]), timezone.utc),
            valid=datetime.fromtimestamp(int(md["GRIB_VALID_TIME"]), timezone.utc),
            res_m=source_resolution_m(dataset),
            units=md.get("GRIB_UNIT", "").strip("[]"),
            comment=md.get("GRIB_COMMENT", element),
        )
    return None


def select_run(sources: list[Source], run: str | None) -> list[Source]:
    """Keep one model run: the requested one, or the newest one present."""
    runs = sorted({src.ref for src in sources})
    if run is None:
        wanted = runs[-1]
        if len(runs) > 1:
            print(
                f"note: {len(runs)} model runs found, using the newest ({iso(wanted)});"
                " pass --run to pick another",
                file=sys.stderr,
            )
    else:
        matches = [r for r in runs if iso(r) == run]
        if not matches:
            raise SystemExit(
                f"model run {run} not found; available: {', '.join(iso(r) for r in runs)}"
            )
        wanted = matches[0]
    return [src for src in sources if src.ref == wanted]


def snap_bbox(
    bbox: tuple[float, float, float, float], res: float
) -> tuple[float, float, float, float]:
    """Grow the extent outward to whole pixels, so the grid stays aligned."""
    west, south, east, north = bbox
    return (
        math.floor(west / res) * res,
        math.floor(south / res) * res,
        math.ceil(east / res) * res,
        math.ceil(north / res) * res,
    )


def target_dataset(bbox: tuple[float, float, float, float], res: float, epsg: int):
    """An in-memory single-band grid, pre-filled with nodata."""
    west, south, east, north = bbox
    width = int(round((east - west) / res))
    height = int(round((north - south) / res))
    if width < 1 or height < 1:
        raise SystemExit(f"empty target grid: bbox {bbox} at {res} m resolution")

    dataset = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform((west, res, 0.0, north, 0.0, -res))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    dataset.SetProjection(srs.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(OUT_NODATA)
    band.Fill(OUT_NODATA)
    return dataset


def resample_for(src_res_m: float, target_res: float, override: str | None) -> str:
    """Average when downsampling a finer domain, bilinear when upsampling."""
    if override:
        return override
    return "average" if src_res_m < target_res else "bilinear"


def warp_timestep(sources: list[Source], target, target_res: float, override: str | None):
    """Warp every domain of one valid time into target, coarsest domain first.

    Each source is a separate gdal.Warp call so the resampling can be chosen per
    domain.  Warping into an existing dataset does not re-initialise it, and
    source nodata (the GRIB land mask) leaves the destination untouched -- so a
    finer domain only overwrites the coarse one where it actually has water.
    """
    for src in sorted(sources, key=lambda s: s.res_m, reverse=True):
        dataset = gdal.Open(str(src.path))
        if dataset is None:
            raise SystemExit(f"cannot open {src.path}")
        band = gdal.Translate("", dataset, format="VRT", bandList=[src.band])
        gdal.Warp(
            target,
            band,
            # The GRIB header describes a sphere (R=6367470); say EPSG:4326
            # explicitly so GDAL does not treat that as a datum to shift from.
            srcSRS="EPSG:4326",
            srcNodata=GRIB_NODATA,
            dstNodata=OUT_NODATA,
            resampleAlg=resample_for(src.res_m, target_res, override),
            multithread=True,
        )
    return target


def band_label(src: Source) -> str:
    return f"{iso(src.valid)} (+{src.lead_hours:g}h)"


def convert(args: argparse.Namespace) -> int:
    files = grib_files(args.paths)
    if not files:
        raise SystemExit(f"no .grib files found under {', '.join(str(p) for p in args.paths)}")

    log(f"scanning {len(files)} GRIB files for {args.field} at {args.level} ...", args.quiet)
    sources = [scan(path, args.field, args.level) for path in files]
    missing = [f for f, s in zip(files, sources) if s is None]
    sources = [s for s in sources if s is not None]
    if not sources:
        raise SystemExit(
            f"none of the {len(files)} files contain {args.field} at level {args.level}"
        )
    for path in missing:
        print(
            f"note: skipped {path.name} (not readable, or no {args.field} at {args.level})",
            file=sys.stderr,
        )

    if args.collections:
        wanted = {c.strip() for c in args.collections.split(",") if c.strip()}
        sources = [s for s in sources if s.collection in wanted]
        if not sources:
            raise SystemExit(f"no files left after --collections {args.collections}")

    sources = select_run(sources, args.run)
    if args.max_lead_hours is not None:
        sources = [s for s in sources if s.lead_hours <= args.max_lead_hours]
        if not sources:
            raise SystemExit(f"no forecast hours within --max-lead-hours {args.max_lead_hours}")

    by_time: dict[datetime, list[Source]] = {}
    for src in sources:
        by_time.setdefault(src.valid, []).append(src)
    times = sorted(by_time)

    model_run = sources[0].ref
    collections = sorted({s.collection for s in sources})
    sample = sources[0]
    log(
        f"model run {iso(model_run)}: {len(times)} forecast hours"
        f" from {len(collections)} domain(s) ({', '.join(collections)})",
        args.quiet,
    )

    bbox = snap_bbox(args.bbox, args.res)
    if bbox != args.bbox:
        print(
            f"note: extent snapped to whole {args.res:g} m pixels: "
            + " ".join(f"{v:.0f}" for v in bbox),
            file=sys.stderr,
        )
    west, south, east, north = bbox
    width = int(round((east - west) / args.res))
    height = int(round((north - south) / args.res))
    nbands = len(times) + (1 if args.with_max else 0)
    log(
        f"target: EPSG:{args.epsg}  {width} x {height} px  {args.res:g} m"
        f"  x {west:.0f}..{east:.0f}  y {south:.0f}..{north:.0f}",
        args.quiet,
    )

    out = Path(args.output)
    if out.exists() and not args.overwrite:
        raise SystemExit(f"{out} exists; pass --overwrite to replace it")
    out.parent.mkdir(parents=True, exist_ok=True)

    driver = gdal.GetDriverByName("GTiff")
    dst = driver.Create(
        str(out), width, height, nbands, gdal.GDT_Float32, options=CREATE_OPTIONS
    )
    if dst is None:
        raise SystemExit(f"cannot create {out}")
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(args.epsg)
    dst.SetProjection(srs.ExportToWkt())
    dst.SetGeoTransform((west, args.res, 0.0, north, 0.0, -args.res))
    dst.SetMetadata(
        {
            "MODEL_RUN": iso(model_run),
            "FIELD": args.field,
            "LEVEL": args.level,
            "DESCRIPTION": sample.comment,
            "UNITS": sample.units,
            "SOURCE_COLLECTIONS": ",".join(collections),
            "CREATED": iso(datetime.now(timezone.utc)),
            "CREATED_BY": "dkss_to_geotiff.py",
        }
    )

    running_max = None
    vmin, vmax = math.inf, -math.inf
    offset = 0.0
    if not args.no_unit_fix and args.field in UNIT_FIXUPS:
        offset, units = UNIT_FIXUPS[args.field]
        dst.SetMetadataItem("UNITS", units)
        if offset:
            dst.SetMetadataItem("UNIT_FIX", f"{offset:+g} added to GDAL's values")
            log(
                f"note: correcting {args.field} by {offset:+g} {units}"
                " (GDAL converts it as if it were Kelvin)",
                args.quiet,
            )
    for number, valid in enumerate(times, 1):
        group = by_time[valid]
        target = target_dataset(bbox, args.res, args.epsg)
        warp_timestep(group, target, args.res, args.resample)
        data = target.GetRasterBand(1).ReadAsArray()
        water = data != OUT_NODATA
        if offset:
            data[water] += offset

        band = dst.GetRasterBand(number)
        band.SetNoDataValue(OUT_NODATA)
        band.SetDescription(band_label(group[0]))
        band.SetMetadata(
            {
                "VALID_TIME": iso(valid),
                "FORECAST_HOUR": f"{group[0].lead_hours:g}",
                "DOMAINS": ",".join(sorted(s.collection for s in group)),
            }
        )
        band.WriteArray(data)

        if water.any():
            vmin = min(vmin, float(data[water].min()))
            vmax = max(vmax, float(data[water].max()))
        if args.with_max:
            masked = np.where(water, data, -np.inf)
            running_max = masked if running_max is None else np.maximum(running_max, masked)

        log(
            f"[{number:4d}/{len(times)}] {band_label(group[0]):32s}"
            f" {len(group)} domain(s), {int(water.sum())} water px",
            args.quiet,
        )

    if args.with_max and running_max is not None:
        summary = np.where(np.isfinite(running_max), running_max, OUT_NODATA)
        band = dst.GetRasterBand(nbands)
        band.SetNoDataValue(OUT_NODATA)
        band.SetDescription(f"maximum over run ({len(times)} hours)")
        band.SetMetadata({"STATISTIC": "max", "MODEL_RUN": iso(model_run)})
        band.WriteArray(summary.astype("float32"))
        log(f"[ max ] per-pixel maximum over the run -> band {nbands}", args.quiet)

    dst.FlushCache()
    dst = None

    if not math.isfinite(vmin):
        vmin, vmax = RAMP_RANGES.get(args.field, (0.0, 1.0))
    # Start from the field's usual display range, but widen it so a storm surge
    # is never flattened against the end of the colour ramp.
    low, high = RAMP_RANGES.get(args.field, (vmin, vmax))
    ramp = (min(low, vmin), max(high, vmax))

    qml = write_qml(out, times, ramp, symmetric=args.field == "DSLM")
    log(f"wrote {out} and {qml}", args.quiet)
    if args.legacy_project:
        project = write_legacy_project(
            out, times, ramp, args.field == "DSLM", args.epsg, bbox
        )
        log(f"wrote {project} for QGIS < 3.38", args.quiet)

    log(
        f"\n{out}: {nbands} band(s), {width} x {height}, "
        f"data range {vmin:.3f} .. {vmax:.3f} {sample.units}",
        args.quiet,
    )
    return 0


# The QML fragments below were produced by QGIS 4.2.1 itself (saveNamedStyle on a
# layer configured through PyQGIS) rather than written by hand, so the element
# names match what QGIS expects.  mode="3" is FixedRangePerBand, which drives the
# temporal controller; it needs QGIS >= 3.38.  The renderer block is understood by
# QGIS 3.x as well, so older versions still get the colour ramp.
PIPE_TEMPLATE = """  <pipe>
    <provider>
      <resampling enabled="false" maxOversampling="2" zoomedInResamplingMethod="nearestNeighbour" zoomedOutResamplingMethod="nearestNeighbour"/>
    </provider>
    <rasterrenderer alphaBand="-1" band="{band}" classificationMax="{vmax:g}" classificationMin="{vmin:g}" nodataColor="" opacity="1" type="singlebandpseudocolor">
      <minMaxOrigin>
        <limits>None</limits>
        <extent>WholeRaster</extent>
        <statAccuracy>Estimated</statAccuracy>
        <cumulativeCutLower>0.02</cumulativeCutLower>
        <cumulativeCutUpper>0.98</cumulativeCutUpper>
        <stdDevFactor>2</stdDevFactor>
      </minMaxOrigin>
      <rastershader>
        <colorrampshader classificationMode="1" clip="0" colorRampType="INTERPOLATED" labelPrecision="3" maximumValue="{vmax:g}" minimumValue="{vmin:g}">
{items}
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
    <huesaturation colorizeBlue="128" colorizeGreen="128" colorizeOn="0" colorizeRed="255" colorizeStrength="100" grayscaleMode="0" invertColors="0" saturation="0"/>
    <rasterresampler maxOversampling="2"/>
    <resamplingStage>resamplingFilter</resamplingStage>
  </pipe>
"""

QML_TEMPLATE = """<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.38.0" styleCategories="AllStyleCategories" hasScaleBasedVisibilityFlag="0" maxScale="0" minScale="1e+08">
  <temporal bandNumber="1" enabled="1" fetchMode="0" mode="3">
    <ranges>
{ranges}
    </ranges>
  </temporal>
{pipe}  <blendMode>0</blendMode>
</qgis>
"""

# QGIS < 3.38 has no per-band temporal mode, so --legacy-project instead writes a
# project of one-band VRTs -- each a view onto the same GeoTIFF, no pixels copied
# -- carrying mode="0" (FixedTemporalRange).  Schema taken from a project written
# by QGIS 3.34.4.
PROJECT_TEMPLATE = """<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34.0" projectname="{title}">
  <title>{title}</title>
  <projectCrs>
    <spatialrefsys nativeFormat="Wkt">
      <authid>EPSG:{epsg}</authid>
    </spatialrefsys>
  </projectCrs>
  <layer-tree-group>
    <customproperties>
      <Option/>
    </customproperties>
{tree}
  </layer-tree-group>
  <projectlayers>
{layers}
  </projectlayers>
  <ProjectTimeSettings temporalRangeStart="{start}" temporalRangeEnd="{end}"/>
  <properties/>
</qgis>
"""

PROJECT_LAYER_TEMPLATE = """    <maplayer type="raster" hasScaleBasedVisibilityFlag="0" maxScale="0" minScale="1e+08" autoRefreshMode="Disabled" refreshOnNotifyEnabled="0">
      <id>{ident}</id>
      <datasource>{source}</datasource>
      <layername>{name}</layername>
      <provider>gdal</provider>
      <srs>
        <spatialrefsys nativeFormat="Wkt">
          <authid>EPSG:{epsg}</authid>
        </spatialrefsys>
      </srs>
      <extent>
        <xmin>{west:.6f}</xmin>
        <ymin>{south:.6f}</ymin>
        <xmax>{east:.6f}</xmax>
        <ymax>{north:.6f}</ymax>
      </extent>
      <temporal mode="0" fetchMode="0" enabled="1">
        <fixedRange>
          <start>{start}</start>
          <end>{end}</end>
        </fixedRange>
      </temporal>
{pipe}    </maplayer>
"""

# ColorBrewer RdBu, reversed: blue low, white mid, red high.  Diverging suits sea
# level (signed about zero); the sequential half is used for one-sided fields.
DIVERGING = ["#053061", "#4393c3", "#f7f7f7", "#d6604d", "#67001f"]
SEQUENTIAL = ["#f7fbff", "#c6dbef", "#6baed6", "#2171b5", "#08306b"]


def colour_ramp_items(vmin: float, vmax: float, colours: list[str]) -> str:
    """The <item> list of the colour ramp, spread evenly over vmin..vmax."""
    step = (vmax - vmin) / (len(colours) - 1)
    return "\n".join(
        f'          <item alpha="255" color="{colour}"'
        f' label="{vmin + index * step:.3g}" value="{vmin + index * step:.6g}"/>'
        for index, colour in enumerate(colours)
    )


def render_pipe(ramp: tuple[float, float], symmetric: bool, band: int = 1) -> str:
    """The <pipe> block: a colour ramp spanning ramp, diverging if symmetric."""
    vmin, vmax = ramp
    if vmax <= vmin:
        vmax = vmin + 1.0
    if symmetric:
        limit = max(abs(vmin), abs(vmax))
        vmin, vmax = -limit, limit
    colours = DIVERGING if symmetric else SEQUENTIAL
    return PIPE_TEMPLATE.format(
        band=band, vmin=vmin, vmax=vmax, items=colour_ramp_items(vmin, vmax, colours)
    )


def write_qml(
    tif: Path,
    times: list[datetime],
    ramp: tuple[float, float],
    symmetric: bool,
) -> Path:
    """Sidecar style: colour ramp plus one temporal range per band.

    QGIS loads <stem>.qml automatically next to <stem>.tif, so the layer arrives
    styled and with the temporal controller already wired to the forecast hours.
    """
    step = times[1] - times[0] if len(times) > 1 else timedelta(hours=1)
    ranges = "\n".join(
        f'      <range band="{number}" begin="{iso(valid)}"'
        f' end="{iso(valid + step)}" includeBeginning="1" includeEnd="0"/>'
        for number, valid in enumerate(times, 1)
    )
    qml = tif.with_suffix(".qml")
    qml.write_text(
        QML_TEMPLATE.format(ranges=ranges, pipe=render_pipe(ramp, symmetric))
    )
    return qml


def write_legacy_project(
    tif: Path,
    times: list[datetime],
    ramp: tuple[float, float],
    symmetric: bool,
    epsg: int,
    bbox: tuple[float, float, float, float],
) -> Path:
    """A .qgs of one-band VRTs, for QGIS versions without per-band temporal mode.

    Every layer is a VRT view onto one band of the GeoTIFF, so this costs a few
    kilobytes and no duplicated pixels.
    """
    bands_dir = tif.parent / f"{tif.stem}_bands"
    bands_dir.mkdir(exist_ok=True)
    step = times[1] - times[0] if len(times) > 1 else timedelta(hours=1)
    pipe = render_pipe(ramp, symmetric)
    west, south, east, north = bbox

    tree, layers = [], []
    for number, valid in enumerate(times, 1):
        vrt = bands_dir / f"band_{number:04d}.vrt"
        gdal.Translate(str(vrt), str(tif), format="VRT", bandList=[number])
        ident = f"band_{number:04d}"
        source = f"./{bands_dir.name}/{vrt.name}"
        name = iso(valid)
        tree.append(
            f'    <layer-tree-layer id="{ident}" source="{source}" name="{name}"'
            f' expanded="0" checked="Qt::Checked" providerKey="gdal">'
            f"<customproperties><Option/></customproperties></layer-tree-layer>"
        )
        layers.append(
            PROJECT_LAYER_TEMPLATE.format(
                ident=ident,
                source=source,
                name=name,
                epsg=epsg,
                west=west,
                south=south,
                east=east,
                north=north,
                start=iso(valid),
                end=iso(valid + step),
                pipe=pipe,
            )
        )

    project = tif.with_suffix(".qgs")
    project.write_text(
        PROJECT_TEMPLATE.format(
            title=tif.stem,
            epsg=epsg,
            tree="\n".join(tree),
            layers="".join(layers),
            start=iso(times[0]),
            end=iso(times[-1] + step),
        )
    )
    return project


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--bbox needs four numbers: west south east north")
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--bbox: {exc}") from exc
    if west >= east or south >= north:
        raise argparse.ArgumentTypeError("--bbox must be west south east north, increasing")
    return west, south, east, north


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="GRIB files or directories to read (default: the data directory)",
    )
    parser.add_argument("-o", "--output", type=Path, default=here / "dkss_sealevel.tif")
    parser.add_argument("--data-dir", type=Path, default=here / "data")
    parser.add_argument("--run", type=normalise_run, help="model run (default: the newest found)")
    parser.add_argument("--collections", help="comma-separated collections to use")
    parser.add_argument("--field", default=DEFAULT_FIELD, help="GRIB_ELEMENT to extract")
    parser.add_argument("--level", default=DEFAULT_LEVEL, help="GRIB_SHORT_NAME level, e.g. 4-DBSL")
    parser.add_argument(
        "--bbox",
        type=parse_bbox,
        default=DEFAULT_BBOX,
        help="target extent in target CRS units: west south east north",
    )
    parser.add_argument("--res", type=float, default=DEFAULT_RES, help="target pixel size [m]")
    parser.add_argument("--epsg", type=int, default=DEFAULT_EPSG, help="target CRS")
    parser.add_argument(
        "--resample",
        choices=["near", "bilinear", "cubic", "cubicspline", "lanczos", "average", "mode"],
        help="force one algorithm (default: average when downsampling, bilinear when up)",
    )
    parser.add_argument("--max-lead-hours", type=float, help="stop after this forecast hour")
    parser.add_argument(
        "--with-max", action="store_true", help="append a band with the per-pixel run maximum"
    )
    parser.add_argument(
        "--no-unit-fix",
        action="store_true",
        help="keep GDAL's values verbatim, including its wrong Kelvin conversion of WTMP",
    )
    parser.add_argument(
        "--legacy-project",
        action="store_true",
        help="also write a .qgs project that animates in QGIS < 3.38",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--quiet", action="store_true")

    args = parser.parse_args(argv)
    if not args.paths:
        if not args.data_dir.exists():
            parser.error(f"no paths given and {args.data_dir} does not exist")
        args.paths = [args.data_dir]
    return args


def main(argv: list[str] | None = None) -> int:
    gdal.UseExceptions()
    # The DMI files use local parameter table 128; GDAL warns about it on every
    # read but decodes the parameters correctly all the same.  Both calls are
    # needed: PushErrorHandler only covers this thread, SetErrorHandler only the
    # worker threads the warper spawns.  Real failures still raise, via
    # UseExceptions.
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    gdal.SetErrorHandler("CPLQuietErrorHandler")
    return convert(parse_args(argv))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
