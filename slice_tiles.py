import math
import os
import argparse
from osgeo import gdal, osr
from tqdm import tqdm
from pyproj import Transformer
import subprocess

# Enable GDAL exceptions
gdal.UseExceptions()
script_dir = os.path.dirname(os.path.abspath(__file__))
# -----------------------------
# Configure Argument Parser
# -----------------------------
parser = argparse.ArgumentParser(
        prog='TilesSlider'
    )
parser.add_argument('--input_dir', help='Input directory', type=str, default=os.getcwd())
parser.add_argument('--output_dir', help='Output directory', type=str, default=os.getcwd())
parser.add_argument('--zoom', help='Zoom level (Default: 20)', type=int, default=20)
parser.add_argument('--tile_size', help='Tile size (Default: 256)', type=int, default=256)
parser.add_argument('--only_mosaic', help='Only create mosaic', action='store_true')
parser.add_argument('--utm_tiles', help='Create UTM tiles instead of Web Mercator tiles', action='store_true')

args = parser.parse_args()

# -----------------------------
# User Configuration
# -----------------------------
input_folder = args.input_dir
zoom = args.zoom
output_folder = args.output_dir
output_folder = os.path.join(output_folder, str(zoom))
os.makedirs(output_folder, exist_ok=True)

tile_size = args.tile_size # Tile size in pixels
only_mosaic = args.only_mosaic
utm_tiles = args.utm_tiles

# -----------------------------
# Helper functions
# -----------------------------
def lon_to_tile_x(lon, zoom):
    """Convert longitude to tile X index at given zoom level."""
    return int((lon + 180.0) / 360.0 * (2 ** zoom))

def lat_to_tile_y(lat, zoom):
    """Convert latitude to tile Y index at given zoom level."""
    lat_rad = math.radians(lat)
    n = math.pi - math.log(math.tan(math.pi / 4 + lat_rad / 2))
    return int((n / math.pi) * (2 ** (zoom - 1)))

def tile_bounds(x, y, zoom):
    """Return bounding box (min_lon, min_lat, max_lon, max_lat) of a Web Mercator tile."""
    n = 2.0 ** zoom
    lon_min = x / n * 360.0 - 180.0
    lat_min = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    lon_max = (x + 1) / n * 360.0 - 180.0
    lat_max = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon_min, lat_min, lon_max, lat_max

def compress_mosaic(input_tif):
    compressed_path = input_tif.replace(".tif", "_compressed.tif")
    print("Compressing mosaic...")

    gdal.Translate(
        compressed_path,
        input_tif,
        creationOptions=[
            "COMPRESS=JPEG",      # or "DEFLATE" (without loss)
            "TILED=YES",
            "BIGTIFF=YES"
        ]
    )

    print(f"Compressed mosaic saved as: {compressed_path}")
    return compressed_path

def create_preview_png(input_tif):
    preview_png = input_tif.replace(".tif", "_preview.png")
    gdal.Translate(
        preview_png,
        input_tif,
        widthPct=20,
        heightPct=20,
        format="PNG"
    )
    print(f"Preview PNG saved as: {preview_png}")

def get_epsg_from_coords(x, y):
    """
    Determine the appropriate EPSG code for a GeoTIFF based on its coordinates.
    Returns the correct global UTM EPSG code (WGS84-based: EPSG:326xx or 327xx).
    Works worldwide.
    """
    try:
        # Try to interpret coordinates as metric (e.g. ETRS89 / UTM) and convert to lat/lon
        transformer = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
        lon, lat = transformer.transform(x, y)
    except Exception:
        # Fallback: assume coordinates are already geographic
        lon, lat = x, y

    # If longitude seems unrealistic, approximate using typical offset
    if not (-180 <= lon <= 180):
        lon = (x - 500000) / 100000 + 9  # rough fallback approximation

    # Determine UTM zone
    zone = int((lon + 180) / 6) + 1

    # Choose EPSG depending on hemisphere
    epsg = 32600 + zone if lat >= 0 else 32700 + zone

    return epsg

def is_utm(epsg):
    """Check if EPSG code corresponds to a UTM CRS."""
    # WGS84 UTM North
    if 32601 <= epsg <= 32660:
        return True
    # WGS84 UTM South
    if 32701 <= epsg <= 32760:
        return True
    # ETRS89 / UTM Europe
    if 25800 <= epsg <= 25899:
        return True
    return False

def get_epsg(ds):
    """Extract EPSG code from GDAL dataset."""
    srs = osr.SpatialReference()
    srs.ImportFromWkt(ds.GetProjection())
    return int(srs.GetAttrValue("AUTHORITY", 1))

# -----------------------------
# Load and merge input TIFs
# -----------------------------
print("Creating a mosaic tiff file out of all input tifs...")

# Automatically find all .tif files in the input folder
tifs = [f for f in os.listdir(input_folder) if f.lower().endswith(".tif")]

# Full paths
tif_paths = [os.path.join(input_folder, f) for f in tifs]

# --- Assign CRS to input TIFFs that have none ---
for tif in tif_paths:
    ds = gdal.Open(tif)
    proj = ds.GetProjection()
    gt = ds.GetGeoTransform()
    ds = None

    if not proj.strip():
        # Compute approximate center point in the source coordinate space
        center_x = gt[0] + gt[1] * 0.5
        center_y = gt[3] + gt[5] * 0.5

        # Determine the correct EPSG automatically
        epsg = get_epsg_from_coords(center_x, center_y)
        print(f"{tif} has no CRS. Assigning EPSG:{epsg} ...")

        # Apply CRS directly to the file using GDAL’s metadata editing
        subprocess.run([
            "gdal_edit.py",
            "-a_srs", f"EPSG:{epsg}",
            tif
        ], check=True)

print("...Building virtual mosaic:", end="\t\t", flush=True)
vrt_path = os.path.join(output_folder, "mosaic.vrt")

# 1: Build VRT FIRST
gdal.BuildVRT(vrt_path, tif_paths)

# 2: Open it AFTER it was created
ds = gdal.Open(vrt_path)
epsg = get_epsg(ds)
print("done.")

if not utm_tiles:
    print("...Reprojecting mosaic to Web Mercator:", end="\t", flush=True)
    # Reproject to Web Mercator
    mosaic_path = os.path.join(output_folder, "mosaic_3857.tif")
    gdal.Warp(mosaic_path, vrt_path, dstSRS="EPSG:3857", resampleAlg="cubic")
    print("done.")
    create_preview_png(mosaic_path)
else:
    print("...Checking if already in UTM:", end="\t\t", flush=True)
    if not is_utm(epsg):
        print("Error: EPSG code is not a UTM CRS")
        sys.exit(1)
    print("done.")
    mosaic_path = os.path.join(output_folder, "mosaic_utm.tif")
    print("...Converting VRT to UTM mosaic TIFF:", end="\t", flush=True)
    gdal.Translate(mosaic_path, vrt_path)
    print("done.")
    create_preview_png(mosaic_path)


if not only_mosaic and not utm_tiles:
    print("Load the reprojected mosaic...")
    # Load the reprojected mosaic
    ds = gdal.Open(mosaic_path)
    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()

    # Compute approximate center point in the source coordinate space
    center_x = gt[0] + gt[1] * 0.5
    center_y = gt[3] + gt[5] * 0.5

    # Determine the correct EPSG automatically
    epsg = get_epsg_from_coords(center_x, center_y)
    print(f"{mosaic_path} has CRS: {epsg} ...")

    # Extract image parameters
    width = ds.RasterXSize
    height = ds.RasterYSize
    minx = gt[0]
    maxy = gt[3]
    maxx = minx + width * gt[1]
    miny = maxy + height * gt[5]

    print(f"Web Mercator extent:\n  X: {minx:.2f} – {maxx:.2f}\n  Y: {miny:.2f} – {maxy:.2f}")

    # -----------------------------
    # Compute tile coverage
    # -----------------------------
    def mercator_to_latlon(mx, my):
        """Convert Web Mercator meters to latitude/longitude."""
        lon = (mx / 20037508.34) * 180.0
        lat = (my / 20037508.34) * 180.0
        lat = 180 / math.pi * (2 * math.atan(math.exp(lat * math.pi / 180.0)) - math.pi / 2)
        return lat, lon

    # Convert corners to lat/lon
    lat_max, lon_min = mercator_to_latlon(minx, maxy)
    lat_min, lon_max = mercator_to_latlon(maxx, miny)

    x_min = lon_to_tile_x(lon_min, zoom)
    x_max = lon_to_tile_x(lon_max, zoom)
    y_min = lat_to_tile_y(lat_max, zoom)
    y_max = lat_to_tile_y(lat_min, zoom)

    print(f"Tile range at zoom {zoom}: X {x_min}–{x_max}, Y {y_min}–{y_max}")

    total_tiles = (x_max - x_min + 1) * (y_max - y_min + 1)
    tile_counter = 0

    print(f"Generating {total_tiles} tiles...\n")

    # -----------------------------
    # Create tiles in Web Mercator space
    # -----------------------------
    for x in tqdm(range(x_min, x_max + 1), desc="Processing columns", unit="cols"):
        for y in range(y_min, y_max + 1):
            tile_counter += 1
            lon_min, lat_min, lon_max, lat_max = tile_bounds(x, y, zoom)

            # Convert bounds to Web Mercator meters
            def latlon_to_merc(lat, lon):
                mx = lon * 20037508.34 / 180.0
                my = math.log(math.tan((90 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
                my = my * 20037508.34 / 180.0
                return mx, my

            mx_min, my_min = latlon_to_merc(lat_min, lon_min)
            mx_max, my_max = latlon_to_merc(lat_max, lon_max)

            # Crop and resample using GDAL Warp
            out_dir = os.path.join(output_folder, str(x))
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"{y}.png")

            gdal.Warp(
                out_path,
                mosaic_path,
                outputBounds=(mx_min, my_min, mx_max, my_max),
                width=tile_size,
                height=tile_size,
                dstSRS="EPSG:3857",
                format="PNG",
                resampleAlg="bilinear",
                multithread=True,
                errorThreshold=0.0,
                warpMemoryLimit=512
            )

    print(f"Web Mercator tiles created successfully! ({tile_counter} total)")

elif not only_mosaic and utm_tiles:
    # ================================
    # UTM TILE GENERATION
    # ================================
    if utm_tiles:

        print("Generating UTM tiles...")

        ds = gdal.Open(vrt_path)
        gt = ds.GetGeoTransform()
        proj = ds.GetProjection()
        width = ds.RasterXSize
        height = ds.RasterYSize

        srs = osr.SpatialReference()
        srs.ImportFromWkt(proj)
        epsg = int(srs.GetAttrValue("AUTHORITY", 1))

        if not is_utm(epsg):
            print(f"ERROR: Mosaic CRS EPSG:{epsg} is NOT UTM")
            sys.exit(1)

        print(f"Mosaic CRS is UTM (EPSG:{epsg})")

        # Extract mosaic extent in UTM meters
        minx = gt[0]
        maxy = gt[3]
        maxx = minx + width * gt[1]
        miny = maxy + height * gt[5]

        print(f"UTM extent:\n  X: {minx} – {maxx}\n  Y: {miny} – {maxy}")

        # --- Save extent for your RViz plugin ---
        with open(os.path.join(output_folder, "utm_origin_info.txt"), "w") as f:
            f.write(f"Meter per pixel Zoom 0: {abs(gt[1]) * (1 << zoom)}\n")
            f.write(f"Origin CRS: {epsg}\n")
            f.write(f"Origin X: {minx}\n")
            f.write(f"Origin Y: {maxy}\n")

        pixel_size = abs(gt[1])  # meters per pixel = 0.2m
        print(f"Meters per pixel: {pixel_size}")
        tile_extent_m = tile_size * pixel_size  # 256px * 0.2m/px = 51.2m

        # Tile grid counts
        tiles_x = int(math.ceil((maxx - minx) / tile_extent_m))
        tiles_y = int(math.ceil((maxy - miny) / tile_extent_m))

        print(f"# of UTM tiles: {tiles_x} × {tiles_y}")

        # ---- Create tiles ----
        for tx in tqdm(range(tiles_x), desc="UTM tile columns"):
            for ty in range(tiles_y):
                # Compute tile boundaries in UTM meters
                x0 = minx + tx * tile_extent_m
                x1 = x0 + tile_extent_m

                y1 = maxy - ty * tile_extent_m
                y0 = y1 - tile_extent_m

                out_dir = os.path.join(output_folder, str(tx))
                os.makedirs(out_dir, exist_ok=True)
                out_path = os.path.join(out_dir, f"{ty}.png")

                gdal.Warp(
                    out_path,
                    vrt_path,
                    outputBounds=(x0, y0, x1, y1),
                    width=tile_size,
                    height=tile_size,
                    dstSRS=f"EPSG:{epsg}",
                    format="PNG",
                    resampleAlg="bilinear",
                    multithread=True,
                )
        srs = None
        print("UTM tiles written successfully.")

# Delete mosaic and VRT files
if os.path.exists(mosaic_path):
    os.remove(mosaic_path)
    # print(f"Deleted mosaic: {mosaic_path}") # Debug print
if os.path.exists(vrt_path):
    os.remove(vrt_path)
    # print(f"Deleted VRT: {vrt_path}")       # Debug print

