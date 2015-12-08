# -*- coding: utf-8 -*-

"""
Cuboid FoF pair search
"""

from __future__ import (absolute_import, division, print_function, unicode_literals)
import numpy as np
from time import time
import sys
import multiprocessing
from functools import partial
from scipy.sparse import coo_matrix
from .double_tree_helpers import (_set_approximate_cell_sizes, 
    _set_approximate_xy_z_cell_sizes, _enclose_in_box)
from .double_tree import *
from .cpairs.pairwise_distances import *
from ...custom_exceptions import *
from ...utils.array_utils import convert_to_ndarray, array_is_monotonic

__all__=['pair_matrix', 'xy_z_pair_matrix']
__author__=['Duncan Campbell']


def pair_matrix(data1, data2, r_max, period=None, verbose=False, num_threads=1,
                approx_cell1_size = None, approx_cell2_size = None):
    """
    Calculate the distance to all pairs with seperations less than ``r_max`` in real space.
    
    Parameters
    ----------
    data1 : array_like
        N1 by 3 numpy array of 3-D positions.
            
    data2 : array_like
        N2 by 3 numpy array of 3-D positions.
            
    r_max : float
        Maximum distance to search for pairs
    
    period : array_like, optional
        length 3 array defining axis-aligned periodic boundary conditions. If only 
        one number, Lbox, is specified, period is assumed to be np.array([Lbox]*3).
        If none, PBCs are set to infinity.  If True, period is set to be Lbox
    
    verbose : Boolean, optional
        If True, print out information and progress.
    
    num_threads : int, optional
        number of 'threads' to use in the pair counting.  if set to 'max', use all 
        available cores.  num_threads=0 is the default.
    
    approx_cell1_size : array_like, optional 
        Length-3 array serving as a guess for the optimal manner by which 
        the `~halotools.mock_observables.pair_counters.FlatRectanguloidDoubleTree` 
        will apportion the ``data`` points into subvolumes of the simulation box. 
        The optimum choice unavoidably depends on the specs of your machine. 
        Default choice is to use 1/10 of the box size in each dimension, 
        which will return reasonable result performance for most use-cases. 
        Performance can vary sensitively with this parameter, so it is highly 
        recommended that you experiment with this parameter when carrying out  
        performance-critical calculations. 
    
    approx_cell2_size : array_like, optional 
        See comments for ``approx_cell1_size``. 
    
    Returns
    -------
    dists : scipy.sparse.coo_matrix
        N1 x N2 sparse matrix in COO format containing distances between points.
    """
    
    search_dim_max = np.array([r_max, r_max, r_max])
    function_args = [data1, data2, period, num_threads, search_dim_max]
    x1, y1, z1, x2, y2, z2, period, num_threads, PBCs = _process_args(*function_args)
    xperiod, yperiod, zperiod = period 
    r_max = float(r_max)
    
    approx_cell1_size, approx_cell2_size = (
        _set_approximate_cell_sizes(approx_cell1_size, approx_cell2_size, r_max, period)
        )
    approx_x1cell_size, approx_y1cell_size, approx_z1cell_size = approx_cell1_size
    approx_x2cell_size, approx_y2cell_size, approx_z2cell_size = approx_cell2_size
    
    double_tree = FlatRectanguloidDoubleTree(x1, y1, z1, x2, y2, z2,
                                             approx_x1cell_size, approx_y1cell_size, approx_z1cell_size, 
                                             approx_x2cell_size, approx_y2cell_size, approx_z2cell_size, 
                                             r_max, r_max, r_max, xperiod, yperiod, zperiod, PBCs=PBCs)
    
    #square radial bins to make distance calculation cheaper
    r_max_squared = r_max**2.0
    
    #print come information
    if verbose==True:
        print("running for pairs with {0} by {1} points".format(len(data1),len(data2)))
        print("cell size= {0}".format(grid1.dL))
        print("number of cells = {0}".format(np.prod(grid1.num_divs)))
    
    #number of cells
    Ncell1 = double_tree.num_x1divs*double_tree.num_y1divs*double_tree.num_z1divs
    
    #create a function to call with only one argument
    engine = partial(_pair_matrix_engine, double_tree, r_max_squared, period, PBCs)
    
    #do the pair counting
    if num_threads>1:
        pool = multiprocessing.Pool(num_threads)
        result = pool.map(engine,range(Ncell1))
        pool.close()
    if num_threads==1:
        result = map(engine,range(Ncell1))
    
    #arrays to store result
    d = np.zeros((0,), dtype='float')
    i_inds = np.zeros((0,), dtype='int')
    j_inds = np.zeros((0,), dtype='int')
    
    #unpack the results
    for i in range(len(result)):
        d = np.append(d,result[i][0])
        i_inds = np.append(i_inds,result[i][1])
        j_inds = np.append(j_inds,result[i][2])
    
    #resort the result (it was sorted to make in continuous over the cell structure)
    i_inds = double_tree.tree1.idx_sorted[i_inds]
    j_inds = double_tree.tree2.idx_sorted[j_inds]
    
    return coo_matrix((d, (i_inds, j_inds)), shape=(len(data1),len(data2)))


def _pair_matrix_engine(double_tree, r_max_squared, period, PBCs, icell1):
    """
    pair counting engine for npairs function.  This code calls a cython function.
    """
    
    d = np.zeros((0,), dtype='float')
    i_inds = np.zeros((0,), dtype='int')
    j_inds = np.zeros((0,), dtype='int')
    
    #extract the points in the cell
    s1 = double_tree.tree1.slice_array[icell1]
    x_icell1, y_icell1, z_icell1 = (
        double_tree.tree1.x[s1],
        double_tree.tree1.y[s1],
        double_tree.tree1.z[s1])
    
    i_min = s1.start
    
    xsearch_length = np.sqrt(r_max_squared)
    ysearch_length = np.sqrt(r_max_squared)
    zsearch_length = np.sqrt(r_max_squared)
    adj_cell_generator = double_tree.adjacent_cell_generator(
        icell1, xsearch_length, ysearch_length, zsearch_length)
            
    adj_cell_counter = 0
    for icell2, xshift, yshift, zshift in adj_cell_generator:
                
        #extract the points in the cell
        s2 = double_tree.tree2.slice_array[icell2]
        x_icell2 = double_tree.tree2.x[s2] + xshift
        y_icell2 = double_tree.tree2.y[s2] + yshift 
        z_icell2 = double_tree.tree2.z[s2] + zshift
        
        j_min = s2.start
        
        dd, ii_inds, jj_inds = pairwise_distance_no_pbc(x_icell1, y_icell1, z_icell1,\
                                                            x_icell2, y_icell2, z_icell2,\
                                                            r_max_squared)
        
        ii_inds = ii_inds+i_min
        jj_inds = jj_inds+j_min
        
        #update storage arrays
        d = np.concatenate((d,dd))
        i_inds = np.concatenate((i_inds,ii_inds))
        j_inds = np.concatenate((j_inds,jj_inds))
    
    return d, i_inds, j_inds


def xy_z_pair_matrix(data1, data2, rp_max, pi_max, period=None, verbose=False,\
                     num_threads=1, approx_cell1_size = None, approx_cell2_size = None):
    """
    Calculate the distance to all pairs with seperations less than or equal to ``rp_max`` 
    and ``pi_max`` in redshift space.
    
    Parameters
    ----------
    data1 : array_like
        N1 by 3 numpy array of 3-dimensional positions. Should be between zero and 
        period.
            
    data2 : array_like
        N2 by 3 numpy array of 3-dimensional positions. Should be between zero and 
        period.
            
    rp_max : float
        maximum distance to connect pairs
    
    pi_max : float
        maximum distance to connect pairs
    
    period : array_like, optional
        length 3 array defining axis-aligned periodic boundary conditions. If only 
        one number, Lbox, is specified, period is assumed to be np.array([Lbox]*3).
        If none, PBCs are set to infinity.  If True, period is set to be Lbox
    
    verbose : Boolean, optional
        If True, print out information and progress.
    
    num_threads : int, optional
        number of 'threads' to use in the pair counting.  if set to 'max', use all 
        available cores.  num_threads=0 is the default.
    
    approx_cell1_size : array_like, optional 
        Length-3 array serving as a guess for the optimal manner by which 
        the `~halotools.mock_observables.pair_counters.FlatRectanguloidDoubleTree` 
        will apportion the ``data`` points into subvolumes of the simulation box. 
        The optimum choice unavoidably depends on the specs of your machine. 
        Default choice is to use 1/10 of the box size in each dimension, 
        which will return reasonable result performance for most use-cases. 
        Performance can vary sensitively with this parameter, so it is highly 
        recommended that you experiment with this parameter when carrying out  
        performance-critical calculations. 
    
    approx_cell2_size : array_like, optional 
        See comments for ``approx_cell1_size``. 
    
    Returns
    -------
    perp_dists : scipy.sparse.coo_matrix
        N1 x N2 sparse matrix in COO format containing perpendicular distances between points.
    
    para_dists : scipy.sparse.coo_matrix
        N1 x N2 sparse matrix in COO format containing parallel distances between points.
    """
    
    search_dim_max = np.array([rp_max, rp_max, pi_max])
    function_args = [data1, data2, period, num_threads, search_dim_max]
    x1, y1, z1, x2, y2, z2, period, num_threads, PBCs = _process_args(*function_args)
    xperiod, yperiod, zperiod = period 
    rp_max = float(rp_max)
    pi_max = float(rp_max)
    
    approx_cell1_size, approx_cell2_size = (
        _set_approximate_xy_z_cell_sizes(approx_cell1_size, approx_cell2_size, rp_max, pi_max, period)
        )
    approx_x1cell_size, approx_y1cell_size, approx_z1cell_size = approx_cell1_size
    approx_x2cell_size, approx_y2cell_size, approx_z2cell_size = approx_cell2_size
    
    double_tree = FlatRectanguloidDoubleTree(x1, y1, z1, x2, y2, z2,
                                             approx_x1cell_size, approx_y1cell_size, approx_z1cell_size, 
                                             approx_x2cell_size, approx_y2cell_size, approx_z2cell_size, 
                                             rp_max, rp_max, pi_max, xperiod, yperiod,zperiod, PBCs=PBCs)
    
    #square radial bins to make distance calculation cheaper
    rp_max_squared = rp_max**2.0
    pi_max_squared = pi_max**2.0
    
    #print come information
    if verbose==True:
        print("running for pairs with {0} by {1} points".format(len(data1),len(data2)))
        print("cell size= {0}".format(grid1.dL))
        print("number of cells = {0}".format(np.prod(grid1.num_divs)))
    
    #number of cells
    Ncell1 = double_tree.num_x1divs*double_tree.num_y1divs*double_tree.num_z1divs
    
    #create a function to call with only one argument
    engine = partial(_xy_z_pair_matrix_engine, double_tree, rp_max_squared, pi_max_squared, period, PBCs)
    
    #do the pair counting
    if num_threads>1:
        pool = multiprocessing.Pool(num_threads)
        result = pool.map(engine,range(Ncell1))
        pool.close()
    if num_threads==1:
        result = map(engine,range(Ncell1))
    
    #arrays to store result
    d_perp = np.zeros((0,), dtype='float')
    d_para = np.zeros((0,), dtype='float')
    i_inds = np.zeros((0,), dtype='int')
    j_inds = np.zeros((0,), dtype='int')
    
    #unpack the results
    for i in range(len(result)):
        d_perp = np.append(d_perp,result[i][0])
        d_para = np.append(d_para,result[i][1])
        i_inds = np.append(i_inds,result[i][2])
        j_inds = np.append(j_inds,result[i][3])
    
    #resort the result (it was sorted to make in continuous over the cell structure)
    i_inds = double_tree.tree1.idx_sorted[i_inds]
    j_inds = double_tree.tree2.idx_sorted[j_inds]
    
    return coo_matrix((d_perp, (i_inds, j_inds)), shape=(len(data1),len(data2))),\
           coo_matrix((d_para, (i_inds, j_inds)), shape=(len(data1),len(data2)))


def _xy_z_pair_matrix_engine(double_tree, rp_max_squared, pi_max_squared, period, PBCs, icell1):
    """
    pair counting engine for xy_z_fof_npairs function.  This code calls a cython function.
    """
    
    d_perp = np.zeros((0,), dtype='float')
    d_para = np.zeros((0,), dtype='float')
    i_inds = np.zeros((0,), dtype='int')
    j_inds = np.zeros((0,), dtype='int')
    
    #extract the points in the cell
    s1 = double_tree.tree1.slice_array[icell1]
    x_icell1, y_icell1, z_icell1 = (
        double_tree.tree1.x[s1],
        double_tree.tree1.y[s1],
        double_tree.tree1.z[s1])
    
    i_min = s1.start
    
    xsearch_length = np.sqrt(rp_max_squared)
    ysearch_length = np.sqrt(rp_max_squared)
    zsearch_length = np.sqrt(pi_max_squared)
    adj_cell_generator = double_tree.adjacent_cell_generator(
        icell1, xsearch_length, ysearch_length, zsearch_length)
    
    adj_cell_counter = 0
    for icell2, xshift, yshift, zshift in adj_cell_generator:
        adj_cell_counter +=1
        
        #extract the points in the cell
        s2 = double_tree.tree2.slice_array[icell2]
        x_icell2 = double_tree.tree2.x[s2] + xshift
        y_icell2 = double_tree.tree2.y[s2] + yshift 
        z_icell2 = double_tree.tree2.z[s2] + zshift
        
        j_min = s2.start
        
        dd_perp, dd_para, ii_inds, jj_inds = pairwise_xy_z_distance_no_pbc(\
                                                 x_icell1, y_icell1, z_icell1,\
                                                 x_icell2, y_icell2, z_icell2,\
                                                 rp_max_squared, pi_max_squared)
        
        ii_inds = ii_inds+i_min
        jj_inds = jj_inds+j_min
        
        #update storage arrays
        d_perp = np.concatenate((d_perp,dd_perp))
        d_para = np.concatenate((d_para,dd_para))
        i_inds = np.concatenate((i_inds,ii_inds))
        j_inds = np.concatenate((j_inds,jj_inds))
    
    return d_perp, d_para, i_inds, j_inds


def _process_args(data1, data2, period, num_threads, search_dim_max):
    """
    private internal function to process the arguments of the pair matrix functions.
    """
    
    if num_threads is not 1:
        if num_threads=='max':
            num_threads = multiprocessing.cpu_count()
        if not isinstance(num_threads,int):
            msg = ("\n Input ``num_threads`` argument must \n"
                   "be an integer or the string 'max'")
            raise HalotoolsError(msg)
    
    # Passively enforce that we are working with ndarrays
    x1 = data1[:,0]
    y1 = data1[:,1]
    z1 = data1[:,2]
    x2 = data2[:,0]
    y2 = data2[:,1]
    z2 = data2[:,2]

    # Set the boolean value for the PBCs variable
    if period is None:
        PBCs = False
        x1, y1, z1, x2, y2, z2, period = (
            _enclose_in_box(x1, y1, z1, x2, y2, z2, min_size=search_dim_max*3.0))
    else:
        PBCs = True
        period = convert_to_ndarray(period).astype(float)
        if len(period) == 1:
            period = np.array([period[0]]*3)
        try:
            assert np.all(period < np.inf)
            assert np.all(period > 0)
        except AssertionError:
            msg = "Input ``period`` must be a bounded positive number in all dimensions"
            raise HalotoolsError(msg)

    return x1, y1, z1, x2, y2, z2, period, num_threads, PBCs

