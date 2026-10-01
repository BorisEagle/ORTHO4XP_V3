"""Read-only attribution of installed Ortho4XP DSF heights. See README.md."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import re
import struct
import sys
import time
import urllib.request

import numpy as np

VERSION = '1.1.1'
ORTHO_DEFAULT = r'E:\GAMES\ORTHO4XP_V3-3.6'
SCENERY_DEFAULT = r'E:\SteamLibrary\steamapps\common\X-Plane 12\Custom Scenery'
HERE = Path(__file__).resolve().parent
TILE_RE = re.compile(r'([+-]\d{2})([+-]\d{3})$')


def runtime_defaults(here=HERE, home=None):
    """Find an installed app and its dedicated height report directory."""
    here = Path(here)
    candidate = here.parents[1]
    ortho = candidate if (candidate/'Ortho4XP.py').is_file() else Path(ORTHO_DEFAULT)
    return ortho, Path(SCENERY_DEFAULT), ortho/'Height-Reports'


def fingerprint(path):
    path = Path(path)
    if not path.is_file():
        return {'path': str(path), 'missing': True}
    st = path.stat()
    with path.open('rb') as f:
        head = f.read(4096)
        f.seek(max(0, st.st_size-4096))
        tail = f.read(4096)
    return {'path': str(path.resolve()), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
            'edge_sha256': hashlib.sha256(head+tail).hexdigest()}


def sample_grid(a, x, y, nodata=None):
    """Bilinear sampling with zero-based *sample centre* coordinates."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    result = np.full(x.shape, np.nan)
    ny, nx = a.shape
    valid = np.isfinite(x) & np.isfinite(y) & (x >= 0) & (x <= nx-1) & (y >= 0) & (y <= ny-1)
    if not valid.any() or min(nx, ny) < 2:
        return result
    xx, yy = x[valid], y[valid]
    ix = np.minimum(np.floor(xx).astype(int), nx-2)
    iy = np.minimum(np.floor(yy).astype(int), ny-2)
    fx, fy = xx-ix, yy-iy
    v = np.stack([a[iy, ix], a[iy, ix+1], a[iy+1, ix], a[iy+1, ix+1]]).astype(float)
    ok = np.isfinite(v).all(axis=0)
    if nodata is not None:
        ok &= (v != nodata).all(axis=0)
    weights = np.stack([(1-fx)*(1-fy), fx*(1-fy), (1-fx)*fy, fx*fy])
    val = np.sum(v*weights, axis=0)
    val[~ok] = np.nan
    result[valid] = val
    return result


def _atoms(data, lo, hi):
    while lo < hi:
        if lo+8 > hi:
            raise ValueError('Truncated DSF atom')
        kind, size = struct.unpack_from('<4sI', data, lo)
        if size < 8 or lo+size > hi:
            raise ValueError('Invalid DSF atom size')
        yield kind, lo+8, lo+size
        lo += size


def _pool(data, a, b):
    count, planes = struct.unpack_from('<IB', data, a)
    if count > 65536 or planes > 32 or planes == 0:
        raise ValueError('Unsupported pool size or plane count')
    a += 5
    columns = []
    for plane in range(planes):
        mode = data[a]
        a += 1
        if mode not in (0, 1, 2, 3):
            raise ValueError('Unsupported pool encoding')
        if mode < 2:
            if a+count*2 > b:
                raise ValueError('Truncated pool')
            v = np.frombuffer(data, '<u2', count, a).copy()
            a += count*2
        else:
            v = np.empty(count, dtype=np.uint16)
            pos = 0
            while pos < count:
                if a >= b:
                    raise ValueError('Truncated RLE pool')
                control = data[a]
                a += 1
                n = control & 127
                if not n or pos+n > count:
                    raise ValueError('Invalid RLE run')
                size = 2 if control & 128 else n*2
                if a+size > b:
                    raise ValueError('Truncated RLE run')
                if control & 128:
                    v[pos:pos+n] = struct.unpack_from('<H', data, a)[0]
                else:
                    v[pos:pos+n] = np.frombuffer(data, '<u2', n, a)
                a += size
                pos += n
        if mode & 1:
            v = (np.cumsum(v, dtype=np.uint64) & 65535).astype(np.uint16)
        if plane < 3:
            columns.append(v)
    if a != b:
        raise ValueError('Unexpected pool payload')
    return np.column_stack(columns), planes


def _terrain_kind(name, folder):
    if name == 'terrain_Water' or re.search(r'_(sea|water)(?:_overlay)?\.ter$', name):
        return 'water'
    path = folder/name
    if not path.is_file():
        raise ValueError('Missing or unsupported terrain definition: '+name)
    text = path.read_text(encoding='utf-8', errors='replace')
    if any(line.strip().split(' ')[0] in ('WATER', 'WATER_COLOR_MASK') for line in text.splitlines()):
        return 'water'
    return 'land'


def _references(data, a, b, counts, terrains, folder):
    masks = [np.zeros(n, bool) for n in counts]
    pool = definition = 0
    flags = 1
    active = None
    physical_land = physical_water = 0
    kinds = {}
    materials = {}

    def take(fmt):
        nonlocal a
        size = struct.calcsize(fmt)
        if a+size > b:
            raise ValueError('Truncated DSF command')
        value = struct.unpack_from(fmt, data, a)
        a += size
        return value[0] if len(value) == 1 else value

    def indices(n, cross=False):
        nonlocal a
        size = n*(4 if cross else 2)
        if a+size > b:
            raise ValueError('Truncated terrain indices')
        v = np.frombuffer(data, '<u2', n*(2 if cross else 1), a)
        a += size
        if active == 'land':
            if cross:
                pairs = v.reshape(-1, 2)
                for p in np.unique(pairs[:, 0]):
                    add(int(p), pairs[pairs[:, 0] == p, 1])
            else:
                add(pool, v)

    def add(p, ids):
        if p >= len(masks) or (len(ids) and int(np.max(ids)) >= counts[p]):
            raise ValueError('Invalid terrain pool reference')
        masks[p][ids] = True

    while a < b:
        op = take('<B')
        if op == 1:
            pool = take('<H')
        elif op == 2:
            take('<I')
        elif op in (3, 4, 5):
            definition = take({3:'<B', 4:'<H', 5:'<I'}[op])
        elif op == 6:
            take('<B')
        elif op == 7:
            take('<H')
        elif op in (8, 10):
            take('<HH')
        elif op in (9, 11):
            n = take('<B')
            a += n*(2 if op == 9 else 4)
        elif op == 12:
            take('<H')
            n = take('<B')
            a += n*2
        elif op == 13:
            take('<HHH')
        elif op == 14:
            take('<H')
            for _ in range(take('<B')):
                n = take('<B')
                a += n*2
        elif op == 15:
            take('<H')
            n = take('<B')
            a += n*2
        elif op in (16, 17, 18):
            if op in (17, 18):
                flags = take('<B')
            if op == 18:
                take('<ff')
            active = None
            if flags & 1 and not flags & 2:
                if definition >= len(terrains):
                    raise ValueError('Invalid terrain definition index')
                if definition not in kinds:
                    kinds[definition] = _terrain_kind(terrains[definition], folder)
                    material = folder/terrains[definition]
                    if (material.is_file() and not re.search(r'_(sea|water)(?:_overlay)?\.ter$', terrains[definition])):
                        materials[str(material)] = fingerprint(material)
                active = kinds[definition]
                physical_land += active == 'land'
                physical_water += active == 'water'
        elif op in (23, 26, 29):
            indices(take('<B'))
        elif op in (24, 27, 30):
            indices(take('<B'), cross=True)
        elif op in (25, 28, 31):
            first, last = take('<HH')
            if last < first:
                raise ValueError('Reversed terrain range')
            if active == 'land':
                add(pool, np.arange(first, last))
        elif op in (32, 33, 34):
            n = take({32:'<B', 33:'<H', 34:'<I'}[op])
            a += n
        else:
            raise ValueError(f'Unsupported DSF command {op}')
        if a > b:
            raise ValueError('Command extends outside DSF atom')
    return masks, physical_land, physical_water, materials


def cell_ids(xyz, lat, lon):
    return (np.clip(((xyz[:, 1]-lat)*8).astype(int), 0, 7)*8
            + np.clip(((xyz[:, 0]-lon)*8).astype(int), 0, 7))


def _trim(xyz, lat, lon, per_cell):
    if not len(xyz):
        return xyz
    cells = cell_ids(xyz, lat, lon)
    x = np.rint((xyz[:, 0]+180)*1e7).astype(np.uint64)
    y = np.rint((xyz[:, 1]+90)*1e7).astype(np.uint64)
    ranks = (x*73856093) ^ (y*19349663)
    keep = []
    for cell in np.unique(cells):
        ids = np.flatnonzero(cells == cell)
        if len(ids) > per_cell:
            ids = ids[np.argpartition(ranks[ids], per_cell-1)[:per_cell]]
        keep.extend(ids[np.argsort(ranks[ids])])
    return xyz[np.asarray(keep, int)]


def read_dsf(path, lat, lon, max_per_cell=32):
    path = Path(path)
    with path.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if len(data) < 28 or data[:8] != b'XPLNEDSF' or struct.unpack_from('<I', data, 8)[0] != 1:
            raise ValueError('Unsupported DSF header (7z archives are not decoded)')
        digest = hashlib.md5()
        for pos in range(0, len(data)-16, 4*1024*1024):
            digest.update(data[pos:min(pos+4*1024*1024, len(data)-16)])
        if digest.digest() != data[-16:]:
            raise ValueError('DSF checksum mismatch')
        pools, scales, terrains, cmds, props = [], [], [], [], {}
        for kind, a, b in _atoms(data, 12, len(data)-16):
            if kind in (b'DAEH', b'NFED', b'DOEG'):
                for sub, c, d in _atoms(data, a, b):
                    if sub == b'LOOP':
                        pools.append((c, d))
                    elif sub == b'LACS':
                        scales.append(np.frombuffer(data, '<f4', (d-c)//4, c).copy().reshape(-1, 2))
                    elif sub == b'TRET':
                        terrains += data[c:d].decode('utf-8').split('\0')[:-1]
                    elif sub == b'PORP':
                        parts = data[c:d].decode('utf-8').split('\0')[:-1]
                        props.update(zip(parts[::2], parts[1::2]))
            elif kind == b'SDMC':
                cmds.append((a, b))
        if props.get('sim/overlay') == '1':
            raise ValueError('Overlay DSF has no base terrain height source')
        if len(pools) != len(scales) or not cmds:
            raise ValueError('Missing or inconsistent DSF geometry')
        counts = [struct.unpack_from('<I', data, a)[0] for a, _ in pools]
        masks = [np.zeros(n, bool) for n in counts]
        land = water = 0
        materials = {}
        for a, b in cmds:
            refs, nl, nw, used_materials = _references(data, a, b, counts, terrains, path.parents[2])
            materials.update(used_materials)
            masks = [m | r for m, r in zip(masks, refs)]
            land += nl
            water += nw
        points = np.empty((0, 3), float)
        total = 0
        for (a, b), scale, mask in zip(pools, scales, masks):
            if not mask.any():
                continue
            raw, planes = _pool(data, a, b)
            if planes < 5 or len(scale) != planes:
                raise ValueError('Invalid terrain pool planes')
            v = raw[mask].astype(float)/65535*scale[:3, 0] + scale[:3, 1]
            if not np.isfinite(v).all() or (v[:, 2] <= -32768).any():
                raise ValueError('Indirect raster heights or invalid heights are unsupported')
            total += len(v)
            v = _trim(v, lat, lon, max_per_cell)
            points = _trim(np.unique(np.concatenate([points, v]), axis=0), lat, lon, max_per_cell)
        info = {'checksum': digest.hexdigest(), 'land_vertices': total,
                'land_patches': land, 'water_patches': water, 'properties': props,
                'sampled_vertices': len(points), 'terrain_fingerprints': materials}
        return info, points


def classify(z, cop, old, cells):
    """Require good absolute fit AND discriminating, geographically spread evidence."""
    z, cop, old, cells = map(np.asarray, (z, cop, old, cells))
    usable = np.isfinite(z) & np.isfinite(cop) & np.isfinite(old)
    ec, eh = np.abs(z-cop), np.abs(z-old)
    gap = np.abs(cop-old)
    different = usable & (gap >= 3)
    quarters = (cells//8//4)*2+(cells%8//4)
    result = {'status': 'UNKNOWN', 'reason': 'Insufficient discriminating evidence',
              'paired_points': int(usable.sum()), 'discriminating_points': int(different.sum())}
    for name, err in (('copernicus', ec), ('viewfinder', eh)):
        valid = np.isfinite(err)
        result[name+'_median_m'] = float(np.median(err[valid])) if valid.any() else None
        result[name+'_p90_m'] = float(np.percentile(err[valid], 90)) if valid.any() else None

    def matches(mask, err, alternative, minimum=24, geographic=True):
        if mask.sum() < minimum:
            return False
        if geographic and (len(np.unique(cells[mask])) < 3 or len(np.unique(quarters[mask])) < 2):
            return False
        return (np.median(err[mask]) <= 2 and np.percentile(err[mask], 90) <= 6
                and np.median(alternative[mask]-err[mask]) >= 2
                and np.mean(err[mask]+1 < alternative[mask]) >= .8)

    votes = {'COPERNICUS': [], 'VIEWFINDER': []}
    for q in np.unique(quarters[different]):
        mask = different & (quarters == q)
        for name, err, alt in (('COPERNICUS', ec, eh), ('VIEWFINDER', eh, ec)):
            if matches(mask, err, alt, minimum=12, geographic=False):
                votes[name].append(int(q))
    result['region_votes'] = votes
    if len(votes['COPERNICUS']) >= 2 and len(votes['VIEWFINDER']) >= 2:
        result.update(status='MIXED', reason='Separate regions fit different sources; review required')
        return result
    for name, err, alt, opposing in (('COPERNICUS', ec, eh, 'VIEWFINDER'),
                                      ('VIEWFINDER', eh, ec, 'COPERNICUS')):
        # Also check ordinary terrain, not just a few source differences.
        if (matches(different, err, alt) and not votes[opposing]
                and np.median(err[usable]) <= 2 and np.percentile(err[usable], 90) <= 8):
            result.update(status=name, reason='Good absolute fit and clear source advantage across regions')
            return result
    if not usable.any():
        result['reason'] = 'One or both reference sources unavailable at the sampled points'
    elif different.sum() < 24:
        result['reason'] = 'References too similar, or too few discriminating points'
    else:
        result['reason'] = 'Absolute fit, regional consistency or spatial support insufficient'
    return result


def load_rasterio(ortho):
    try:
        import rasterio
    except ImportError:
        # Reuse the user's existing Windows cp312 Ortho runtime libraries.
        import site
        existing = Path(ortho)/'venv'/'Lib'/'site-packages'
        if existing.is_dir():
            site.addsitedir(str(existing))
        try:
            import rasterio
        except ImportError as e:
            raise RuntimeError('Rasterio is required. Run with the supplied launcher or a working Ortho4XP Python.') from e
    return rasterio


def tile_name(lat, lon):
    return f'{lat:+03d}{lon:+04d}'


def dem_name(lat, lon):
    return f'{"N" if lat >= 0 else "S"}{abs(lat):02d}{"E" if lon >= 0 else "W"}{abs(lon):03d}'


def dem_files(ortho, lat, lon):
    group = tile_name(math.floor(lat/10)*10, math.floor(lon/10)*10)
    directory = Path(ortho)/'Elevation_data'/group
    base = dem_name(lat, lon)
    return directory/(base+'_COP30.tif'), directory/(base+'.hgt')


def cop_url(lat, lon):
    base = dem_name(lat, lon)
    name = f'Copernicus_DSM_COG_10_{base[:3]}_00_{base[3:]}_00_DEM'
    return f'https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif'


def remote_fingerprint(url):
    req = urllib.request.Request(url, method='HEAD')
    with urllib.request.urlopen(req, timeout=15) as response:
        etag = response.headers.get('ETag')
        if not etag:
            raise ValueError('Remote reference has no stable ETag')
        return {'url': url, 'etag': etag, 'length': response.headers.get('Content-Length')}


def sample_tiff(source, xs, ys, rasterio):
    """Only read small windows, sharing windows for points in the same 128px area."""
    result = np.full(len(xs), np.nan)
    # GDAL honours COG range reads; limit failures instead of waiting indefinitely.
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', GDAL_HTTP_TIMEOUT='20',
                      GDAL_HTTP_CONNECTTIMEOUT='10', GDAL_HTTP_MAX_RETRY='1',
                      CPL_VSIL_CURL_ALLOWED_EXTENSIONS='.tif'):
        with rasterio.open(str(source)) as ds:
            if ds.crs is None or ds.crs.to_epsg() != 4326:
                raise ValueError('Reference must use EPSG:4326')
            if ds.transform.b != 0 or ds.transform.d != 0:
                raise ValueError('Rotated reference raster is unsupported')
            # Rasterio transforms are Area corner transforms, including for PixelIsPoint.
            # Half a pixel brings them back to the actual sample centres.
            xx = (xs-ds.transform.c)/ds.transform.a-.5
            yy = (ys-ds.transform.f)/ds.transform.e-.5
            valid = (xx >= 0) & (xx <= ds.width-1) & (yy >= 0) & (yy <= ds.height-1)
            ids = np.flatnonzero(valid)
            keys = (np.floor(yy[ids]/128).astype(int)*(ds.width//128+1)
                    + np.floor(xx[ids]/128).astype(int))
            for key in np.unique(keys):
                sel = ids[keys == key]
                left = max(0, int(np.floor(xx[sel].min())))
                top = max(0, int(np.floor(yy[sel].min())))
                right = min(ds.width, int(np.floor(xx[sel].max()))+2)
                bottom = min(ds.height, int(np.floor(yy[sel].max()))+2)
                if right-left < 2:
                    left = max(0, right-2)
                if bottom-top < 2:
                    top = max(0, bottom-2)
                window = rasterio.windows.Window(left, top, right-left, bottom-top)
                block = ds.read(1, window=window).astype(float)
                result[sel] = sample_grid(block, xx[sel]-left, yy[sel]-top, ds.nodata)
    return result


def sample_hgt(path, lat, lon, xs, ys):
    size = Path(path).stat().st_size
    n = math.isqrt(size//2)
    if size != 2*n*n or n not in (1201, 3601):
        raise ValueError('Unsupported HGT dimensions')
    a = np.memmap(path, dtype='>i2', mode='r', shape=(n, n))
    try:
        return sample_grid(a, (xs-lon)*(n-1), (lat+1-ys)*(n-1), -32768)
    finally:
        a._mmap.close()


def regridded_cop(source, xs, ys, lat, lon, rasterio):
    # Reproduce the current loader's 1/3600-degree grid sampling without constructing it.
    x = (xs-lon)*3600
    y = (lat+1-ys)*3600
    ix, iy = np.floor(x), np.floor(y)
    fx, fy = x-ix, y-iy
    xx = np.concatenate([lon+ix/3600, lon+(ix+1)/3600]*2)
    yy = np.concatenate([lat+1-iy/3600]*2 + [lat+1-(iy+1)/3600]*2)
    v = sample_tiff(source, xx, yy, rasterio).reshape(4, -1)
    return np.sum(v*np.array([(1-fx)*(1-fy), fx*(1-fy), (1-fx)*fy, fx*fy]), axis=0)


def airport_centres(path):
    if not path.is_file():
        return np.empty((0, 3))
    rows = []
    with path.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            if row.get('type') == 'closed':
                continue
            try:
                rows.append([float(row['longitude_deg']), float(row['latitude_deg']),
                             .7 if row.get('type') == 'heliport' else 3.0])
            except (KeyError, ValueError):
                continue
    return np.asarray(rows) if rows else np.empty((0, 3))


def eligible_points(points, airports, lat, lon):
    inside = ((points[:, 0] > lon+.002) & (points[:, 0] < lon+.998)
              & (points[:, 1] > lat+.002) & (points[:, 1] < lat+.998))
    v = points[inside]
    keep = np.ones(len(v), bool)
    if len(airports):
        near = airports[(np.abs(airports[:, 0]-(lon+.5)) < 1)
                        & (np.abs(airports[:, 1]-(lat+.5)) < 1)]
        for x, y, radius in near:
            km2 = ((v[:, 0]-x)*111.32*math.cos(math.radians(y)))**2 + ((v[:, 1]-y)*111.32)**2
            keep &= km2 > radius**2
    return v[keep], {'border_excluded': int((~inside).sum()), 'airport_excluded': int((~keep).sum())}


def make_key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def _overlay(path):
    # A metadata check for priority resolution; source classification still verifies the full DSF.
    try:
        with path.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            if data[:8] != b'XPLNEDSF':
                return None
            for kind, a, b in _atoms(data, 12, len(data)-16):
                if kind == b'DAEH':
                    for sub, c, d in _atoms(data, a, b):
                        if sub == b'PORP':
                            parts = data[c:d].decode().split('\0')[:-1]
                            return dict(zip(parts[::2], parts[1::2])).get('sim/overlay') == '1'
            return False
    except (OSError, ValueError, struct.error):
        return None


def priority_entries(root):
    path = root/'scenery_packs.ini'
    entries = []
    if path.is_file():
        for line in path.read_text(encoding='utf-8-sig', errors='replace').splitlines():
            match = re.match(r'^(SCENERY_PACK(?:_DISABLED)?)\s+(.+?)\s*$', line)
            if not match:
                continue
            folder = Path(match[2].rstrip('/\\'))
            if not folder.is_absolute():
                folder = root.parent/folder
            entries.append((folder.resolve(), match[1] == 'SCENERY_PACK'))
    return path, entries


def _dsf_path(folder, name):
    lat, lon = parse_tile(name)
    group = tile_name(math.floor(lat/10)*10, math.floor(lon/10)*10)
    return folder/'Earth nav data'/group/(name+'.dsf')


def parse_tile(name):
    match = TILE_RE.fullmatch(name)
    if not match:
        raise ValueError('Tile must look like +28-017 or +41+012: '+name)
    lat, lon = map(int, match.groups())
    if not (-90 <= lat < 90 and -180 <= lon < 180):
        raise ValueError('Tile coordinates outside the Earth')
    return lat, lon


def discover_tiles(root, requested):
    """Directory/INI inventory only. --tile never decodes unrelated tile files."""
    root = Path(root)
    ini, entries = priority_entries(root)
    listed = {str(p).casefold(): enabled for p, enabled in entries}
    folders = {}
    if requested:
        for name in requested:
            parse_tile(name)
            folders[name] = [(root/('zOrtho4XP_'+name)).resolve()]
    else:
        for folder in sorted(root.glob('zOrtho4XP_*')):
            match = TILE_RE.search(folder.name)
            if folder.is_dir() and match:
                folders.setdefault(match[0], []).append(folder.resolve())
    if not folders:
        raise ValueError('No zOrtho4XP tile folders found in '+str(root))
    first_base = {}
    wanted = set(folders)
    # Avoid probing every scenery pack for every tile on a full-library run.
    # Inventory each pack once and stop reading metadata once priority is established.
    for folder, enabled in entries:
        if not enabled:
            continue
        if requested:
            paths = (_dsf_path(folder, name) for name in wanted if name not in first_base)
        else:
            nav = folder/'Earth nav data'
            if not nav.is_dir():
                continue
            paths = nav.glob('*/*.dsf')
        for path in paths:
            name = path.stem
            if name not in wanted or name in first_base or not path.is_file():
                continue
            overlay = _overlay(path)
            if overlay is not True:
                first_base[name] = (folder, overlay is None)
    rows = []
    for name, candidates in sorted(folders.items()):
        active, unresolved_priority = first_base.get(name, (None, False))
        for folder in candidates:
            path = _dsf_path(folder, name)
            row = {'tile': name, 'pack': folder.name, 'dsf': str(path), 'priority_verified': False}
            if not path.is_file():
                row.update(status='NOT_BUILT', reason='Installed DSF absent')
            elif listed.get(str(folder).casefold()) is False:
                row.update(status='DISABLED', reason='Disabled in scenery_packs.ini')
            elif unresolved_priority:
                row.update(status='UNKNOWN', reason='Cannot verify higher priority DSF format: '+str(active))
            elif active is not None and active != folder:
                row.update(status='SHADOWED', reason='Higher priority base mesh: '+str(active))
            elif ini.is_file() and active is None:
                row.update(status='UNLISTED', reason='Not active in scenery_packs.ini')
            else:
                row['priority_verified'] = active == folder
                if not ini.is_file():
                    row['priority_note'] = 'scenery_packs.ini absent; conversion action requires priority review'
            rows.append(row)
    return rows, ini


def _sample_cache(row, args, airports, airport_sig):
    lat, lon = parse_tile(row['tile'])
    sig = {'version': VERSION, 'dsf': fingerprint(row['dsf']), 'airports': airport_sig,
           'per_cell': args.per_cell, 'border_margin': .002, 'airport_radius_km': [3, .7]}
    key = make_key(sig)
    path = args.output/'cache'/('samples-'+key+'.npz')
    if path.is_file() and not args.refresh:
        with np.load(path, allow_pickle=False) as saved:
            info = json.loads(str(saved['info']))
            materials = info.get('terrain_fingerprints', {})
            if all(fingerprint(p) == old for p, old in materials.items()):
                sig['materials'] = materials
                return saved['xyz'], info, sig
    info, xyz = read_dsf(row['dsf'], lat, lon, args.per_cell)
    xyz, excluded = eligible_points(xyz, airports, lat, lon)
    info.update(excluded)
    info['eligible_vertices'] = len(xyz)
    temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as f:
        np.savez_compressed(f, xyz=xyz, info=np.array(json.dumps(info)))
    temporary.replace(path)
    sig['materials'] = info.get('terrain_fingerprints', {})
    return xyz, info, sig


def _reference_info(args, lat, lon):
    cop, hgt = dem_files(args.ortho, lat, lon)
    errors = []
    sources = {'copernicus': str(cop) if cop.is_file() else None,
               'viewfinder': str(hgt) if hgt.is_file() else None}
    signatures = {'copernicus': fingerprint(cop), 'viewfinder': fingerprint(hgt)}
    if not cop.is_file() and not args.offline:
        url = cop_url(lat, lon)
        try:
            signatures['copernicus'] = remote_fingerprint(url)
            sources['copernicus'] = url
        except Exception as e:
            errors.append('Copernicus remote unavailable: '+str(e))
    if sources['copernicus'] is None:
        errors.append('Copernicus reference unavailable')
    if sources['viewfinder'] is None:
        errors.append('Cached Viewfinder HGT unavailable; no historical reference download is attempted')
    return sources, signatures, errors


def audit_one(row, args, airports, airport_sig, rasterio):
    start = time.monotonic()
    lat, lon = parse_tile(row['tile'])
    xyz, info, sample_sig = _sample_cache(row, args, airports, airport_sig)
    public_info = {k:v for k,v in info.items() if k != 'terrain_fingerprints'}
    public_info.update(terrain_materials_checked=len(info.get('terrain_fingerprints', {})),
                       terrain_manifest_sha256=make_key(info.get('terrain_fingerprints', {})))
    row = dict(row, dsf_info=public_info)
    if info['land_patches'] == 0 and info['water_patches'] > 0:
        return dict(row, status='NO_LAND', reason='Physical DSF terrain patches contain water only', elapsed_s=round(time.monotonic()-start, 3))
    if len(xyz) < 24:
        return dict(row, status='UNKNOWN', reason='Insufficient eligible land samples', elapsed_s=round(time.monotonic()-start, 3))
    sources, refs, errors = _reference_info(args, lat, lon)
    key = make_key({'samples': sample_sig, 'references': refs, 'offline': args.offline, 'version': VERSION})
    cache = args.output/'cache'/('result-'+make_key({'dsf': row['dsf']})+'.json')
    if cache.is_file() and not args.refresh:
        saved = json.loads(cache.read_text(encoding='utf-8'))
        if saved.get('key') == key and saved['result'].get('status') in ('COPERNICUS', 'VIEWFINDER', 'MIXED'):
            result = saved['result']
            # Priority is checked on every run, even when source evidence is cached.
            result.update({k:v for k,v in row.items() if k != 'dsf_info'})
            result.update(cached=True, elapsed_s=round(time.monotonic()-start, 3))
            return result

    def compare(v, variant):
        x, y, z = v.T
        cop = old = np.full(len(v), np.nan)
        if sources['copernicus']:
            try:
                cop = (sample_tiff(sources['copernicus'], x, y, rasterio) if variant == 'native'
                       else regridded_cop(sources['copernicus'], x, y, lat, lon, rasterio))
            except Exception as e:
                errors.append('Copernicus sampling failed: '+str(e))
        if sources['viewfinder']:
            try:
                old = sample_hgt(sources['viewfinder'], lat, lon, x, y)
            except Exception as e:
                errors.append('Viewfinder sampling failed: '+str(e))
        decision = classify(z, cop, old, cell_ids(v, lat, lon))
        valid = np.isfinite(cop) & np.isfinite(old)
        ranked = np.flatnonzero(valid)
        if len(ranked):
            # Include ordinary good-fit, discriminating evidence for auditability.
            target = cop if decision['status'] == 'COPERNICUS' else old
            good = ranked[np.abs(z[ranked]-target[ranked]) <= 2]
            ranked = good[np.argsort(np.abs(cop[good]-old[good]))[-8:]] if len(good) else ranked[:8]
        decision['evidence'] = [{'lon': float(x[i]), 'lat': float(y[i]), 'dsf_m': float(z[i]),
                                  'copernicus_m': float(cop[i]), 'viewfinder_m': float(old[i])} for i in ranked]
        decision.update(reference_variant=variant, points_compared=len(v))
        return decision

    early = _trim(xyz, lat, lon, 4)
    decision = compare(early, 'native')
    stages = ['small sample: '+decision['status']]
    if decision['status'] == 'UNKNOWN' and len(xyz) > len(early):
        decision = compare(xyz, 'native')
        stages.append('expanded sample: '+decision['status'])
    if decision['status'] in ('UNKNOWN', 'VIEWFINDER') and sources['copernicus'] and sources['viewfinder']:
        alternative = compare(xyz, 'current_1_arcsecond_grid')
        stages.append('current loader grid: '+alternative['status'])
        if decision['status'] == 'VIEWFINDER' and alternative['status'] != 'VIEWFINDER':
            decision.update(status='UNKNOWN', reason='Viewfinder attribution not consistent across Copernicus loading variants')
            decision['alternative_variant'] = {k:v for k,v in alternative.items() if k != 'evidence'}
        elif decision['status'] == 'UNKNOWN' and alternative['status'] == 'VIEWFINDER':
            decision.update(reason='Only one Copernicus sampling variant supports Viewfinder; review required')
            decision['alternative_variant'] = {k:v for k,v in alternative.items() if k != 'evidence'}
        elif alternative['status'] != 'UNKNOWN':
            decision = alternative
        else:
            decision['alternative_variant'] = {k:v for k,v in alternative.items() if k != 'evidence'}
    result = dict(row, **decision, references=refs, reference_errors=sorted(set(errors)),
                  stages=stages, cached=False, elapsed_s=round(time.monotonic()-start, 3))
    write_json(cache, {'key': key, 'result': result})
    return result


def save_reports(args, results, complete):
    counts = {}
    for row in results:
        counts[row['status']] = counts.get(row['status'], 0)+1
    document = {'classifier_version': VERSION, 'updated': time.strftime('%Y-%m-%d %H:%M:%S %z'),
                'complete': complete, 'scenery_root': str(args.scenery), 'ortho_root': str(args.ortho),
                'offline': args.offline, 'tile_filter': args.tiles or [], 'counts': counts, 'tiles': results}
    write_json(args.output/'results.json', document)
    fields = ['tile', 'pack', 'status', 'action', 'reason', 'priority_verified', 'points_compared',
              'paired_points', 'discriminating_points', 'copernicus_median_m', 'copernicus_p90_m',
              'viewfinder_median_m', 'viewfinder_p90_m', 'reference_variant', 'cached', 'elapsed_s', 'dsf']
    csv_path = args.output/'results.csv'
    temporary = csv_path.with_suffix('.csv.tmp')
    with temporary.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fields, extrasaction='ignore')
        writer.writeheader()
        for row in results:
            action = ('CONVERT' if row['status'] == 'VIEWFINDER' and row['priority_verified'] else
                      'KEEP' if row['status'] in ('COPERNICUS', 'NO_LAND') else
                      'SKIP' if row['status'] in ('DISABLED', 'SHADOWED', 'UNLISTED', 'NOT_BUILT') else 'REVIEW')
            writer.writerow(dict(row, action=action))
    temporary.replace(csv_path)
    convert = sorted({r['tile'] for r in results if r['status'] == 'VIEWFINDER' and r['priority_verified']})
    review = sorted({r['tile'] for r in results if r['status'] in ('UNKNOWN', 'MIXED')
                     or (r['status'] == 'VIEWFINDER' and not r['priority_verified'])})
    (args.output/'convert-to-copernicus.txt').write_text(''.join(t+'\n' for t in convert), encoding='utf-8')
    (args.output/'review-tiles.txt').write_text(''.join(t+'\n' for t in review), encoding='utf-8')
    lines = ['# Installed DSF elevation audit', '', 'Run complete: '+str(complete), '',
             '## Counts', ''] + [f'- {status}: {n}' for status, n in sorted(counts.items())]
    lines += ['', '## Conversion candidates', '',
              'These active tiles fit Viewfinder and differ from Copernicus: '+str(len(convert)), '',
              '```text', *convert, '```', '', '## Review', '',
              'Ambiguous/missing references or priority review: '+str(len(review)), '',
              'See results.csv for each tile and results.json for sample evidence.', '',
              'Configs, dates and raster dimensions are not treated as height-source proof.',
              'No scenery or Ortho4XP configuration was modified.']
    (args.output/'summary.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def main(argv=None):
    default_ortho, default_scenery, default_output = runtime_defaults()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ortho', type=Path, default=default_ortho)
    parser.add_argument('--scenery', type=Path, default=default_scenery)
    parser.add_argument('--output', type=Path, default=default_output)
    parser.add_argument('--airports', type=Path, default=HERE/'airports.csv')
    parser.add_argument('--tile', dest='tiles', action='append', metavar='+28-017', help='Restrict to one tile; repeat for a pilot')
    parser.add_argument('--offline', action='store_true', help='Use existing DEM files only')
    parser.add_argument('--refresh', action='store_true', help='Ignore sample and result caches')
    parser.add_argument('--per-cell', type=int, default=32, help='Maximum samples in each of 64 geographic cells (default 32)')
    args = parser.parse_args(argv)
    if not 4 <= args.per_cell <= 128:
        parser.error('--per-cell must be between 4 and 128')
    try:
        args.output = args.output.resolve()
        # Reports may live in the dedicated app folder; scenery and data stay untouched.
        ortho_root, scenery_root = args.ortho.resolve(), args.scenery.resolve()
        report_root = ortho_root/'Height-Reports'
        if args.output == scenery_root or scenery_root in args.output.parents:
            raise ValueError('Output must be outside Custom Scenery')
        if args.output == ortho_root or (ortho_root in args.output.parents
                and args.output != report_root and report_root not in args.output.parents):
            raise ValueError('Reports inside Ortho4XP must use its Height-Reports folder')
        rasterio = load_rasterio(args.ortho)
        rows, ini = discover_tiles(args.scenery, args.tiles)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output/'cache').mkdir(exist_ok=True)
        airports = airport_centres(args.airports)
        airport_sig = fingerprint(args.airports)
        print(f'Classifier {VERSION}: {len(rows)} tile folders. Scenery is read-only.', flush=True)
        print('References: local first'+(' (offline)' if args.offline else '; missing Copernicus uses remote COG windows'), flush=True)
        if not len(airports):
            print('WARNING: airport exclusion CSV unavailable. Fit checks still abstain on poor matches.', flush=True)
        results = []
        save_reports(args, results, False)
        last_report = time.monotonic()
        try:
            for number, row in enumerate(rows, 1):
                print(f'[{number}/{len(rows)}] {row["tile"]}: ', end='', flush=True)
                try:
                    result = row if 'status' in row else audit_one(row, args, airports, airport_sig, rasterio)
                except Exception as e:
                    result = dict(row, status='UNKNOWN', reason=f'{type(e).__name__}: {e}')
                results.append(result)
                print(result['status']+(' (cached)' if result.get('cached') else ''), flush=True)
                if number % 25 == 0 or time.monotonic()-last_report >= 30:
                    save_reports(args, results, False)
                    last_report = time.monotonic()
        except KeyboardInterrupt:
            save_reports(args, results, False)
            print('\nStopped. Completed results saved; run again to resume.', flush=True)
            return 130
        save_reports(args, results, True)
        print('Reports: '+str(args.output), flush=True)
        print('Conversion candidates are in convert-to-copernicus.txt; uncertain tiles are in review-tiles.txt.', flush=True)
        return 0
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
