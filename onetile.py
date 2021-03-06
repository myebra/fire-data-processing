"""
Script to create one tile-year of LFMC data, from the original MODIS products.

Likely to remain a (useful) work in progress for some time.

Requires PyNIO to read MODIS .hdf files, and therefore Python 2 for now.

It's also a best-possible port of equations developed for MODIS C5 to C6 data;
we plan to fully upgrade in future but that will require revalidation.

"""

import os
import re
import json
import glob
import argparse
import datetime

import numpy as np
import pandas as pd
import xarray as xr


__version__ = '0.2.0'

modis_band_map = {
    'Nadir_Reflectance_Band1': 'red_630_690',
    'Nadir_Reflectance_Band2': 'nir1_780_900',
    'Nadir_Reflectance_Band3': 'blue_450_520',
    'Nadir_Reflectance_Band4': 'green_530_610',
    'Nadir_Reflectance_Band5': 'nir2_1230_1250',
    'Nadir_Reflectance_Band6': 'swir1_1550_1750',
    'Nadir_Reflectance_Band7': 'swir2_2090_2350',
}

bands_to_use = ['red_630_690', 'nir1_780_900', 'green_530_610',
                'swir1_1550_1750', 'swir2_2090_2350', 'ndii']


functor_cache = {}


def get_functor(veg_type):
    """Returns a function to get the mean and stdev of LFMC for the top n values.

    Note that the function object is cached to avoid loading the vmat and smat
    tables more than once per vegetation type.
    """
    if veg_type in functor_cache:
        return functor_cache[veg_type]
    # Get the lookup table
    merged_lookup = pd.read_csv('lookup_tables/merged_lookup.csv', index_col='ID')
    merged_lookup['ndii'] = difference_index(
        merged_lookup.nir1_780_900, merged_lookup.swir1_1550_1750)
    table = merged_lookup.where(merged_lookup.VEGTYPE == veg_type)
    vmat = table[bands_to_use].values
    vsmat = np.sqrt((vmat ** 2).sum(axis=1))

    def get_top_n(mb, vmat=vmat, vsmat=vsmat, fmc=table.FMC.values):
        spectral_angle = np.arccos(
            np.einsum('ij,j->i', vmat, mb) /
            (np.sqrt(np.einsum('i,i->', mb, mb)) * vsmat)
        )
        top_values = fmc[np.argpartition(spectral_angle, 40)[:40]]
        return top_values.mean(axis=-1), top_values.std(axis=-1)

    functor_cache[veg_type] = get_top_n
    return get_top_n


def difference_index(a, b):
    """A common pattern, eg NDVI, NDII, etc."""
    return (a - b) / (a + b)


def get_fmc(dataset, masks):
    """Get the mean and stdev of LFMC for the given Xarray dataset (one time-step)."""
    bands = xr.concat([dataset[b] for b in bands_to_use], dim='band')
    ok = np.logical_and(dataset.ndvi_ok_mask, bands.notnull().all(dim='band'))

    out = np.full((2,) + ok.shape, np.nan, dtype='float32')

    for kind, mask in masks.items():
        cond = np.logical_and(ok, mask[:bands.y.size, :bands.x.size]).values
        vals = bands.values[:, cond]
        if vals.size:
            # Only calculate for and assign to the unmasked values
            out[:,cond] = np.apply_along_axis(get_functor(kind), 0, vals)

    data_vars = dict(lvmc_mean=(('y', 'x'), out[0]),
                     lvmc_stdv=(('y', 'x'), out[1]))
    return xr.Dataset(data_vars=data_vars, coords=dataset.coords)


def get_reflectance(year, tile):
    files = sorted(glob.glob(
        '/g/data/u39/public/data/modis/lpdaac-tiles-c6/MCD43A4.006/' +
        '{year}.??.??/MCD43A4.A{year}???.{tile}.006.*.hdf'
        .format(year=year, tile=tile)
    ))
    pattern = re.compile(r'MCD43A4.A\d{4}(?P<day>\d{3}).h\d\dv\d\d.006.\d+.hdf')
    dates = []
    for f in files:
        day, = pattern.match(os.path.basename(f)).groups()
        dates.append(datetime.date(int(year), 1, 1) +
                     datetime.timedelta(days=int(day) - 1))

    dates = pd.to_datetime(dates)
    dates.name = 'time'

    ds = xr.concat([xr.open_dataset(fname, chunks=2400) for fname in files], dates)
    out = xr.Dataset()
    for i in map(str, range(1, 8)):
        key = 'Nadir_Reflectance_Band' + i
        data_ok = ds['BRDF_Albedo_Band_Mandatory_Quality_Band' + i] == 0
        out[modis_band_map[key]] = ds[key].astype('f4').where(data_ok)
    out['ndvi_ok_mask'] = 0.15 < difference_index(out.nir1_780_900, out.red_630_690)
    out['ndii'] = difference_index(out.nir1_780_900, out.swir1_1550_1750)

    out.rename({'YDim:MOD_Grid_BRDF': 'y',
                'XDim:MOD_Grid_BRDF': 'x'}, inplace=True)
    out.time.encoding.update(dict(
        units='days since 1900-01-01', calendar='gregorian', dtype='i4'))
    return out


def get_masks(year, tile):
    file, = glob.glob(
        '/g/data/u39/public/data/modis/lpdaac-tiles-c5/MCD12Q1.051/' +
        '{year}.??.??/MCD12Q1.A{year}???.{tile}.051.*.hdf'
        .format(year=min(year, '2013'), tile=tile)
    )
    arr = xr.open_dataset(file).Land_Cover_Type_1
    classes = {
        'grass': (u'grasslands', u'croplands'),
        'shrub': (u'closed shrubland', u'open shrublands'),
        'forest': (
            u'evergreen needleleaf forest', u'evergreen broadleaf forest',
            u'deciduous needleleaf forest', u'deciduous broadleaf forest',
            u'mixed forests', u'woody savannas', u'savannas'),
    }
    return {
        k: np.sum((arr == arr.attrs[name]) for name in v).astype(bool)
        for k, v in classes.items()
    }


def add_sinusoidal_var(ds):
    with open('sinusoidal.json') as f:
        attrs = json.load(f)
    attrs['GeoTransform'] = ' '.join(str(float(x)) for x in [
        # Affine matrix - start/step/rotation, start/rotation/step - in 1D
        ds.x[0], (ds.x[-1] - ds.x[0]) / ds.x.size, 0,
        ds.y[0], 0, (ds.y[-1] - ds.y[0]) / ds.y.size
    ])
    ds['sinusoidal'] = xr.DataArray(np.zeros((), 'S1'), attrs=attrs)


def main(year, tile):
    # Get the main dataset - demo is one tile for a year
    ds = get_reflectance(year, tile)
    # Get the landcover masks
    masks = get_masks(year, tile)

    # Do the expensive bit
    out = xr.concat(
        [get_fmc(ds.sel(time=ts), masks=masks) for ts in ds.time],
        dim='time',
    )

    # Ugly hack because PyNIO dropped coords; add them in from another MODIS dataset
    with xr.open_dataset(glob.glob(
            '/g/data/ub8/au/FMC/sinusoidal/MCD43A4.2001.{}.005.*_LFMC.nc'
            .format(tile))[0]) as coord_ds:
        out['x'] = coord_ds.x
        out['y'] = coord_ds.y

    with open('nc_metadata.json') as f:
        json_attrs = json.load(f)

    # Add metadata to the resulting file
    out.attrs.update(json_attrs)
    add_sinusoidal_var(out)
    var_attrs = dict(
        units='%', grid_mapping='sinusoidal',
        comment='Ratio of water to dry plant matter.  '
        'Mean of top 40 matches from observed to simulated reflectance.'
    )
    out.lvmc_mean.attrs.update(dict(long_name='LVMC Arithmetic Mean', **var_attrs))
    out.lvmc_stdv.attrs.update(dict(long_name='LVMC Standard Deviation', **var_attrs))
    out.time.encoding.update(dict(units='days since 1900-01-01', calendar='gregorian', dtype='i4'))
    for d in (out.lvmc_mean, out.lvmc_stdv):
        d.encoding.update(dict(
            shuffle=True, zlib=True, chunks=dict(x=400, y=400, time=6),
            # After compression, set fill to work around GSKY transparency bug
            _FillValue=-999,
        ))

    # Save the file!
    out_file = '/g/data/ub8/au/FMC/c6/LVMC_{}_{}.nc'.format(year, tile)
    out.to_netcdf(out_file)
    # Make it visible via Thredds
    os.system('chmod a+rx ' + out_file)


def get_validated_args():

    def check_year(val):
        """Validate arg and transform glob pattern to file list."""
        assert re.match(r'\A20\d\d\Z', val), repr(val)
        return val

    def check_tile(val):
        """Validate that arg is an existing directory."""
        assert re.match(r'\Ah\d\dv\d\d\Z', val), repr(val)
        return val

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-V', '--version', action='version', version=__version__)
    parser.add_argument(
        '--year', type=check_year, default=os.environ.get('FMC_YEAR'),
        help='four-digit year to process')
    parser.add_argument(
        '--tile', type=check_tile, default=os.environ.get('FMC_TILE'),
        help='tile to process, "hXXvYY"')
    return parser.parse_args()


if __name__ == '__main__':
    args = get_validated_args()
    print(args)
    main(args.year, args.tile)
