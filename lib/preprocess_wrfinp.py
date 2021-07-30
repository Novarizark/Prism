#/usr/bin/env python
"""Preprocessing the WRF input file"""

import datetime
import xarray as xr
import pandas as pd
import netCDF4 as nc4
import wrf  
import os, subprocess
from multiprocessing import Pool

import lib
from utils import utils

print_prefix='lib.preprocess_wrfinp>>'

class WrfMesh:

    '''
    Construct grid info and UVW mesh template
    
    Attributes
    -----------
    
    Methods
    -----------
    
    '''
    
    def __init__(self, cfg, call_from='training'):
        """ construct input wrf file names """
        
        utils.write_log(print_prefix+'Init wrf_mesh obj...')
        utils.write_log(print_prefix+'Read input files...')
        
        # collect global attr
        self.nc_fn_base='./input/'+call_from+'/'
        self.ntasks=int(cfg['SHARE']['ntasks'])
        self.varlist=lib.cfgparser.cfg_get_varlist(cfg,'SHARE','var')

        if call_from=='training':
            timestamp_start=datetime.datetime.strptime(cfg['TRAINING']['training_start']+'12','%Y%m%d%H')
            timestamp_end=datetime.datetime.strptime(cfg['TRAINING']['training_end']+'12','%Y%m%d%H')
            self.dateseries=pd.date_range(start=timestamp_start, end=timestamp_end, freq='D')
        elif call_from=='inference':
            fn_stream=subprocess.check_output('ls '+self.nc_fn_base+'wrfout*', shell=True).decode('utf-8')
            fn_list=fn_stream.split()
            start_basename=fn_list[0].split('/')[3]
            if cfg['INFERENCE'].getboolean('debug_mode'):
                utils.write_log(print_prefix+'Debug mode turns on!')
                end_basename=fn_list[self.ntasks-1].split('/')[3]
            else:
                end_basename=fn_list[-1].split('/')[3]
            timestamp_start=datetime.datetime.strptime(start_basename[11:],'%Y-%m-%d_%H:%M:%S')
            timestamp_end=datetime.datetime.strptime(end_basename[11:],'%Y-%m-%d_%H:%M:%S')
            self.dateseries=pd.date_range(start=timestamp_start, end=timestamp_end, freq='H')
    
        self.load_data()
    
    def load_data(self):
        
        nc_fn_base=self.nc_fn_base
        datestamp=self.dateseries[0] 
        varlist=self.varlist
        ntasks=self.ntasks
        da_dic={}
       
        # -------read the rest files 
        # let's do the multiprocessing magic!
        utils.write_log(print_prefix+'Multiprocessing initiated. Master process %s.' % os.getpid())
        file_dates=self.dateseries
        len_file=len(file_dates)
        len_per_task=len_file//ntasks
        results=[]
        
        # start process pool
        process_pool = Pool(processes=ntasks)
        
        # open tasks ID 0 to ntasks-2
        for itsk in range(ntasks-1):  
            
            ifile_dates=file_dates[itsk*len_per_task:(itsk+1)*len_per_task]
            
            result=process_pool.apply_async(
                run_mtsk, 
                args=(itsk, ifile_dates, da_dic, self, ))
            results.append(result)

        # open ID ntasks-1 in case of residual
        ifile_dates=file_dates[(ntasks-1)*len_per_task:]

        result=process_pool.apply_async(
            run_mtsk, 
            args=(ntasks-1, ifile_dates, da_dic, self, ))

        results.append(result)
        utils.write_log(print_prefix+'Waiting for all subprocesses done...')
        
        process_pool.close()
        process_pool.join()
        
        # reorg da_dict

        for idx, res in enumerate(results):
            if idx==0:
                da_dic=res.get()
            else:
                for var in varlist:
                    da_dic[var]=xr.concat(
                        [da_dic[var], res.get()[var]], dim='time') 
        
        # ------global info
        # -------read the first file to fill data structure
        nc_fn=nc_fn_base+'wrfout_d01_'+datestamp.strftime('%Y-%m-%d_%H:%M:%S')
        utils.write_log(print_prefix+'Read first file for metadata')
        
        ncfile=nc4.Dataset(nc_fn)
        # lats lons on mass and staggered grids
        self.xlat=wrf.getvar(ncfile,'XLAT')
        self.xlong=wrf.getvar(ncfile,'XLONG')
        ncfile.close()
       

        
        self.data_dic = da_dic 
        self.varlist=varlist
        # shape
        shp=self.data_dic[varlist[0]].shape
        self.nrec=shp[0]
        self.nrow=shp[1]
        self.ncol=shp[2]

def run_mtsk(itsk, file_dates, da_dic, wrf_hdl):
    """
    multitask read file
    """
    nc_fn_base=wrf_hdl.nc_fn_base
    varlist=wrf_hdl.varlist
    len_files=len(file_dates)
    da_dic={}
    
    # read the first file in the list
    datestamp=file_dates[0]
    nc_fn=nc_fn_base+'wrfout_d01_'+datestamp.strftime('%Y-%m-%d_%H:%M:%S')
    utils.write_log('%sTASK[%02d]: Read %04d of %04d --- %s' % (print_prefix, itsk, 0,(len_files-1), nc_fn))
    
    ncfile=nc4.Dataset(nc_fn)
    for var in varlist:
            da_dic[var]=get_var_xr(ncfile,var)
    ncfile.close()
    
    # read the rest files in the list
    for idx, datestamp in enumerate(file_dates[1:]):
        nc_fn=nc_fn_base+'wrfout_d01_'+datestamp.strftime('%Y-%m-%d_%H:%M:%S')
        utils.write_log('%sTASK[%02d]: Read %04d of %04d --- %s' % (print_prefix, itsk, (idx+1), (len_files-1), nc_fn))
        #utils.write_log(print_prefix+'TASK[%02d]: Read '+nc_fn % itsk)
        
        ncfile=nc4.Dataset(nc_fn)
        
        for var in varlist:
            da_dic[var]=xr.concat([da_dic[var],get_var_xr(ncfile,var)], dim='time')
    
        ncfile.close()
    utils.write_log('%sTASK[%02d]: All files loaded.' % (print_prefix, itsk))
    return da_dic

def get_var_xr(ncfile, var):
    ''' retrun var xr obj according to var name'''
    
    if var == 'h500' or var == 'h200' :
        z=wrf.getvar(ncfile,'z')
        pres=wrf.getvar(ncfile,'pressure')

    if var == 'h500':
        var_xr=wrf.interplevel(z, pres, 500).interpolate_na(dim='south_north',fill_value='extrapolate')
    elif var == 'h200':
        var_xr=wrf.interplevel(z, pres, 200).interpolate_na(dim='south_north',fill_value='extrapolate')
    else:
        var_xr=wrf.getvar(ncfile, var)
    return var_xr


def get_varlist(cfg):
    ''' seperate vars in cfg varlist csv format '''
    varlist=cfg['SHARE']['var'].split(',')
    varlist=[ele.strip() for ele in varlist]
    return varlist


if __name__ == "__main__":
    '''
    Code for unit test
    '''
    utils.write_log('Read Config...')

    
    # init wrf handler and read training data
    wrf_hdl=lib.preprocess_wrfinp.WrfMesh(cfg_hdl)
 
