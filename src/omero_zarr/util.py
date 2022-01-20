import time
from typing import Dict, List

from omero.gateway import ImageWrapper
from zarr.storage import FSStore


def print_status(t0: int, t: int, count: int, total: int) -> None:
    """Prints percent done and ETA.
    t0: start timestamp in seconds
    t: current timestamp in seconds
    count: number of tasks done
    total: total number of tasks
    """
    percent_done = float(count) * 100 / total
    dt = t - t0
    if dt > 0:
        rate = float(count) / (t - t0)
        eta_f = float(total - count) / rate
        eta = time.strftime("%H:%M:%S", time.gmtime(eta_f))
    else:
        eta = "NA"
    status = f"{percent_done:.2f}% done, ETA: {eta}"
    print(status, end="\r", flush=True)


def open_store(name: str) -> FSStore:
    """
    Create an FSStore instance that supports nested storage of chunks.
    """
    return FSStore(
        name,
        auto_mkdir=True,
        key_separator="/",
        normalize_keys=False,
        mode="w",
    )


def marshal_axes(
    image: ImageWrapper, levels: int = 1, multiscales_zoom: float = 2.0
) -> Dict[str, List]:
    # Prepare axes and transformations info...
    size_c = image.getSizeC()
    size_z = image.getSizeZ()
    size_t = image.getSizeT()
    pixel_sizes = {}
    pix_size_x = image.getPixelSizeX(units=True)
    pix_size_y = image.getPixelSizeY(units=True)
    pix_size_z = image.getPixelSizeZ(units=True)
    # All OMERO units.lower() are valid UDUNITS-2 and therefore NGFF spec
    if pix_size_x is not None:
        pixel_sizes["x"] = {
            "units": str(pix_size_x.getUnit()).lower(),
            "value": pix_size_x.getValue(),
        }
    if pix_size_y is not None:
        pixel_sizes["y"] = {
            "units": str(pix_size_y.getUnit()).lower(),
            "value": pix_size_y.getValue(),
        }
    if pix_size_z is not None:
        pixel_sizes["z"] = {
            "units": str(pix_size_z.getUnit()).lower(),
            "value": pix_size_z.getValue(),
        }

    axes = []
    if size_t > 1:
        axes.append({"name": "t", "type": "time"})
    if size_c > 1:
        axes.append({"name": "c", "type": "channel"})
    if size_z > 1:
        axes.append({"name": "z", "type": "space"})
        if pixel_sizes and "z" in pixel_sizes:
            axes[-1]["units"] = pixel_sizes["z"]["units"]
    # last 2 dimensions are always y and x
    for dim in ("y", "x"):
        axes.append({"name": dim, "type": "space"})
        if pixel_sizes and dim in pixel_sizes:
            axes[-1]["units"] = pixel_sizes[dim]["units"]

    # Each path needs a transformations list...
    transformations = []
    zooms = {"x": 1.0, "y": 1.0, "z": 1.0}
    for level in range(levels):
        # {"type": "scale", "scale": [2.0, 2.0, 2.0], "axisIndices": [2, 3, 4]}
        scales = []
        axisIndices = []
        for index, axis in enumerate(axes):
            if axis["name"] in pixel_sizes:
                scales.append(zooms[axis["name"]] * pixel_sizes[axis["name"]]["value"])
                axisIndices.append(index)
        # ...with a single 'scale' transformation each
        if len(scales) > 0:
            transformations.append(
                [{"type": "scale", "scale": scales, "axisIndices": axisIndices}]
            )
        # NB we rescale X and Y for each level, but not Z
        zooms["x"] = zooms["x"] * multiscales_zoom
        zooms["y"] = zooms["y"] * multiscales_zoom

    return {"axes": axes, "transformations": transformations}
