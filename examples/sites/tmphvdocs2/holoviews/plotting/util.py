from __future__ import unicode_literals
from collections import defaultdict
import traceback

import numpy as np
import param

from ..core import (HoloMap, DynamicMap, CompositeOverlay, Layout,
                    Overlay, GridSpace, NdLayout, Store)
from ..core.spaces import get_nested_streams
from ..core.util import (match_spec, is_number, wrap_tuple, basestring,
                         get_overlay_spec, unique_iterator)
from ..element import Area, Polygons
from ..operation.stats import univariate_kde, bivariate_kde, Operation
from ..streams import LinkedStream

def displayable(obj):
    """
    Predicate that returns whether the object is displayable or not
    (i.e whether the object obeys the nesting hierarchy
    """
    if isinstance(obj, Overlay) and any(isinstance(o, (HoloMap, GridSpace))
                                        for o in obj):
        return False
    if isinstance(obj, HoloMap):
        return not (obj.type in [Layout, GridSpace, NdLayout])
    if isinstance(obj, (GridSpace, Layout, NdLayout)):
        for el in obj.values():
            if not displayable(el):
                return False
        return True
    return True


class Warning(param.Parameterized): pass
display_warning = Warning(name='Warning')

def collate(obj):
    if isinstance(obj, Overlay):
        nested_type = [type(o).__name__ for o in obj
                       if isinstance(o, (HoloMap, GridSpace))][0]
        display_warning.warning("Nesting %ss within an Overlay makes it difficult "
                                "to access your data or control how it appears; "
                                "we recommend calling .collate() on the Overlay "
                                "in order to follow the recommended nesting "
                                "structure shown in the Composing Data tutorial"
                                "(http://goo.gl/2YS8LJ)" % nested_type)

        return obj.collate()
    if isinstance(obj, DynamicMap):
        return obj.collate()
    if isinstance(obj, HoloMap):
        display_warning.warning("Nesting {0}s within a {1} makes it difficult "
                                "to access your data or control how it appears; "
                                "we recommend calling .collate() on the {1} "
                                "in order to follow the recommended nesting "
                                "structure shown in the Composing Data tutorial"
                                "(https://goo.gl/2YS8LJ)".format(obj.type.__name__, type(obj).__name__))
        return obj.collate()
    elif isinstance(obj, (Layout, NdLayout)):
        try:
            display_warning.warning(
                "Layout contains HoloMaps which are not nested in the "
                "recommended format for accessing your data; calling "
                ".collate() on these objects will resolve any violations "
                "of the recommended nesting presented in the Composing Data "
                "tutorial (https://goo.gl/2YS8LJ)")
            expanded = []
            for el in obj.values():
                if isinstance(el, HoloMap) and not displayable(el):
                    collated_layout = Layout.from_values(el.collate())
                    expanded.extend(collated_layout.values())
            return Layout(expanded)
        except:
            raise Exception(undisplayable_info(obj))
    else:
        raise Exception(undisplayable_info(obj))


def isoverlay_fn(obj):
    """
    Determines whether object is a DynamicMap returning (Nd)Overlay types.
    """
    return isinstance(obj, DynamicMap) and (isinstance(obj.last, CompositeOverlay))


def overlay_depth(obj):
    """
    Computes the depth of a DynamicMap overlay if it can be determined
    otherwise return None.
    """
    if isinstance(obj, DynamicMap):
        if isinstance(obj.last, CompositeOverlay):
            return len(obj.last)
        elif obj.last is None:
            return None
        return 1
    else:
        return 1


def compute_overlayable_zorders(obj, path=[]):
    """
    Traverses an overlayable composite container to determine which
    objects are associated with specific (Nd)Overlay layers by
    z-order, making sure to take DynamicMap Callables into
    account. Returns a mapping between the zorders of each layer and a
    corresponding lists of objects.

    Used to determine which overlaid subplots should be linked with
    Stream callbacks.
    """
    path = path+[obj]
    zorder_map = defaultdict(list)

    # Process non-dynamic layers
    if not isinstance(obj, DynamicMap):
        if isinstance(obj, CompositeOverlay):
            for z, o in enumerate(obj):
                zorder_map[z] = [o, obj]
        else:
            if obj not in zorder_map[0]:
                zorder_map[0].append(obj)
        return zorder_map

    isoverlay = isinstance(obj.last, CompositeOverlay)
    isdynoverlay = obj.callback._is_overlay
    if obj not in zorder_map[0] and not isoverlay:
        zorder_map[0].append(obj)
    depth = overlay_depth(obj)

    # Process the inputs of the DynamicMap callback
    dmap_inputs = obj.callback.inputs if obj.callback.link_inputs else []
    for z, inp in enumerate(dmap_inputs):
        no_zorder_increment = False
        if any(not (isoverlay_fn(p) or p.last is None) for p in path) and isoverlay_fn(inp):
            # If overlay has been collapsed do not increment zorder
            no_zorder_increment = True

        input_depth = overlay_depth(inp)
        if depth is not None and input_depth is not None and depth < input_depth:
            # Skips branch of graph where the number of elements in an
            # overlay has been reduced but still contains more than one layer
            if depth > 1:
                continue
            else:
                no_zorder_increment = True

        # Recurse into DynamicMap.callback.inputs and update zorder_map
        z = z if isdynoverlay else 0
        deep_zorders = compute_overlayable_zorders(inp, path=path)
        offset = max(zorder_map.keys())
        for dz, objs in deep_zorders.items():
            global_z = offset+z if no_zorder_increment else offset+dz+z
            zorder_map[global_z] = list(unique_iterator(zorder_map[global_z]+objs))

    # If object branches but does not declare inputs (e.g. user defined
    # DynamicMaps returning (Nd)Overlay) add the items on the DynamicMap.last
    found = any(isinstance(p, DynamicMap) and p.callback._is_overlay for p in path)
    linked =  any(isinstance(s, LinkedStream) and s.linked for s in obj.streams)
    if (found or linked) and isoverlay and not isdynoverlay:
        offset = max(zorder_map.keys())
        for z, o in enumerate(obj.last):
            if isoverlay and linked:
                zorder_map[offset+z].append(obj)
            if o not in zorder_map[offset+z]:
                zorder_map[offset+z].append(o)
    return zorder_map


def initialize_dynamic(obj):
    """
    Initializes all DynamicMap objects contained by the object
    """
    dmaps = obj.traverse(lambda x: x, specs=[DynamicMap])
    for dmap in dmaps:
        if dmap.unbounded:
            # Skip initialization until plotting code
            continue
        if not len(dmap):
            dmap[dmap._initial_key()]


def get_plot_frame(map_obj, key_map, cached=False):
    """
    Returns an item in a HoloMap or DynamicMap given a mapping key
    dimensions and their values.
    """
    if map_obj.kdims and len(map_obj.kdims) == 1 and map_obj.kdims[0] == 'Frame':
        # Special handling for static plots
        return map_obj.last
    key = tuple(key_map[kd.name] for kd in map_obj.kdims)
    if key in map_obj.data and cached:
        return map_obj.data[key]
    else:
        try:
            return map_obj[key]
        except KeyError:
            return None
        except StopIteration as e:
            raise e
        except Exception:
            print(traceback.format_exc())
            return None


def undisplayable_info(obj, html=False):
    "Generate helpful message regarding an undisplayable object"

    collate = '<tt>collate</tt>' if html else 'collate'
    info = "For more information, please consult the Composing Data tutorial (http://git.io/vtIQh)"
    if isinstance(obj, HoloMap):
        error = "HoloMap of %s objects cannot be displayed." % obj.type.__name__
        remedy = "Please call the %s method to generate a displayable object" % collate
    elif isinstance(obj, Layout):
        error = "Layout containing HoloMaps of Layout or GridSpace objects cannot be displayed."
        remedy = "Please call the %s method on the appropriate elements." % collate
    elif isinstance(obj, GridSpace):
        error = "GridSpace containing HoloMaps of Layouts cannot be displayed."
        remedy = "Please call the %s method on the appropriate elements." % collate

    if not html:
        return '\n'.join([error, remedy, info])
    else:
        return "<center>{msg}</center>".format(msg=('<br>'.join(
            ['<b>%s</b>' % error, remedy, '<i>%s</i>' % info])))


def compute_sizes(sizes, size_fn, scaling_factor, scaling_method, base_size):
    """
    Scales point sizes according to a scaling factor,
    base size and size_fn, which will be applied before
    scaling.
    """
    if sizes.dtype.kind not in ('i', 'f'):
        return None
    if scaling_method == 'area':
        pass
    elif scaling_method == 'width':
        scaling_factor = scaling_factor**2
    else:
        raise ValueError(
            'Invalid value for argument "scaling_method": "{}". '
            'Valid values are: "width", "area".'.format(scaling_method))
    sizes = size_fn(sizes)
    return (base_size*scaling_factor*sizes)


def get_sideplot_ranges(plot, element, main, ranges):
    """
    Utility to find the range for an adjoined
    plot given the plot, the element, the
    Element the plot is adjoined to and the
    dictionary of ranges.
    """
    key = plot.current_key
    dims = element.dimensions()
    dim = dims[0] if 'frequency' in dims[1].name else dims[1]
    range_item = main
    if isinstance(main, HoloMap):
        if issubclass(main.type, CompositeOverlay):
            range_item = [hm for hm in main.split_overlays()[1]
                          if dim in hm.dimensions('all')][0]
    else:
        range_item = HoloMap({0: main}, kdims=['Frame'])
        ranges = match_spec(range_item.last, ranges)

    if dim.name in ranges:
        main_range = ranges[dim.name]
    else:
        framewise = plot.lookup_options(range_item.last, 'norm').options.get('framewise')
        if framewise and range_item.get(key, False):
            main_range = range_item[key].range(dim)
        else:
            main_range = range_item.range(dim)

    # If .main is an NdOverlay or a HoloMap of Overlays get the correct style
    if isinstance(range_item, HoloMap):
        range_item = range_item.last
    if isinstance(range_item, CompositeOverlay):
        range_item = [ov for ov in range_item
                      if dim in ov.dimensions('all')][0]
    return range_item, main_range, dim


def within_range(range1, range2):
    """Checks whether range1 is within the range specified by range2."""
    range1 = [r if np.isfinite(r) else None for r in range1]
    range2 = [r if np.isfinite(r) else None for r in range2]
    return ((range1[0] is None or range2[0] is None or range1[0] >= range2[0]) and
            (range1[1] is None or range2[1] is None or range1[1] <= range2[1]))


def validate_unbounded_mode(holomaps, dynmaps):
    composite = HoloMap(enumerate(holomaps), kdims=['testing_kdim'])
    holomap_kdims = set(unique_iterator([kd.name for dm in holomaps for kd in dm.kdims]))
    hmranges = {d: composite.range(d) for d in holomap_kdims}
    if any(not set(d.name for d in dm.kdims) <= holomap_kdims
                        for dm in dynmaps):
        raise Exception('DynamicMap that are unbounded must have key dimensions that are a '
                        'subset of dimensions of the HoloMap(s) defining the keys.')
    elif not all(within_range(hmrange, dm.range(d)) for dm in dynmaps
                              for d, hmrange in hmranges.items() if d in dm.kdims):
        raise Exception('HoloMap(s) have keys outside the ranges specified on '
                        'the DynamicMap(s).')


def get_dynamic_mode(composite):
    "Returns the common mode of the dynamic maps in given composite object"
    dynmaps = composite.traverse(lambda x: x, [DynamicMap])
    holomaps = composite.traverse(lambda x: x, ['HoloMap'])
    dynamic_unbounded = any(m.unbounded for m in dynmaps)
    if holomaps:
        validate_unbounded_mode(holomaps, dynmaps)
    elif dynamic_unbounded and not holomaps:
        raise Exception("DynamicMaps in unbounded mode must be displayed alongside "
                        "a HoloMap to define the sampling.")
    return dynmaps and not holomaps, dynamic_unbounded


def initialize_unbounded(obj, dimensions, key):
    """
    Initializes any DynamicMaps in unbounded mode.
    """
    select = dict(zip([d.name for d in dimensions], key))
    try:
        obj.select([DynamicMap], **select)
    except KeyError:
        pass


def save_frames(obj, filename, fmt=None, backend=None, options=None):
    """
    Utility to export object to files frame by frame, numbered individually.
    Will use default backend and figure format by default.
    """
    backend = Store.current_backend if backend is None else backend
    renderer = Store.renderers[backend]
    fmt = renderer.params('fig').objects[0] if fmt is None else fmt
    plot = renderer.get_plot(obj)
    for i in range(len(plot)):
        plot.update(i)
        renderer.save(plot, '%s_%s' % (filename, i), fmt=fmt, options=options)


def dynamic_update(plot, subplot, key, overlay, items):
    """
    Given a plot, subplot and dynamically generated (Nd)Overlay
    find the closest matching Element for that plot.
    """
    match_spec = get_overlay_spec(overlay,
                                  wrap_tuple(key),
                                  subplot.current_frame)
    specs = [(i, get_overlay_spec(overlay, wrap_tuple(k), el))
             for i, (k, el) in enumerate(items)]
    return closest_match(match_spec, specs)


def closest_match(match, specs, depth=0):
    """
    Recursively iterates over type, group, label and overlay key,
    finding the closest matching spec.
    """
    new_specs = []
    match_lengths = []
    for i, spec in specs:
        if spec[0] == match[0]:
            new_specs.append((i, spec[1:]))
        else:
            if is_number(match[0]) and is_number(spec[0]):
                match_length = -abs(match[0]-spec[0])
            elif all(isinstance(s[0], basestring) for s in [spec, match]):
                match_length = max(i for i in range(len(match[0]))
                                   if match[0].startswith(spec[0][:i]))
            else:
                match_length = 0
            match_lengths.append((i, match_length, spec[0]))

    if len(new_specs) == 1:
        return new_specs[0][0]
    elif new_specs:
        depth = depth+1
        return closest_match(match[1:], new_specs, depth)
    else:
        if depth == 0 or not match_lengths:
            return None
        else:
            return sorted(match_lengths, key=lambda x: -x[1])[0][0]


def map_colors(arr, crange, cmap, hex=True):
    """
    Maps an array of values to RGB hex strings, given
    a color range and colormap.
    """
    if isinstance(crange, np.ndarray):
        xsorted = np.argsort(crange)
        ypos = np.searchsorted(crange[xsorted], arr)
        arr = xsorted[ypos]
    else:
        if isinstance(crange, tuple):
            cmin, cmax = crange
        else:
            cmin, cmax = np.nanmin(arr), np.nanmax(arr)
        arr = (arr - cmin) / (cmax-cmin)
        arr = np.ma.array(arr, mask=np.logical_not(np.isfinite(arr)))
    arr = cmap(arr)
    if hex:
        arr *= 255
        return ["#{0:02x}{1:02x}{2:02x}".format(*(int(v) for v in c[:-1]))
                for c in arr]
    else:
        return arr


def dim_axis_label(dimensions, separator=', '):
    """
    Returns an axis label for one or more dimensions.
    """
    if not isinstance(dimensions, list): dimensions = [dimensions]
    return separator.join([d.pprint_label for d in dimensions])


def attach_streams(plot, obj, precedence=1.1):
    """
    Attaches plot refresh to all streams on the object.
    """
    def append_refresh(dmap):
        for stream in get_nested_streams(dmap):
            if plot.refresh not in stream._subscribers:
                stream.add_subscriber(plot.refresh, precedence)
    return obj.traverse(append_refresh, [DynamicMap])


def traverse_setter(obj, attribute, value):
    """
    Traverses the object and sets the supplied attribute on the
    object. Supports Dimensioned and DimensionedPlot types.
    """
    obj.traverse(lambda x: setattr(x, attribute, value))


def get_min_distance(element):
    """
    Gets the minimum sampling distance of the x- and y-coordinates
    in a grid.
    """
    xys = element.array([0, 1]).astype('float64').view(dtype=np.complex128)
    m, n = np.meshgrid(xys, xys)
    distances = np.abs(m-n)
    np.fill_diagonal(distances, np.inf)
    distances = distances[distances>0]
    if len(distances):
        return distances.min()
    return 0


def rgb2hex(rgb):
    """
    Convert RGB(A) tuple to hex.
    """
    if len(rgb) > 3:
        rgb = rgb[:-1]
    return "#{0:02x}{1:02x}{2:02x}".format(*(int(v*255) for v in rgb))


# linear_kryw_0_100_c71 (aka "fire"):
# A perceptually uniform equivalent of matplotlib's "hot" colormap, from
# http://peterkovesi.com/projects/colourmaps

fire_colors = linear_kryw_0_100_c71 = [\
[0,        0,           0         ],  [0.027065, 2.143e-05,   0         ],
[0.052054, 7.4728e-05,  0         ],  [0.071511, 0.00013914,  0         ],
[0.08742,  0.0002088,   0         ],  [0.10109,  0.00028141,  0         ],
[0.11337,  0.000356,    2.4266e-17],  [0.12439,  0.00043134,  3.3615e-17],
[0.13463,  0.00050796,  2.1604e-17],  [0.14411,  0.0005856,   0         ],
[0.15292,  0.00070304,  0         ],  [0.16073,  0.0013432,   0         ],
[0.16871,  0.0014516,   0         ],  [0.17657,  0.0012408,   0         ],
[0.18364,  0.0015336,   0         ],  [0.19052,  0.0017515,   0         ],
[0.19751,  0.0015146,   0         ],  [0.20401,  0.0015249,   0         ],
[0.20994,  0.0019639,   0         ],  [0.21605,  0.002031,    0         ],
[0.22215,  0.0017559,   0         ],  [0.22808,  0.001546,    1.8755e-05],
[0.23378,  0.0016315,   3.5012e-05],  [0.23955,  0.0017194,   3.3352e-05],
[0.24531,  0.0018097,   1.8559e-05],  [0.25113,  0.0019038,   1.9139e-05],
[0.25694,  0.0020015,   3.5308e-05],  [0.26278,  0.0021017,   3.2613e-05],
[0.26864,  0.0022048,   2.0338e-05],  [0.27451,  0.0023119,   2.2453e-05],
[0.28041,  0.0024227,   3.6003e-05],  [0.28633,  0.0025363,   2.9817e-05],
[0.29229,  0.0026532,   1.9559e-05],  [0.29824,  0.0027747,   2.7666e-05],
[0.30423,  0.0028999,   3.5752e-05],  [0.31026,  0.0030279,   2.3231e-05],
[0.31628,  0.0031599,   1.2902e-05],  [0.32232,  0.0032974,   3.2915e-05],
[0.32838,  0.0034379,   3.2803e-05],  [0.33447,  0.0035819,   2.0757e-05],
[0.34057,  0.003731,    2.3831e-05],  [0.34668,  0.0038848,   3.502e-05 ],
[0.35283,  0.0040418,   2.4468e-05],  [0.35897,  0.0042032,   1.1444e-05],
[0.36515,  0.0043708,   3.2793e-05],  [0.37134,  0.0045418,   3.012e-05 ],
[0.37756,  0.0047169,   1.4846e-05],  [0.38379,  0.0048986,   2.796e-05 ],
[0.39003,  0.0050848,   3.2782e-05],  [0.3963,   0.0052751,   1.9244e-05],
[0.40258,  0.0054715,   2.2667e-05],  [0.40888,  0.0056736,   3.3223e-05],
[0.41519,  0.0058798,   2.159e-05 ],  [0.42152,  0.0060922,   1.8214e-05],
[0.42788,  0.0063116,   3.2525e-05],  [0.43424,  0.0065353,   2.2247e-05],
[0.44062,  0.006765,    1.5852e-05],  [0.44702,  0.0070024,   3.1769e-05],
[0.45344,  0.0072442,   2.1245e-05],  [0.45987,  0.0074929,   1.5726e-05],
[0.46631,  0.0077499,   3.0976e-05],  [0.47277,  0.0080108,   1.8722e-05],
[0.47926,  0.0082789,   1.9285e-05],  [0.48574,  0.0085553,   3.0063e-05],
[0.49225,  0.0088392,   1.4313e-05],  [0.49878,  0.0091356,   2.3404e-05],
[0.50531,  0.0094374,   2.8099e-05],  [0.51187,  0.0097365,   6.4695e-06],
[0.51844,  0.010039,    2.5791e-05],  [0.52501,  0.010354,    2.4393e-05],
[0.53162,  0.010689,    1.6037e-05],  [0.53825,  0.011031,    2.7295e-05],
[0.54489,  0.011393,    1.5848e-05],  [0.55154,  0.011789,    2.3111e-05],
[0.55818,  0.012159,    2.5416e-05],  [0.56485,  0.012508,    1.5064e-05],
[0.57154,  0.012881,    2.541e-05 ],  [0.57823,  0.013283,    1.6166e-05],
[0.58494,  0.013701,    2.263e-05 ],  [0.59166,  0.014122,    2.3316e-05],
[0.59839,  0.014551,    1.9432e-05],  [0.60514,  0.014994,    2.4323e-05],
[0.6119,   0.01545,     1.3929e-05],  [0.61868,  0.01592,     2.1615e-05],
[0.62546,  0.016401,    1.5846e-05],  [0.63226,  0.016897,    2.0838e-05],
[0.63907,  0.017407,    1.9549e-05],  [0.64589,  0.017931,    2.0961e-05],
[0.65273,  0.018471,    2.0737e-05],  [0.65958,  0.019026,    2.0621e-05],
[0.66644,  0.019598,    2.0675e-05],  [0.67332,  0.020187,    2.0301e-05],
[0.68019,  0.020793,    2.0029e-05],  [0.68709,  0.021418,    2.0088e-05],
[0.69399,  0.022062,    1.9102e-05],  [0.70092,  0.022727,    1.9662e-05],
[0.70784,  0.023412,    1.7757e-05],  [0.71478,  0.024121,    1.8236e-05],
[0.72173,  0.024852,    1.4944e-05],  [0.7287,   0.025608,    2.0245e-06],
[0.73567,  0.02639,     1.5013e-07],  [0.74266,  0.027199,    0         ],
[0.74964,  0.028038,    0         ],  [0.75665,  0.028906,    0         ],
[0.76365,  0.029806,    0         ],  [0.77068,  0.030743,    0         ],
[0.77771,  0.031711,    0         ],  [0.78474,  0.032732,    0         ],
[0.79179,  0.033741,    0         ],  [0.79886,  0.034936,    0         ],
[0.80593,  0.036031,    0         ],  [0.81299,  0.03723,     0         ],
[0.82007,  0.038493,    0         ],  [0.82715,  0.039819,    0         ],
[0.83423,  0.041236,    0         ],  [0.84131,  0.042647,    0         ],
[0.84838,  0.044235,    0         ],  [0.85545,  0.045857,    0         ],
[0.86252,  0.047645,    0         ],  [0.86958,  0.049578,    0         ],
[0.87661,  0.051541,    0         ],  [0.88365,  0.053735,    0         ],
[0.89064,  0.056168,    0         ],  [0.89761,  0.058852,    0         ],
[0.90451,  0.061777,    0         ],  [0.91131,  0.065281,    0         ],
[0.91796,  0.069448,    0         ],  [0.92445,  0.074684,    0         ],
[0.93061,  0.08131,     0         ],  [0.93648,  0.088878,    0         ],
[0.94205,  0.097336,    0         ],  [0.9473,   0.10665,     0         ],
[0.9522,   0.1166,      0         ],  [0.95674,  0.12716,     0         ],
[0.96094,  0.13824,     0         ],  [0.96479,  0.14963,     0         ],
[0.96829,  0.16128,     0         ],  [0.97147,  0.17303,     0         ],
[0.97436,  0.18489,     0         ],  [0.97698,  0.19672,     0         ],
[0.97934,  0.20846,     0         ],  [0.98148,  0.22013,     0         ],
[0.9834,   0.23167,     0         ],  [0.98515,  0.24301,     0         ],
[0.98672,  0.25425,     0         ],  [0.98815,  0.26525,     0         ],
[0.98944,  0.27614,     0         ],  [0.99061,  0.28679,     0         ],
[0.99167,  0.29731,     0         ],  [0.99263,  0.30764,     0         ],
[0.9935,   0.31781,     0         ],  [0.99428,  0.3278,      0         ],
[0.995,    0.33764,     0         ],  [0.99564,  0.34735,     0         ],
[0.99623,  0.35689,     0         ],  [0.99675,  0.3663,      0         ],
[0.99722,  0.37556,     0         ],  [0.99765,  0.38471,     0         ],
[0.99803,  0.39374,     0         ],  [0.99836,  0.40265,     0         ],
[0.99866,  0.41145,     0         ],  [0.99892,  0.42015,     0         ],
[0.99915,  0.42874,     0         ],  [0.99935,  0.43724,     0         ],
[0.99952,  0.44563,     0         ],  [0.99966,  0.45395,     0         ],
[0.99977,  0.46217,     0         ],  [0.99986,  0.47032,     0         ],
[0.99993,  0.47838,     0         ],  [0.99997,  0.48638,     0         ],
[1,        0.4943,      0         ],  [1,        0.50214,     0         ],
[1,        0.50991,     1.2756e-05],  [1,        0.51761,     4.5388e-05],
[1,        0.52523,     9.6977e-05],  [1,        0.5328,      0.00016858],
[1,        0.54028,     0.0002582 ],  [1,        0.54771,     0.00036528],
[1,        0.55508,     0.00049276],  [1,        0.5624,      0.00063955],
[1,        0.56965,     0.00080443],  [1,        0.57687,     0.00098902],
[1,        0.58402,     0.0011943 ],  [1,        0.59113,     0.0014189 ],
[1,        0.59819,     0.0016626 ],  [1,        0.60521,     0.0019281 ],
[1,        0.61219,     0.0022145 ],  [1,        0.61914,     0.0025213 ],
[1,        0.62603,     0.0028496 ],  [1,        0.6329,      0.0032006 ],
[1,        0.63972,     0.0035741 ],  [1,        0.64651,     0.0039701 ],
[1,        0.65327,     0.0043898 ],  [1,        0.66,        0.0048341 ],
[1,        0.66669,     0.005303  ],  [1,        0.67336,     0.0057969 ],
[1,        0.67999,     0.006317  ],  [1,        0.68661,     0.0068648 ],
[1,        0.69319,     0.0074406 ],  [1,        0.69974,     0.0080433 ],
[1,        0.70628,     0.0086756 ],  [1,        0.71278,     0.0093486 ],
[1,        0.71927,     0.010023  ],  [1,        0.72573,     0.010724  ],
[1,        0.73217,     0.011565  ],  [1,        0.73859,     0.012339  ],
[1,        0.74499,     0.01316   ],  [1,        0.75137,     0.014042  ],
[1,        0.75772,     0.014955  ],  [1,        0.76406,     0.015913  ],
[1,        0.77039,     0.016915  ],  [1,        0.77669,     0.017964  ],
[1,        0.78298,     0.019062  ],  [1,        0.78925,     0.020212  ],
[1,        0.7955,      0.021417  ],  [1,        0.80174,     0.02268   ],
[1,        0.80797,     0.024005  ],  [1,        0.81418,     0.025396  ],
[1,        0.82038,     0.026858  ],  [1,        0.82656,     0.028394  ],
[1,        0.83273,     0.030013  ],  [1,        0.83889,     0.031717  ],
[1,        0.84503,     0.03348   ],  [1,        0.85116,     0.035488  ],
[1,        0.85728,     0.037452  ],  [1,        0.8634,      0.039592  ],
[1,        0.86949,     0.041898  ],  [1,        0.87557,     0.044392  ],
[1,        0.88165,     0.046958  ],  [1,        0.88771,     0.04977   ],
[1,        0.89376,     0.052828  ],  [1,        0.8998,      0.056209  ],
[1,        0.90584,     0.059919  ],  [1,        0.91185,     0.063925  ],
[1,        0.91783,     0.068579  ],  [1,        0.92384,     0.073948  ],
[1,        0.92981,     0.080899  ],  [1,        0.93576,     0.090648  ],
[1,        0.94166,     0.10377   ],  [1,        0.94752,     0.12051   ],
[1,        0.9533,      0.14149   ],  [1,        0.959,       0.1672    ],
[1,        0.96456,     0.19823   ],  [1,        0.96995,     0.23514   ],
[1,        0.9751,      0.2786    ],  [1,        0.97992,     0.32883   ],
[1,        0.98432,     0.38571   ],  [1,        0.9882,      0.44866   ],
[1,        0.9915,      0.51653   ],  [1,        0.99417,     0.58754   ],
[1,        0.99625,     0.65985   ],  [1,        0.99778,     0.73194   ],
[1,        0.99885,     0.80259   ],  [1,        0.99953,     0.87115   ],
[1,        0.99989,     0.93683   ],  [1,        1,           1         ]]

# Bokeh palette
fire = [str('#{0:02x}{1:02x}{2:02x}'.format(int(r*255),int(g*255),int(b*255)))
        for r,g,b in fire_colors]

# Matplotlib colormap
try:
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.cm import register_cmap
    fire_cmap   = LinearSegmentedColormap.from_list("fire",   fire_colors, N=len(fire_colors))
    fire_r_cmap = LinearSegmentedColormap.from_list("fire_r", list(reversed(fire_colors)), N=len(fire_colors))
    register_cmap("fire", cmap=fire_cmap)
    register_cmap("fire_r", cmap=fire_r_cmap)
except ImportError:
    pass
