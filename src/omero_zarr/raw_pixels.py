import argparse
import os
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

import numpy
import numpy as np
import omero.clients  # noqa
import omero.gateway  # required to allow 'from omero_zarr import raw_pixels'
from ome_zarr.writer import (
    write_multiscales_metadata,
    write_plate_metadata,
    write_well_metadata,
)
from omero.rtypes import unwrap
from skimage.transform import resize
from zarr.hierarchy import Array, Group, open_group

from . import __version__
from . import ngff_version as VERSION
from .util import marshal_axes, marshal_transformations, open_store, print_status


def image_to_zarr(image: omero.gateway.ImageWrapper, args: argparse.Namespace) -> None:
    target_dir = args.output
    cache_dir = target_dir if args.cache_numpy else None
    try:
        chunk_size = int(args.chunk_size)
    except TypeError:
        chunk_size  = None

    name = os.path.join(target_dir, "%s.zarr" % image.id)
    print(f"Exporting to {name} ({VERSION})")
    store = open_store(name)
    root = open_group(store)
    add_image(image, root, cache_dir=cache_dir, chunk_size=chunk_size)
    add_omero_metadata(root, image)
    add_toplevel_metadata(root)
    print("Finished.")


def add_image(
    image: omero.gateway.ImageWrapper,
    parent: Group, 
    cache_dir: Optional[str] = None, 
    chunk_size: Union[None, int, Tuple[int, int]] = None,
) -> Tuple[int, List[Dict[str, Any]]]:
    """Adds an OMERO image pixel data as array to the given parent zarr group.
    Optionally caches the pixel data in the given cache_dir directory.
    Returns the number of resolution levels generated for the image.
    """

    def get_cache_filename(z: int, c: int, t: int) -> str:
        assert cache_dir is not None
        return os.path.join(cache_dir, str(image.id), f"{z:03d}-{c:03d}-{t:03d}.npy")

    size_c = image.getSizeC()
    size_z = image.getSizeZ()
    size_x = image.getSizeX()
    size_y = image.getSizeY()
    size_t = image.getSizeT()
    d_type = image.getPixelsType()

    zct_list = []
    for t in range(size_t):
        for c in range(size_c):
            for z in range(size_z):
                if cache_dir is not None:
                    # We only want to load from server if not cached locally
                    filename = get_cache_filename(z, c, t)
                    if not os.path.exists(filename):
                        zct_list.append((z, c, t))
                else:
                    zct_list.append((z, c, t))

    pixels = image.getPrimaryPixels()

    def planeGen() -> np.ndarray:

        planes = pixels.getPlanes(zct_list)
        yield from planes

    planes = planeGen()

    # Target size for smallest multiresolution
    TARGET_SIZE = 96
    level_count = 1
    longest = max(size_x, size_y)
    while longest > TARGET_SIZE:
        longest = longest // 2
        level_count += 1

    paths = add_raw_image(
        planes=planes,
        size_z=size_z,
        size_c=size_c,
        size_t=size_t,
        d_type=d_type,
        parent=parent,
        level_count=level_count,
        cache_dir=cache_dir,
        cache_file_name_func=get_cache_filename,
        chunk_size=chunk_size,
    )

    axes = marshal_axes(image)
    transformations = marshal_transformations(image, len(paths))

    datasets: List[Dict[Any, Any]] = [{"path": path} for path in paths]
    for dataset, transform in zip(datasets, transformations):
        dataset["coordinateTransformations"] = transform

    write_multiscales_metadata(parent, datasets, axes=axes)

    return (level_count, axes)


def add_raw_image(
    *,
    planes: Iterator[np.ndarray],
    size_z: int,
    size_c: int,
    size_t: int,
    d_type: np.dtype,
    parent: Group,
    level_count: int,
    cache_dir: Optional[str] = None,
    cache_file_name_func: Callable[[int, int, int], str] = None,
    chunk_size: Union[None, int, Tuple[int, int]] = None,
) -> List[str]:
    """Adds the raw image pixel data as array to the given parent zarr group.
    Optionally caches the pixel data in the given cache_dir directory.
    Returns the number of resolution levels generated for the image.

    planes: Generator returning planes in order of zct (whatever order
            OMERO returns in its plane generator). Each plane must be a
            numpy array with shape (size_y, sizex), or None to skip the
            plane.
    """

    if cache_dir is not None:
        cache = True
    else:
        cache = False
        cache_dir = ""

    if type(chunk_size) == int:
        chunk_size = (chunk_size, chunk_size)

    dims = [dim for dim in [size_t, size_c, size_z] if dim != 1]

    paths: List[str] = []
    field_groups: List[Array] = []
    for t in range(size_t):
        for c in range(size_c):
            for z in range(size_z):
                if cache:
                    assert cache_file_name_func
                    filename = cache_file_name_func(z, c, t)
                    if os.path.exists(filename):
                        plane = numpy.load(filename)
                    else:
                        plane = next(planes)
                        os.makedirs(os.path.dirname(filename), mode=511, exist_ok=True)
                        numpy.save(filename, plane)
                else:
                    plane = next(planes)
                if plane is None:
                    continue
                for level in range(level_count):
                    size_y = plane.shape[0]
                    size_x = plane.shape[1]
                    # If on first plane, create a new group for this resolution level
                    if len(field_groups) <= level:
                        path = str(level)
                        paths.append(path)
                        field_groups.append(
                            parent.create(
                                path,
                                shape=tuple(dims + [size_y, size_x]),
                                chunks=tuple([1] * len(dims)) + ((size_y, size_x) if chunk_size is None else chunk_size),
                                dtype=d_type,
                            )
                        )

                    indices = []
                    if size_t > 1:
                        indices.append(t)
                    if size_c > 1:
                        indices.append(c)
                    if size_z > 1:
                        indices.append(z)

                    field_groups[level][tuple(indices)] = plane

                    if (level + 1) < level_count:
                        # resize for next level...
                        plane = resize(
                            plane,
                            output_shape=(size_y // 2, size_x // 2),
                            order=0,
                            preserve_range=True,
                            anti_aliasing=False,
                        ).astype(plane.dtype)

    return paths


def marshal_acquisition(acquisition: omero.gateway._PlateAcquisitionWrapper) -> Dict:
    """Marshal a PlateAcquisitionWrapper to JSON"""
    acq = {
        "id": acquisition.id,
        "name": acquisition.name,
        "maximumfieldcount": acquisition.maximumFieldCount,
    }
    if acquisition.description:
        acq["description"] = acquisition.description
    if acquisition.startTime:
        acq["starttime"] = acquisition.startTime
    if acquisition.endTime:
        acq["endtime"] = acquisition.endTime
    return acq


def plate_to_zarr(plate: omero.gateway._PlateWrapper, args: argparse.Namespace) -> None:
    """
    Exports a plate to a zarr file using the hierarchy discussed here ('Option 3'):
    https://github.com/ome/omero-ms-zarr/issues/73#issuecomment-706770955
    """
    gs = plate.getGridSize()
    n_rows = gs["rows"]
    n_cols = gs["columns"]
    n_fields = plate.getNumberOfFields()
    total = n_rows * n_cols * (n_fields[1] - n_fields[0] + 1)

    target_dir = args.output
    cache_dir = target_dir if args.cache_numpy else None
    name = os.path.join(target_dir, "%s.zarr" % plate.id)
    try:
        chunk_size = int(args.chunk_size)
    except TypeError:
        chunk_size  = None

    store = open_store(name)
    print(f"Exporting to {name} ({VERSION})")
    root = open_group(store)

    count = 0
    max_fields = 0
    t0 = time.time()

    well_paths = set()

    col_names = [str(name) for name in plate.getColumnLabels()]
    row_names = [str(name) for name in plate.getRowLabels()]

    # Add acquisitions key if at least one plate acquisition exists
    acquisitions = list(plate.listPlateAcquisitions())
    plate_acq = None
    if acquisitions:
        plate_acq = [marshal_acquisition(x) for x in acquisitions]

    for well in plate.listChildren():
        row = plate.getRowLabels()[well.row]
        col = plate.getColumnLabels()[well.column]
        fields = []
        for field in range(n_fields[0], n_fields[1] + 1):
            count += 1
            ws = well.getWellSample(field)
            if ws and ws.getImage():
                ac = ws.getPlateAcquisition()
                field_name = "%d" % field
                img = ws.getImage()
                well_paths.add(f"{row}/{col}")
                field_info = {"path": f"{field_name}"}
                if ac:
                    field_info["acquisition"] = ac.id
                fields.append(field_info)
                row_group = root.require_group(row)
                col_group = row_group.require_group(col)
                field_group = col_group.require_group(field_name)
                add_image(img, field_group, cache_dir=cache_dir, chunk_size=chunk_size)
                add_omero_metadata(field_group, img)
                # Update Well metadata after each image
                write_well_metadata(col_group, fields)
                max_fields = max(max_fields, field + 1)
            print_status(int(t0), int(time.time()), count, total)

    if len(well_paths) == 0:
        raise ValueError("Empty wells list")

    write_plate_metadata(
        root,
        row_names,
        col_names,
        wells=list(well_paths),
        field_count=max_fields,
        acquisitions=plate_acq,
        name=plate.name,
    )

    add_toplevel_metadata(root)
    print("Finished.")


def add_omero_metadata(zarr_root: Group, image: omero.gateway.ImageWrapper) -> None:

    image_data = {
        "id": 1,
        "channels": [channelMarshal(c) for c in image.getChannels()],
        "rdefs": {
            "model": (image.isGreyscaleRenderingModel() and "greyscale" or "color"),
            "defaultZ": image._re.getDefaultZ(),
            "defaultT": image._re.getDefaultT(),
        },
        "version": VERSION,
    }
    zarr_root.attrs["omero"] = image_data
    image._closeRE()


def add_toplevel_metadata(zarr_root: Group) -> None:

    zarr_root.attrs["_creator"] = {"name": "omero-zarr", "version": __version__}


def channelMarshal(channel: omero.model.Channel) -> Dict[str, Any]:
    return {
        "label": channel.getLabel(),
        "color": channel.getColor().getHtml(),
        "inverted": channel.isInverted(),
        "family": unwrap(channel.getFamily()),
        "coefficient": unwrap(channel.getCoefficient()),
        "window": {
            "min": channel.getWindowMin(),
            "max": channel.getWindowMax(),
            "start": channel.getWindowStart(),
            "end": channel.getWindowEnd(),
        },
        "active": channel.isActive(),
    }
