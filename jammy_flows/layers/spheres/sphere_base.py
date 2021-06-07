import torch
from torch import nn
import numpy

from .. import layer_base

class sphere_base(layer_base.layer_base):

    def __init__(self, dimension=1, euclidean_to_sphere_as_first=True, use_extra_householder=True, use_permanent_parameters=False, higher_order_cylinder_parametrization=False):
    
        super().__init__(dimension=dimension)

        self.euclidean_to_sphere_as_first=euclidean_to_sphere_as_first
        self.use_extra_householder=use_extra_householder
        self.use_permanent_parameters=use_permanent_parameters

        self.higher_order_cylinder_parametrization=higher_order_cylinder_parametrization

        if(self.higher_order_cylinder_parametrization):
            assert(dimension > 1)

        self.num_householder_params=0

        if(self.use_extra_householder):

            self.num_householder_params=(dimension+1)*(dimension+1)
            self.total_param_num+=self.num_householder_params

      
            if(self.use_permanent_parameters):

                self.householder_params=nn.Parameter(
                    torch.randn(self.num_householder_params).type(torch.double)
                )

            else:

                self.householder_params=torch.zeros(self.num_householder_params).type(torch.double).unsqueeze(0)

    def return_safe_angle(self, x):

        angle_diff=1e-10

        small_mask=x<angle_diff

        large_mask=x>(numpy.pi-angle_diff)

        ret=torch.ones_like(x)
        ret[small_mask]=angle_diff
        ret[large_mask]=numpy.pi-angle_diff
        ret[(~small_mask) & (~large_mask)]=x[(~small_mask) & (~large_mask)]

        return ret

    def compute_householder_matrix(self, vs, dim,device=torch.device("cpu")):

        Q = torch.eye(dim, device=device).type(torch.double).unsqueeze(0).repeat(vs.shape[0], 1,1)
       
        for i in range(1):
        
            v = vs[:,i].reshape(-1,dim, 1).to(device)
            
            v = v / v.norm(dim=1).unsqueeze(-1)

            Qi = torch.eye(dim, device=device).type(torch.double).unsqueeze(0) - 2 * torch.bmm(v, v.permute(0, 2, 1))

            Q = torch.bmm(Q, Qi)

        return Q

    def eucl_to_spherical_embedding(self, x):
        
        ## Follows convention in DOI: 10.2307/2308932
        
        angles=[]
        for ind in range(x.shape[1]-1):
            if(ind< (x.shape[1]-2)):
                angles.append(torch.acos(x[:,ind:ind+1]/torch.sum(x[:,ind:]**2, dim=1, keepdims=True).sqrt()))
            else:

                new_angle=torch.acos(x[:,ind:ind+1]/torch.sum(x[:,ind:]**2, dim=1, keepdims=True).sqrt())
                mask_smaller=(x[:,ind+1:ind+2]<0).double()
                
                new_angle=mask_smaller*(2*numpy.pi-new_angle)+(1.0-mask_smaller)*new_angle
                angles.append(new_angle)

        return torch.cat(angles, dim=1)

    def spherical_to_eucl_embedding(self, x):

        ## Follows convention in DOI: 10.2307/2308932
        ## 2d is flipped, and 3d is permuted twice from usual x=rcosphi sin theta, y=r sinphi sintheta, z= r cos theta

        if(self.dimension==1):

            eucl=torch.cat( [torch.cos(x), torch.sin(x)], dim=1)

            return eucl

        elif(self.dimension==2):
            # theta / phi
            theta=x[:,0:1]
            phi=x[:,1:2]

            x=torch.cos(theta)
            y=torch.sin(theta)*torch.cos(phi)
            z=torch.sin(theta)*torch.sin(phi)

            eucl=torch.cat( [x,y,z], dim=1)

            return eucl
        else:

            eucl_list=[]

            for ind in range(x.shape[1]):
                if(ind==0):
                    eucl_list.append(torch.cos(x[:,0]))
                elif(ind==1):
                    eucl_list.append(torch.sin(x[:,0])*torch.cos(x[:,1]))
                else:
                    base=torch.sin(x[:,0])

                    for internal_ind in range(ind-1):
                        base*=torch.sin(x[:,internal_ind+1])

                    if(ind==x.shape[1]-1):
                        base*=torch.sin(x[:,-1])
                    else:
                        base*=torch.cos(x[:,-1])

                    eucl_list.append(base)

            eucl=torch.cat( eucl_list, dim=1)

            return eucl

    def inplane_euclidean_to_spherical(self, x, log_det):

        ## in concordance with embedding transformation first coordinate will be radius, last coordinate the 0-2pi angle, all intermediate angles 0-pi
        
        transformed_coords=[]
        keep_sign=None

        for ind in range(self.dimension):
            if(ind==0):
                radius=(x**2).sum(dim=1, keepdims=True).sqrt()

                ## we dont want radii of exactly 0
                radius[radius==0]=1e-10

                transformed_coords.append(radius)
                keep_sign=(x>=0)*1.0

                """
                    Jacobi determinante for r cancels out later, so we leave it out
                """
                #if(self.higher_order_cylinder_parametrization==False):
                    ## standard jacobian 
                #    log_det+=-torch.log(radius[:,0])*(self.dimension-1)
                                    
            else:
                

                mod_ind=ind-1

                new_angle=torch.acos(x[:,mod_ind:mod_ind+1]/torch.sum(x[:,mod_ind:]**2, dim=1, keepdims=True).sqrt())
               
                if(ind==self.dimension-1):
                    ## last one, check sign flip
                    mask_smaller=(x[:,ind:ind+1]<0).double()
                    new_angle=mask_smaller*(2*numpy.pi-new_angle)+(1.0-mask_smaller)*new_angle
                else:
                    raise NotImplementedError("D>2 not implemented for D-spheres currently")
                    log_det=log_det+torch.log(torch.sin(new_angle[:,0]))*(self.dimension-1-ind)

                transformed_coords.append(new_angle)

        
        return torch.cat(transformed_coords, dim=1), log_det, keep_sign

    def inplane_spherical_to_euclidean(self, x, log_det, sign):
        ## sign is required only for 1-dimensional transformation, which is ambiguous since there is no angle
        if(self.dimension==1):
           
            return x*sign, log_det
        elif(self.dimension==2):

            x_val=x[:,0:1]*torch.cos(x[:,1:2])
            y_val=x[:,0:1]*torch.sin(x[:,1:2])

        
            ## standard jacobian .. not needed if corresponding inverse factor in *sphere_to_plane* is not used
            """
                Jacobi determinante for r cancels out later, so we leave it out
            """
            #if(self.higher_order_cylinder_parametrization==False):

            #    log_det+=torch.log(x[:,0])

            return torch.cat([x_val,y_val], dim=1), log_det

        else:
            transformed_coords=[]

            for ind in range(self.dimension):

                if(ind==0):
                    coord=x[:,0]*torch.cos(x[:,1])
                    transformed_coords.append(coord)
                elif(ind==1):
                    
                    coord_y=x[:,0]*torch.sin(x[:,1]*torch.cos(x[:,2]))
                    transformed_coords.append(coord_y)
                else:
                    base=x[:,0]
                    for mod_ind in range(ind-1):
                        base*=torch.sin(x[:,mod_ind+1])

                    if(ind==(self.dimension-1)):
                        base*=torch.sin(x[:,ind])
                    else:
                        base*=torch.cos(x[:,ind])

            return torch.cat(transformed_coords, dim=1), log_det

    def sphere_to_plane(self, x, log_det, sf_extra=None):

        sign=None

        if(self.dimension==1):
            
            sign=(x>numpy.pi)*-1.0+(x<=numpy.pi)*1.0
            new_x=(sign>0)*x+(sign<0)*(2*numpy.pi-x)


            ## based on -pi-pi
            #sign=(x<0)*-1.0+(x>=0)*1.0
            #new_x=(sign>0)*x+(sign<0)*(-x)
            
            x=numpy.sqrt(2.0)*torch.erfinv(1.0-new_x/numpy.pi)
           
          
            ## take inverse derivative coz its easier to calculate
            log_det=log_det-numpy.log(numpy.sqrt(2.0*numpy.pi)) + (x[:,0]**2)/2.0    

        elif(self.dimension==2):
            
            if(self.higher_order_cylinder_parametrization):

                 ## work with lncyl->lnr

                lnr=0.5*torch.log(-2.0*x[:,0:1])

                #log_det+=(-lnr-x[:,0:1]).sum(axis=-1)
                log_det=log_det+(-x[:,0:1]).sum(axis=-1)

                x[:,0:1]=torch.exp(lnr)#

                #print("logdet sphere_to_plane ", (-x[:,0:1]).sum(axis=-1))
                
            else:
                cos_x=torch.cos(x[:,0:1])
              
                good_cos_x=(cos_x!=1.0) & (cos_x!=-1.0)
               
                cos_x=(cos_x==1.0)*(cos_x-1e-5)+(cos_x==-1.0)*(cos_x+1e-5)+good_cos_x*cos_x

                r_g=torch.sqrt(-torch.log( (1.0-cos_x)/2.0 )*2.0)
 
                inner=1.0-2.0*torch.exp(-((r_g)**2)/2.0)

                ## the normal logdet .. we use another factor that drops the r term and is in concordance with *inplane_spherical_to_euclidean* definition
                ## we also drop the sin(theta) factor, to be in accord with the spherical measure
                ### FULL TERM:
                ### log_det+=-torch.log(r_g[:,0])-torch.log(1.0-cos_x[:,0])+torch.log(torch.sin(x[:,0]))
                log_det=log_det-torch.log(1.0-cos_x[:,0])#+torch.log(torch.sin(x[:,0]))
        
                x=torch.cat([r_g, x[:,1:2]],dim=1)

        else:
            print("dimension > 2 not implement at the moment")
            raise NotImplementedError

        x, log_det=self.inplane_spherical_to_euclidean(x, log_det,sign)

        return x, log_det

    def plane_to_sphere(self, x, log_det):

        x, log_det, keep_sign=self.inplane_euclidean_to_spherical(x, log_det)
        sf_extra=None
        #print("log det initial", log_det)
        ## first coordinate is now radial coordinate, other coordinates are angles
        if(self.dimension==1):
            
            log_det=log_det+numpy.log(numpy.sqrt(2.0*numpy.pi))-(x[:,0]**2)/2.0
            
            x=numpy.pi*(1.0-torch.erf(x/numpy.sqrt(2.0)))
            
            ## go from 0/pi and +1/-1 binary sign to a full 2pi range and get rid of the binary sign
            #x=keep_sign*x+(1.0-keep_sign)*(-x)
            x=keep_sign*x+(1.0-keep_sign)*(2*numpy.pi-x)
            
        elif(self.dimension==2):
            ## first dim is radius so only transform that

            if(self.higher_order_cylinder_parametrization):

                lncyl=-((x[:,0:1])**2)/2.0
                lnr=torch.log(x[:,0:1])


                ####
                #log_det+=(lnr+lncyl).sum(axis=-1)
                log_det=log_det+(lncyl).sum(axis=-1)
                
                large_r_bound=10.0
                small_r_bound=0.001

                large_mask=x[:,0:1]>=large_r_bound
                middle_mask=(x[:,0:1]>small_r_bound) & (x[:,0:1]<large_r_bound)
                small_mask=x[:,0:1] <=small_r_bound

                sfcyl=torch.ones_like(lncyl)
                sfcyl[small_mask]=2*lnr[small_mask]-numpy.log(2.0)
                sfcyl[middle_mask]=torch.log(1.0-torch.exp(-x[:,0:1]**2/2.0))[middle_mask]
                sfcyl[large_mask]=-torch.exp(-x[:,0:1]**2/2.0)[large_mask]

                sf_extra=sfcyl
                x[:,0:1]=lncyl

                #print("logdet planetosphere ", (lncyl).sum(axis=-1))
               
            else:

                new_theta=torch.acos(1.0-2.0*torch.exp(-((x[:,0:1])**2)/2.0))

                r_g=x[:,0]
              
                inner=1.0-2.0*torch.exp(-((r_g)**2)/2.0)
                
                
                #log_det+=-0.5*torch.log(1.0-inner**2)+torch.log(r_g*2.0)-0.5*r_g**2

                # |dr/dtheta| = (1/r)/(1-cos(theta)) * sin(theta)
                ## sin(theta) is the area element factor

                ## the normal logdet .. we use another factor that drops the r term and is in concordance with *inplane_spherical_to_euclidean* definition
                ## we also drop the sin(theta) factor, to be in accord with the spherical measure
                ## FULL TERM:
                ## log_det-=-torch.log(r_g)-torch.log(1.0-torch.cos(new_theta[:,0]))#+torch.log(torch.sin(new_theta[:,0]))
                log_det=log_det+torch.log(1.0-torch.cos(new_theta[:,0]))#+torch.log(torch.sin(new_theta[:,0]))
              
               
                x=torch.cat([new_theta, x[:,1:2]],dim=1)

        else:
            print("dimension > 2 not implement at the moment")
            raise NotImplementedError

        return x, log_det, sf_extra

    ## inverse flow mapping
    def inv_flow_mapping(self, inputs, extra_inputs=None, include_area_element=True):
        
        [x, log_det] = inputs

        ## (1) apply inverse householder rotation if desired

        if(self.use_extra_householder):
           
            eucl=self.spherical_to_eucl_embedding(x)

            ## householder dimension is one higher than sphere dimension (we rotate in embedding space)
            hh_dim=self.dimension+1

            mat_pars=torch.reshape(self.householder_params, [1, hh_dim, hh_dim])

            if(extra_inputs is not None):
                
                mat_pars=mat_pars+torch.reshape(extra_inputs[:,:self.num_householder_params], [x.shape[0], hh_dim, hh_dim])

                #extra_input_counter+=self.num_householder_params
            else:
                mat_pars=mat_pars.repeat(x.shape[0],1,1)

            mat=self.compute_householder_matrix(mat_pars, hh_dim, device=x.device)

            ## permute because we do inverse rotation
            eucl = torch.bmm(mat.permute(0,2,1), eucl.unsqueeze(-1)).squeeze(-1)

            x=self.eucl_to_spherical_embedding(eucl)


        
        ## correction required due to sphere
        if(x.shape[1]==2):
            #print("inv 1) x ", x[:,0:1])
            safe_angles=self.return_safe_angle(x[:,0:1])
            x=torch.cat( [safe_angles, x[:,1:]], dim=1)
            #log_det+=-torch.log(torch.sin(x[:,0]))
          
            #print("inv 1) ld ", -torch.log(torch.sin(x[:,0])))
        ## (2) apply sphere intrinsic inverse flow function
        ## in 1 d case, _inv_flow_mapping should take as input values between 0 and 2pi, and outputs values between -pi and pi for easier further processing

        sf_extra=None
        if(extra_inputs is None):
            inv_flow_results = self._inv_flow_mapping([x, log_det])
        else:   
            inv_flow_results = self._inv_flow_mapping([x, log_det], extra_inputs=extra_inputs[:, self.num_householder_params:])
        
        x, log_det = inv_flow_results[:2]

        if(self.higher_order_cylinder_parametrization):
            sf_extra=inv_flow_results[2]

        ## (3) apply sphere to euclidean space stereographic projection if this is the first flow in the chain
        if(self.euclidean_to_sphere_as_first):

            x, log_det=self.sphere_to_plane(x, log_det, sf_extra=sf_extra)

        return x, log_det

    ## flow mapping (sampling pass)
    def flow_mapping(self,inputs, extra_inputs=None):
      
        [x, log_det] = inputs

        ## (1) first plane to sphere stereographic mapping
        sf_extra=None
        if(self.euclidean_to_sphere_as_first):
            x, log_det, sf_extra=self.plane_to_sphere(x, log_det)

        ## (2) apply sphere-intrinsic flow
        if(extra_inputs is None):
            x,log_det = self._flow_mapping([x, log_det], sf_extra=sf_extra)
        else:   
            x,log_det = self._flow_mapping([x, log_det], extra_inputs=extra_inputs[:, self.num_householder_params:], sf_extra=sf_extra)

        ## correction due to sphere            
        if(x.shape[1]==2):

            safe_angles=self.return_safe_angle(x[:,0:1])

            x=torch.cat( [safe_angles, x[:,1:]], dim=1)

            #log_det+=torch.log(torch.sin(x[:,0]))
    
        ## (3) apply householder rotation in embedding space if wanted
        #extra_input_counter=0
        if(self.use_extra_householder):

            eucl=self.spherical_to_eucl_embedding(x)

            #xy=torch.cat((x, y), dim=1)

            #indices=self.householder_indices

            ## householder dimension is one higher than sphere dimension (we rotate in embedding space)
            hh_dim=self.dimension+1

            mat_pars=torch.reshape(self.householder_params, [1, hh_dim, hh_dim])

            if(extra_inputs is not None):
                
                mat_pars=mat_pars+torch.reshape(extra_inputs[:,:self.num_householder_params], [x.shape[0], hh_dim, hh_dim])

            
                #extra_input_counter+=self.num_householder_params
            else:
                mat_pars=mat_pars.repeat(x.shape[0],1,1)

            mat=self.compute_householder_matrix(mat_pars, hh_dim, device=x.device)

            eucl = torch.bmm(mat, eucl.unsqueeze(-1)).squeeze(-1)  # uncomment
           
            x=self.eucl_to_spherical_embedding(eucl)

        return x,log_det

    def init_params(self, params):

        assert(len(params)==self.total_param_num)

       
        if(self.use_extra_householder):
            
            self.householder_params.data=params[:self.num_householder_params].reshape(1, self.num_householder_params)

            ## initialize child layers
            self._init_params(params[self.num_householder_params:])
        else:
           
            self._init_params(params)

    def get_desired_init_parameters(self):

        ## householder params are defined by this parent spherical layer .. layer-specific params are defined by _get_desired_init_parameters
        par_list=[]
        if(self.num_householder_params>0):

            par_list.append(torch.randn((self.num_householder_params)))

        par_list.append(self._get_desired_init_parameters())

        return torch.cat(par_list)

    def return_problematic_pars_between_hh_and_intrinsic(self, x, extra_inputs=None, flag_pole_distance=0.02):
        """
        This function allows to return the coordinates after (inverse) or before (forward) the householder rotation. 
        Intended to be used for crosschecks and plotting purposes, since coordinates close to the poles can make problems for spheres.
        """
        
        if(self.use_extra_householder==False):
            return torch.Tensor([])
        
        eucl=self.spherical_to_eucl_embedding(x)

        ## householder dimension is one higher than sphere dimension (we rotate in embedding space)
        hh_dim=self.dimension+1

        mat_pars=torch.reshape(self.householder_params, [1, hh_dim, hh_dim])

        if(extra_inputs is not None):
            
            mat_pars=mat_pars+torch.reshape(extra_inputs[:,:self.num_householder_params], [x.shape[0], hh_dim, hh_dim])

            #extra_input_counter+=self.num_householder_params
        else:
            mat_pars=mat_pars.repeat(x.shape[0],1,1)

        mat=self.compute_householder_matrix(mat_pars, hh_dim, device=x.device)

        ## permute because we do inverse rotation
        eucl = torch.bmm(mat.permute(0,2,1), eucl.unsqueeze(-1)).squeeze(-1)

        new_pts=self.eucl_to_spherical_embedding(eucl)

        mask=(new_pts[:,0]<flag_pole_distance) | (new_pts[:,0] >(numpy.pi-flag_pole_distance))
        #mask=(new_pts[:,1]<flag_pole_distance) | (new_pts[:,1] >(2*numpy.pi-flag_pole_distance))
        
        problematic_points=x[mask]

        #print("problematic points", problematic_points)
        return problematic_points

    def _embedding_conditional_return(self, x):
        return self.spherical_to_eucl_embedding(x)
    def _embedding_conditional_return_num(self): 
        return self.dimension+1

    ##########################################################
    ## Functions to override 
    ##########################################################
    
    def _init_params(self, params):
        raise NotImplementedError

    def _get_desired_init_parameters(self):
        raise NotImplementedError

    def _inv_flow_mapping(self, inputs, extra_inputs=None, sf_extra=None):
        raise NotImplementedError

    def _flow_mapping(self, inputs, extra_inputs=None, sf_extra=None):
        raise NotImplementedError

    




    