import os
import logging

import ESMF

import numpy as np
import xarray as xr
import xesmf as xe

from . import config
from . import util


class grid_ref(object):
    def __init__(self,name,clobber=False):
        self.name = name
        self._gen_grid_file(clobber=clobber)
        self._esmf_grid_from_scrip()

    def _gen_grid_file(self,clobber=False):
        '''Generate a SCRIP grid file for "grid"'''

        self.scrip_grid_file = f'{config.dir_grid_files}/{self.name}.nc'

        if os.path.exists(self.scrip_grid_file) and not clobber:
            return

        assert self.name in config.known_grids, f'Unknown grid: {self.name}'

        #-- get grid generation method and kwargs
        info = config.known_grids[self.name]
        grid_gen_method_name = info['gen_grid_file']['method']
        gen_grid_method = getattr(util,grid_gen_method_name)
        kwargs = info['gen_grid_file']['kwargs']

        #-- generate the grid
        logging.info(f'generating grid file {self.scrip_grid_file}')
        gen_grid_method(self.name, self.scrip_grid_file, **kwargs,
                        clobber=clobber)

    def _esmf_grid_from_scrip(self):
        '''Generate an ESMF grid object from a SCRIP grid file.'''

        self.ds = xr.open_dataset(self.scrip_grid_file)

        self.grid = ESMF.Grid(filename = self.scrip_grid_file,
                              filetype = ESMF.api.constants.FileFormat.SCRIP,
                              add_corner_stagger=True)

        self.shape = self.ds.grid_dims.values
        mask = self.grid.add_item(ESMF.GridItem.MASK)
        mask[:] = self.ds.grid_imask.values.reshape(self.shape[::-1]).T


class regridder(object):
    def __init__(self, name_grid_src, name_grid_dst, method,
                 clobber = False):

        self.name_grid_src = name_grid_src
        self.name_grid_dst = name_grid_dst
        self.grid_ref_src = grid_ref(name_grid_src)
        self.grid_ref_dst = grid_ref(name_grid_dst)
        self.method = method
        self.clobber = clobber

        self.N_src = self.grid_ref_src.shape[0] * self.grid_ref_src.shape[1]
        self.N_dst = self.grid_ref_dst.shape[0] * self.grid_ref_dst.shape[1]

        self._gen_weights()
        self.A = xe.smm.read_weights(self.weight_file, self.N_src, self.N_dst)


    def _gen_weights(self):

        self.weight_file = ('{0}/{1}_to_{2}_{3}.nc'.format(
            config.dir_weight_files,
            self.name_grid_src,
            self.name_grid_dst,
            self.method)
        )

        if os.path.exists(self.weight_file):
            if self.clobber:
                logging.info(f'removing {self.weight_file}')
                os.remove(self.weight_file)
            else:
                return

        logging.info(f'generating {self.weight_file}')
        regrid = xe.backend.esmf_regrid_build(self.grid_ref_src.grid,
                                              self.grid_ref_dst.grid,
                                              filename = self.weight_file,
                                              method = self.method)

        xe.backend.esmf_regrid_finalize(regrid)


    def __call__(self, da_in, renormalize = True, apply_mask = None,
                 interp_coord = {}, post_method = None,
                 post_method_kwargs = {}):

        return self.regrid_dataarray(da_in,
                                     renormalize = renormalize,
                                     apply_mask = apply_mask,
                                     interp_coord = interp_coord,
                                     post_method = post_method,
                                     post_method_kwargs = post_method_kwargs)


    def regrid_dataarray(self, da_in, renormalize = True,
                         apply_mask = None,
                         interp_coord = {},
                         post_method = None,
                         post_method_kwargs = {}):
        '''Regrid an `xarray.DataArray`.'''

        #-- pull data, dims and coords from incoming DataArray
        data_src = da_in.values
        non_lateral_dims = da_in.dims[:-2]
        copy_coords = {d: da_in.coords[d] for d in non_lateral_dims
                       if d in da_in.coords}

        #-- if renormalize == True, remap a field of ones
        if renormalize:
            ones_src = np.where(np.isnan(data_src), 0., 1.)
            data_src = np.where(np.isnan(data_src), 0., data_src)

        #-- remap the field
        data_dst = xe.smm.apply_weights(self.A, data_src,
                                        self.grid_ref_dst.shape[1],
                                        self.grid_ref_dst.shape[0])

        #-- renormalize to include only non-missing data_src
        if renormalize:
            ones_dst = xe.smm.apply_weights(self.A, ones_src,
                                            self.grid_ref_dst.shape[1],
                                            self.grid_ref_dst.shape[0])
            ones_dst = np.where(ones_dst > 0., ones_dst, np.nan)
            data_dst = data_dst / ones_dst
            data_dst = np.where(ones_dst > 0., data_dst, np.nan)

        #-- reform into xarray.DataArray
        da_out = xr.DataArray(data_dst,
                              name = da_in.name,
                              dims = da_in.dims,
                              attrs = da_in.attrs,
                              coords = copy_coords)
        da_out.attrs['regrid_method'] = self.method

        #-- interpolate coordinates (i.e., vertical)
        #   setup to copy lowest/highest values where extrapolation is needed
        for dim,new_coord in interp_coord.items():
            if dim in da_out.dims:
                extrap_values = (da_out.isel(**{dim: 0}),
                                 da_out.isel(**{dim: -1})
                                 )

                da_out = da_out.interp(coords = {dim: new_coord},
                                       method = 'linear',
                                       assume_sorted = True,
                                       kwargs = {'fill_value': extrap_values})

        #-- apply a missing-values mask
        if apply_mask is not None:
            if apply_mask.dims != da_in.dims:
                logging.warning(f'masking {apply_mask.dims}; '
                                f'data have dims: {da_in.dims}')
            da_out = da_out.where(apply_mask)

        #-- apply a post_method
        if post_method is not None:
            da_out = post_method(da_out,**post_method_kwargs)

        return da_out