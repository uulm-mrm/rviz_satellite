import math
import os
import argparse
from osgeo import gdal
from tqdm import tqdm

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
            "COMPRESS=JPEG",      # Oder "DEFLATE" (verlustfrei)
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

# -----------------------------
# Load and merge input TIFs
# -----------------------------
print("Creating a mosaic tiff file out of all input tifs...")

# Automatically find all .tif files in the input folder
tifs = [f for f in os.listdir(input_folder) if f.lower().endswith(".tif")]

# Full paths
tif_paths = [os.path.join(input_folder, f) for f in tifs]

print("...Building virtual mosaic:", end="\t\t", flush=True)
# Build virtual mosaic
vrt_path = os.path.join(output_folder, "mosaic.vrt")
gdal.BuildVRT(vrt_path, tif_paths)
print("done.")

print("...Reprojecting mosaic to Web Mercator:", end="\t", flush=True)
# Reproject to Web Mercator
mosaic_path = os.path.join(output_folder, "mosaic_3857.tif")
gdal.Warp(mosaic_path, vrt_path, dstSRS="EPSG:3857", resampleAlg="cubic")
print("done.")

if not only_mosaic:
    print("Load the reprojected mosaic...")
    # Load the reprojected mosaic
    ds = gdal.Open(mosaic_path)
    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()

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

create_preview_png(mosaic_path)
# Delete mosaic and VRT files
if os.path.exists(mosaic_path):
    os.remove(mosaic_path)
    # print(f"Deleted mosaic: {mosaic_path}") # Debug print
if os.path.exists(vrt_path):
    os.remove(vrt_path)
    # print(f"Deleted VRT: {vrt_path}")       # Debug print

