#/usr/bin/env python
"""
Core Component: Prism Classifier 
    Classes: 
    -----------
        prism: core class, weather classifier 

    Functions:
    -----------
"""
import xarray as xr
import numpy as np
import pandas as pd
import copy
import sys, os
import json

from utils import utils
import minisom
import pickle

# calculate metrics
import sklearn.metrics as skm

CWD=sys.path[0]
print_prefix='core.prism>>'

class Prism:

    '''
    Prism clusterer, use wrf mesh variables 
    
    Attributes
    -----------

    Methods
    -----------
    train(), train the model by historical WRF data
    cast(), cast on real-time data 
    evaluate(), evaluate the model performance by several metrics

    '''
    
    def __init__(self, wrf_hdl, cfg_hdl, call_from='trainning'):
        """ construct prism classifier """
        self.nrec=wrf_hdl.nrec
        nrow=self.nrow=wrf_hdl.nrow
        ncol=self.ncol=wrf_hdl.ncol

        self.nfea=nrow*ncol 
        varlist=self.varlist=wrf_hdl.varlist 
        self.nvar=len(varlist)
        self.dateseries=wrf_hdl.dateseries

        self.xlat, self.xlong=wrf_hdl.xlat, wrf_hdl.xlong
        
        # self.data(recl, nvar, nrow*ncol)
        self.data=np.empty([self.nrec,self.nvar,nrow*ncol])

        if call_from=='trainning':
            for idx, var in enumerate(varlist):
                raw_data=wrf_hdl.data_dic[var].values.reshape((self.nrec,-1))
                self.data[:,idx,:]=raw_data
                
            self.preprocess=cfg_hdl['TRAINING']['preprocess_method']
            self.n_nodex=int(cfg_hdl['TRAINING']['n_nodex'])
            self.n_nodey=int(cfg_hdl['TRAINING']['n_nodey'])
            self.sigma=float(cfg_hdl['TRAINING']['sigma'])
            self.lrate=float(cfg_hdl['TRAINING']['learning_rate'])
            self.iterations=int(cfg_hdl['TRAINING']['iterations'])
            self.nb_func=cfg_hdl['TRAINING']['nb_func']

            if self.preprocess == 'temporal_norm':
                self.data, self.mean, self.std=utils.get_std_dim0(self.data)
 
        elif call_from=='inference':
            db_in=xr.load_dataset(CWD+'/db/som_cluster.nc')            
            
            self.preprocess=db_in.attrs['preprocess_method']
            self.nb_func=db_in.attrs['neighbourhood_function']
            self.n_nodex=db_in.dims['n_nodex']
            self.n_nodey=db_in.dims['n_nodey']
            self.resamp_frq=cfg_hdl['INFERENCE']['resamp_freq']

            # dispatch wrf_hdl.data
            for idx, var in enumerate(varlist):
                raw_data=wrf_hdl.data_dic[var].values.reshape((self.nrec,-1))
                self.data[:,idx,:]=raw_data
 
            if self.preprocess == 'temporal_norm':
                mean, std = db_in['mean'].values, db_in['std'].values
                mean = mean.reshape(self.nvar,-1)
                std = std.reshape(self.nvar,-1)

                for ii in range(0, self.nrec):
                    self.data[ii,:,:]=(self.data[ii,:,:]-mean)/std
        
        self.data=self.data.reshape((self.nrec,-1))

    def train(self, train_data=None, verbose=True):
        """ train the prism classifier """
        if verbose:
            utils.write_log(print_prefix+'trainning...')
        
        if train_data is None:
            train_data = self.data
        
        # init som
        som = minisom.MiniSom(
                self.n_nodex, self.n_nodey, self.nvar*self.nfea, 
                neighborhood_function=self.nb_func, sigma=self.sigma, 
                learning_rate=self.lrate) 
        
        # train som
        som.train(train_data, self.iterations, verbose=verbose) 

        self.q_err=som.quantization_error(train_data)

        self.winners=[som.winner(x) for x in train_data]
        self.som=som
        
    def cast(self):
        """ cast the prism on new synoptic maps """
        utils.write_log(print_prefix+'casting...')
        self.load()
        
        # archive classification result in csv
        winners=[self.som.winner(x) for x in self.data]
        
        data_list=[]
        for datestamp, winner in zip(self.dateseries, winners):
            data_list.append(
                    ['('+str(winner[0])+','+str(winner[1])+')', 
                    winner[0]*self.n_nodey+winner[1]])
        
        df_out = pd.DataFrame(
                data_list, columns=['type2d_cor', 'type_id'],
                index=self.dateseries)

        df_out=df_out.resample(self.resamp_frq).apply(
                lambda x: x.value_counts().index[0])
        df_out.to_csv(CWD+'/output/inference_cluster.csv')
        
        utils.write_log(print_prefix+'prism inference is completed!')

    def evaluate(self,cfg, train_data=None, verbose=True):
        """ evaluate the clustering result """
        if verbose: 
            utils.write_log(print_prefix+'prism evaluates...')
        
        if train_data is None:
            train_data = self.data
        
        edic={'quatization_error':self.q_err}
        
        label=[str(winner[0])+str(winner[1]) for winner in self.winners]
        s_score=skm.silhouette_score(train_data, label, metric='euclidean')
        
        edic.update({'silhouette_score':s_score})
        
        if verbose:
            utils.write_log(print_prefix+'prism evaluation dict: %s' % str(edic))

        edic.update({'cfg_para':cfg._sections})
        
        self.edic=edic

    def archive(self):
        """ archive the prism classifier in database """

        utils.write_log(print_prefix+'prism archives...')
        
        # archive evaluation dict
        with open(CWD+'/db/edic.json', 'w') as f:
            json.dump(self.edic,f)

        # archive model
        with open(CWD+'/db/som.archive', 'wb') as outfile:
            pickle.dump(self.som, outfile)

        # archive classification result in csv
        with open(CWD+'/db/train_cluster.csv', 'w') as f:
            for datestamp, winner in zip(self.dateseries, self.winners):
                f.write(datestamp.strftime('%Y-%m-%d_12:00:00,')+str(winner[0])+','+str(winner[1])+'\n')

        # archive classification result in netcdf
        centroid=self.som.get_weights()
        centroid=centroid.reshape(self.n_nodex, self.n_nodey, self.nvar, self.nrow, self.ncol)
        
        ds_out=self.org_output_nc(centroid)
        out_fn=CWD+'/db/som_cluster.nc'
        ds_out.to_netcdf(out_fn)
        
        utils.write_log(print_prefix+'prism construction is completed!')


    def org_output_nc(self, centroid):
        """ organize output file """
        ds_vars={   
            'som_cluster':(['n_nodex','n_nodey','nvar', 'nrow','ncol'], centroid),
            'var_vector':(['ntimes','ngrids'], self.data),
            'xlat':(['nrow', 'ncol'], self.xlat),
            'xlong':(['nrow', 'ncol'], self.xlong)}
        
        ds_coords={'nvar':self.varlist}
        ds_attrs={
            'preprocess_method':self.preprocess,
            'neighbourhood_function':self.nb_func}
        
        if self.preprocess == 'temporal_norm':
            self.mean=self.mean.reshape(self.nvar, self.nrow, self.ncol)
            self.std=self.std.reshape(self.nvar, self.nrow, self.ncol)
            
            # reverse temporal_norm
            for ii in range(0, self.n_nodex):
                for jj in range(0, self.n_nodey):
                    centroid[ii,jj,:,:,:]=centroid[ii,jj,:,:,:]*self.std+self.mean
            ds_vars.update({
                'mean':(['nvar', 'nrow', 'ncol'], self.mean),
                'std':(['nvar','nrow', 'ncol'], self.std)}) 
        
        ds_out= xr.Dataset(
            data_vars=ds_vars, coords=ds_coords,
            attrs=ds_attrs) 

        return ds_out


    def load(self):
        """ load the archived prism classifier in database """
        with open(CWD+'/db/som.archive', 'rb') as infile:
            self.som = pickle.load(infile)

if __name__ == "__main__":
    pass
