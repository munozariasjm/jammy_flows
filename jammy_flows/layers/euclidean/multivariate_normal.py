import torch
from torch import nn
import numpy

from .. import bisection_n_newton as bn
from .. import layer_base
from ... import extra_functions
from .. import matrix_fns
from . import euclidean_base

import math
import torch.nn.functional as F
import torch.distributions as tdist
import scipy.linalg
from scipy.optimize import minimize
import time
normal_dist=tdist.Normal(0, 1)

import pylab

def generate_log_function_bounded_in_logspace(min_val_normal_space=1, max_val_normal_space=10, center=False, clamp=False, min_clamp_value=None, max_clamp_value=None):
    
    ## min and max values are in normal space -> must be positive
    assert(min_val_normal_space > 0)

    ln_max=numpy.log(max_val_normal_space)
    ln_min=numpy.log(min_val_normal_space)

    ## this shift makes the function equivalent to a normal exponential for small values
    center_val=ln_max

    ## can also center around zero (it will be centered in exp space, not in log space)
    if(center==False):
        center_val=0.0



    if(clamp):
        def f(x):

            res=torch.cat([torch.zeros_like(x).unsqueeze(-1), (-torch.clamp(x, min=min_clamp_value, max=max_clamp_value)+center_val).unsqueeze(-1)], dim=-1)

            first_term=ln_max-torch.logsumexp(res, dim=-1, keepdim=True)

            return torch.logsumexp( torch.cat([first_term, torch.ones_like(first_term)*ln_min], dim=-1), dim=-1)
    else:
        def f(x):

            res=torch.cat([torch.zeros_like(x).unsqueeze(-1), (-x+center_val).unsqueeze(-1)], dim=-1)

            first_term=ln_max-torch.logsumexp(res, dim=-1, keepdim=True)

            return torch.logsumexp( torch.cat([first_term, torch.ones_like(first_term)*ln_min], dim=-1), dim=-1)

    return f


class mvn_block(euclidean_base.euclidean_base):
    def __init__(self, 
                 dimension, 
                 cov_type="full", 
                 use_permanent_parameters=False, 
                 model_offset=0, 
                 width_smooth_saturation=1,
                 lower_bound_for_widths=0.01,
                 upper_bound_for_widths=100,
                 softplus_for_width=0,
                 clamp_widths=0):
        """
        Modified version of official implementation in hhttps://github.com/chenlin9/Gaussianization_Flows (https://arxiv.org/abs/2003.01941). Fixes numerical issues with bisection inversion due to more efficient newton iterations, added offsets, and allows 
        to use reparametrization trick for VAEs due to Newton iterations.
        Parameters:
        dimension (int): dimension of the PDF
        num_kde (int): number of KDE s in the one-dimensional PDF
        num_householder_iter (int): if <=0, no householder transformation is performed. If positive, it defines the number of parameters in householder transformations.
        use_permanent_parameters (float): If permantent parameters are used (no depnendence on other input), or if input is used to define the parameters (conditional pdf).
        mapping_approximation (str): One of "partly_crude", "partly_precise", "full_pade". Partly_pade_crude is implemented in the original repository, but has numerical issues.
        It is recommended to use "partly_precise" or "full_pade".
        """
        super().__init__(dimension=dimension, use_permanent_parameters=use_permanent_parameters, model_offset=model_offset)
        self.init = False

        self.cov_type=cov_type
        assert(self.cov_type=="unit_gaussian" or self.cov_type=="diagonal_symmetric" or self.cov_type=="diagonal" or self.cov_type=="full" ), (self.cov_type)
        assert(lower_bound_for_widths>0.0)

        self.width_min=lower_bound_for_widths
        ## defines maximum width - None -> no maximum width .. only used for exponential width function to cap high values
        self.width_max=None

        if(upper_bound_for_widths > 0):
            self.width_max=upper_bound_for_widths

            ### clamp at three times the logarithm to upper bound log_width .. more than enough for whole range
            self.log_width_max_to_clamp=numpy.log(self.width_max)*3.0

        self.log_width_min_to_clamp=numpy.log(0.01*self.width_min)
        ## clamping widths?
        self.clamp_widths=clamp_widths

        self.softplus_for_width=softplus_for_width

        ## doing a smooth (min-max bound) regularization?
        self.width_smooth_saturation=width_smooth_saturation
        if(self.width_smooth_saturation):
            assert(self.width_max is not None), "We require a maximum saturation level for smooth saturation!"

        #########################################################
        
        ## dimension of target space
        self.dimension = dimension

        if(self.softplus_for_width):
            ## softplus
            if(clamp_widths):
                upper_clamp=None
                if(self.width_max is not None):
                    # clamp upper bound with exact width_max value
                    upper_clamp=numpy.log(self.width_max)
                self.make_positive=lambda x: torch.log(torch.nn.functional.softplus(torch.clamp(x, min=self.log_width_min_to_clamp, max=upper_clamp))+self.width_min)
            else:
                self.make_positive=lambda x: torch.log(torch.nn.functional.softplus(x)+self.width_min)
            
            

        else:
            ## exponential-type width relation
            if(self.width_smooth_saturation == 0):
                ## normal, infinetly growing exponential
                if(self.clamp_widths):
                    # clamp upper bound with exact width_max value
                    upper_clamp=None
                    if(self.width_max is not None):
                        upper_clamp=numpy.log(self.width_max)
                    self.make_positive=lambda x: torch.log(torch.exp(torch.clamp(x, min=self.log_width_min_to_clamp, max=upper_clamp))+self.width_min)
                else:
                    self.make_positive=lambda x: torch.log(torch.exp(x)+self.width_min)

            else:
                ## exponential function at beginning but flattens out at width_max -> no infinite growth
                ## numerically stable via logsumexp .. clamping should not be necessary, but can be done to damp down large gradients
                ## in weird regions of parameter space
                
                ln_width_max=numpy.log(self.width_max)
                ln_width_min=numpy.log(self.width_min)

                if(self.clamp_widths):

                    exp_like_fn=generate_log_function_bounded_in_logspace(self.width_min, self.width_max, center=True, clamp=True, min_clamp_value=self.log_width_min_to_clamp, max_clamp_value=self.log_width_max_to_clamp)

                else:

                    exp_like_fn=generate_log_function_bounded_in_logspace(self.width_min, self.width_max, center=True)

                self.make_positive=exp_like_fn

        
        if(self.cov_type=="diagonal_symmetric"):

            if(self.use_permanent_parameters):
                self.single_diagonal_log = nn.Parameter(torch.randn(1, 1).type(torch.double))

            self.total_param_num+=1

        elif(self.cov_type=="diagonal"):

            if(self.use_permanent_parameters):
                self.full_diagonal_log = nn.Parameter(torch.randn(1, self.dimension).type(torch.double))

            self.total_param_num+=self.dimension

        elif(self.cov_type=="full"):

            if(self.use_permanent_parameters):
                self.full_diagonal_log = nn.Parameter(torch.randn(1, self.dimension).type(torch.double))
              
                self.lower_triangular_entries = nn.Parameter(torch.randn(1, int(self.dimension*(self.dimension-1)/2)  ).type(torch.double))

            self.total_param_num+=int(self.dimension+self.dimension*(self.dimension-1)/2)



        #######################################


    def _obtain_usable_flow_params(self, x, cov_type, extra_inputs=None):

        single_diagonal=None
        full_diagonal=None
        lower_triangular_entries=None

        if(cov_type=="unit_gaussian"):
            return None, None, None

        if(extra_inputs is not None):
            extra_counter=0
            if(cov_type=="diagonal_symmetric"):

                single_diagonal=extra_inputs

            elif(cov_type=="diagonal"):

                full_diagonal=extra_inputs

            elif(cov_type=="full"):

                full_diagonal=extra_inputs[:, :self.dimension]

                lower_triangular_entries=extra_inputs[:,self.dimension:]
        else:

            if(cov_type=="diagonal_symmetric"):
                single_diagonal=self.single_diagonal_log.to(x)

            elif(cov_type=="diagonal"):

                full_diagonal=self.full_diagonal_log.to(x)

            elif(cov_type=="full"):

                full_diagonal=self.full_diagonal_log.to(x)

                lower_triangular_entries=self.lower_triangular_entries.to(x)

        return single_diagonal, full_diagonal, lower_triangular_entries

    def _flow_mapping(self, inputs, extra_inputs=None, verbose=False, lower=-1e5, upper=1e5): 
        
        [z, log_det]=inputs

        if(self.cov_type=="unit_gaussian"):
            ## save time
            return z, log_det

        single_log_diagonal, full_log_diagonal, lower_triangular_entries=self._obtain_usable_flow_params(z, self.cov_type, extra_inputs=extra_inputs)

        # use inverse of lower transformation matrix
        trafo_matrix, extra_logdet=matrix_fns.obtain_lower_triangular_matrix_and_logdet(self.dimension, single_log_diagonal_entry=single_log_diagonal, log_diagonal_entries=full_log_diagonal, lower_triangular_entries=lower_triangular_entries, cov_type=self.cov_type)

        res=torch.einsum("...ij, ...j", trafo_matrix, z)
        log_det=log_det+extra_logdet

        return res, log_det

    def _inv_flow_mapping(self, inputs, extra_inputs=None):

        [x, log_det] = inputs

        if(self.cov_type=="unit_gaussian"):
            # save time
            return x, log_det

        single_log_diagonal, full_log_diagonal, lower_triangular_entries=self._obtain_usable_flow_params(x, self.cov_type, extra_inputs=extra_inputs)

        ## normally the inverse mapping involves the Upper trinagular matrix .. but we can just a well work with the lower triangular one, which just perumtes the dimensions
        trafo_matrix, extra_logdet=matrix_fns.obtain_inverse_lower_triangular_matrix_and_logdet(self.dimension, single_log_diagonal_entry=single_log_diagonal, log_diagonal_entries=full_log_diagonal, lower_triangular_entries=lower_triangular_entries, cov_type=self.cov_type)

        #res=torch.bmm(trafo_matrix, x)
        res=torch.einsum("...ij, ...j", trafo_matrix, x)
        log_det=log_det+extra_logdet

        return res, log_det

    def _get_desired_init_parameters(self):

        ## householder params / means of kdes / log_widths of kdes / normalizations (if fit normalization)

        if(self.cov_type=="unit_gaussian"):
            return torch.empty(0)

        desired_param_vec=[]

        if(self.cov_type=="diagonal_symmetric"):

            ## means
            desired_param_vec.append(torch.zeros(1,dtype=torch.float64))

        elif(self.cov_type=="diagonal"):
            desired_param_vec.append(torch.zeros(self.dimension,dtype=torch.float64))

        elif(self.cov_type=="full"):
            desired_param_vec.append(torch.zeros(self.dimension,dtype=torch.float64))
            desired_param_vec.append(torch.zeros(int(self.dimension*(self.dimension-1)/2) ,dtype=torch.float64))

        return torch.cat(desired_param_vec)

    def _init_params(self, params):

        if(self.cov_type=="diagonal_symmetric"):  
           
            self.single_diagonal_log.data=torch.reshape(params[:1], [1, 1])

        elif(self.cov_type=="diagonal"):

            self.full_diagonal_log.data=torch.reshape(params[:self.dimension], [1, self.dimension])

        elif(self.cov_type=="diagonal"):

            self.full_diagonal_log.data=torch.reshape(params[:self.dimension], [1, self.dimension])
            self.lower_triangular_entries.data=torch.reshape(params[self.dimension:], [1, int(self.dimension*(self.dimension-1)/2)])
        

    def _obtain_layer_param_structure(self, param_dict, extra_inputs=None, previous_x=None, extra_prefix=""): 
        """ 
        Debugging function that puts current flow parameters along with their name into "param_dict".
        """

        single_diagonal=None
        full_diagonal=None
        lower_triangular_entries=None

        if(self.cov_type=="unit_gaussian"):
            return 

        if(extra_inputs is not None):
            extra_counter=0
            if(self.cov_type=="diagonal_symmetric"):

                single_diagonal=extra_inputs
                param_dict[extra_prefix+"log_diagonal_symmetric"]=single_diagonal.data

            elif(self.cov_type=="diagonal"):

                full_diagonal=extra_inputs
                param_dict[extra_prefix+"log_diagonal"]=full_diagonal.data

            elif(self.cov_type=="full"):

                full_diagonal=extra_inputs[:, :self.dimension]

                param_dict[extra_prefix+"log_diagonal"]=full_diagonal.data

                lower_triangular_entries=extra_inputs[:,self.dimension:]

                param_dict[extra_prefix+"lower_trinagular_entries"]=lower_triangular_entries.data
        else:

            if(self.cov_type=="diagonal_symmetric"):

                single_diagonal=self.single_diagonal_log
                param_dict[extra_prefix+"log_diagonal_symmetric"]=single_diagonal.data

            elif(self.cov_type=="diagonal"):

                full_diagonal=self.full_diagonal_log
                param_dict[extra_prefix+"log_diagonal"]=full_diagonal.data

            elif(self.cov_type=="full"):

                full_diagonal=self.full_diagonal_log
                param_dict[extra_prefix+"log_diagonal"]=full_diagonal.data

                lower_triangular_entries=self.lower_triangular_entries

                param_dict[extra_prefix+"lower_trinagular_entries"]=lower_triangular_entries.data
