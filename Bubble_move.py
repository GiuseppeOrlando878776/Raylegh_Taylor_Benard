from My_Parameters import My_Parameters
from Auxiliary_Functions import *
from Boundary_Conditions import WallBoundary

from sys import exit
#import matplotlib.pyplot as plt

class BubbleMove:
    """Class constructor"""
    def __init__(self, param_name):
        """
        Param --- class Parameters to store desired configuration
        Re    --- Reynolds number
        At    --- Atwood number
        Bo    --- Bond number
        rho1  --- Lighter density
        dt    --- Specified time step
        t_end --- End time of the simulation
        deg   --- Polynomial degree
        """

        self.Param = My_Parameters(param_name).get_param()

        try:
            self.Re            = float(self.Param["Reynolds_number"])
            self.At            = float(self.Param["Atwood_number"])
            self.Bo            = float(self.Param["Bond_number"])
            self.rho1          = float(self.Param["Lighter_density"])
            self.dt            = float(self.Param["Time_step"])
            self.t_end         = float(self.Param["End_time"])
        except RuntimeError as e:
            print(str(e) +  "\nPlease check configuration file")
            exit(1)

        #Since this parameters are more related to the numeric part
        #rather than physics we set a default value
        #and so they are present for sure
        self.deg = self.Param["Polynomial_degree"]
        self.reinit_method = self.Param["Reinit_Type"]
        self.stab_method   = self.Param["Stabilization_Type"]
        self.NS_sol_method = self.Param["NS_procedure"]

        #Define an auxiliary dictionary to set proper stabilization
        try:
            self.switcher_NS = {'Standard':self.solve_Standard_NS_system, \
                                'ICT':self.solve_ICT_NS_systems}
        except NameError as e:
            print("Solution procedure for solving Navier-Stokes " + str(e).split("'")[1] + \
                  " declared but not implemented")
            exit(1)
        assert self.NS_sol_method in self.switcher_NS, \
               "Solution method for NAvier-Stokes not available"

        #Define an auxiliary dictionary to set proper stabilization
        try:
            self.switcher_stab = {'IP':self.IP, 'SUPG':self.SUPG}
        except NameError as e:
            print("Stabilization method " + str(e).split("'")[1] + " declared but not implemented")
            exit(1)
        assert self.stab_method in self.switcher_stab, \
               "Stabilization method not available"

        #Check correctness of reinitialization method (in order to avoid typos in particular
        #since, contrarly to stabilization, it is more difficult to think to other choises)
        assert self.reinit_method in ['Conservative','Non_Conservative'], \
               "Reinitialization method not available"

        #Save computational box extrema
        try:
            self.base   = float(self.Param["Base"]) #This is also the reference length
            self.height = float(self.Param["Height"])
        except RuntimeError as e:
            print(str(e) +  "\nPlease check configuration file")
            exit(1)

        #Compute heavier density
        self.rho2 = self.rho1*(1.0 + self.At)/(1.0 - self.At)

        #Compute viscosity: the 'lighter' viscosity will be computed by
        #Reynolds number, while for the 'heavier' we choose to impose a constant
        #density-viscosity ratio (arbitrary choice)
        self.mu1 = self.rho1*np.sqrt(9.81*self.At*self.base)*self.base/self.Re
        self.mu2 = self.rho2*self.mu1/self.rho1

        #Convert useful constants to constant FENICS function
        self.DT = Constant(self.dt)
        self.e2 = Constant((0.0,1.0))

        #Set parameter for standard output
        set_log_level(self.Param["Log_Level"])


    """Build the mesh for the simulation"""
    def build_mesh(self):
        #Generate mesh
        n_points = self.Param["Number_vertices"]
        self.mesh = RectangleMesh(Point(0.0, 0.0), Point(self.base, self.height), \
                                  n_points, n_points)

        #Prepare useful variables for stabilization
        self.h = CellDiameter(self.mesh)/self.base
        if(self.stab_method == 'IP'):
            self.n_mesh = FacetNormal(self.mesh)
            self.h_avg  = (self.h('+') + self.h('-'))/2.0

        #Parameter for interface thickness
        self.eps = 1.0e-8
        self.alpha = Constant(0.1) #Penalty parameter

        #Parameters for reinitialization steps
        hmin = self.mesh.hmin()
        if(self.reinit_method == 'Non_Conservative'):
            self.eps_reinit = Constant(hmin)
            self.alpha_reinit = Constant(0.0625*hmin)
            self.dt_reinit = Constant(np.minimum(0.0001, 0.5*hmin)) #We choose an explicit treatment to keep the linearity
                                                                    #and so a very small step is needed
        elif(self.reinit_method == 'Conservative'):
            self.dt_reinit = Constant(0.5*hmin**(1.1))
            self.eps_reinit = Constant(0.5*hmin**(0.9))

        #Define function spaces
        Velem        = VectorElement("Lagrange", self.mesh.ufl_cell(), self.deg + 1)
        Qelem        = FiniteElement("Lagrange" if self.deg > 0 else "DG", self.mesh.ufl_cell(), self.deg)

        self.W  = FunctionSpace(self.mesh, Velem*Qelem)
        self.assigner = FunctionAssigner(self.W, [self.W.sub(0),self.W.sub(1)])
        self.Q  = FunctionSpace(self.mesh, "CG", 2)
        self.Q2 = VectorFunctionSpace(self.mesh, "CG", 1)

        #Define trial and test functions
        (self.u, self.p) = TrialFunctions(self.W)
        self.phi         = TrialFunction(self.Q)
        (self.v, self.q) = TestFunctions(self.W)
        self.l           = TestFunction(self.Q)

        #Define functions for solutions at previous and current time steps
        self.w_old    = Function(self.W)
        self.phi_old  = Function(self.Q)
        self.w_curr   = Function(self.W)
        (self.u_curr, self.p_curr) = self.w_curr.split(True)
        self.phi_curr = Function(self.Q)

        #Define function for reinitialization
        self.phi0 = Function(self.Q)
        self.phi_intermediate = Function(self.Q) #This is fundamental in case on 'non-conservative'
                                                 #reinitialization and it is also useful for clearness

        #Define function for normal to the interface
        self.grad_phi = Function(self.Q2)


    """Set the proper initial condition"""
    def set_initial_condition(self):
        #Read from configuration file center and radius
        try:
            center = Point(float(self.Param["x_center"]), float(self.Param["y_center"]))
            radius = float(self.Param["Radius"])
        except RuntimeError as e:
            print(str(e) +  "\nPlease check configuration file")
            exit(1)

        #Set initial condition of bubble and check geoemtric limits
        f = Expression("sqrt((x[0]-A)*(x[0]-A) + (x[1]-B)*(x[1]-B))-r",
                        A = center[0], B = center[1], r = radius, degree = 2)
        assert center[0] - radius > 0.0 and center[0] + radius < self.base and \
               center[1] - radius > 0.0 and center[1] + radius < self.height,\
                "Initial condition of interface goes outside the domain"

        #Assign initial condition
        self.phi_old.assign(interpolate(f,self.Q))
        self.w_old.assign(interpolate(Constant((0.0,0.0,0.0)),self.W))
        (self.u_old, self.p_old) = self.w_old.split()
        self.rho_old = self.rho(self.phi_old,self.eps)
        self.mu_old  = self.mu(self.phi_old,self.eps)

        #Compute normal vector to the interface
        self.grad_phi = project(grad(self.phi_old), self.Q2)
        self.n = self.grad_phi/sqrt(inner(self.grad_phi, self.grad_phi))

        #Define function and vector for plotting level-set and computing volume
        self.tmp = Function(self.Q)
        self.lev_set = np.empty_like(self.phi_old.vector().get_local())


    """Auxiliary function to compute density"""
    def rho(self, x, eps):
        return self.rho1*(1.0 - CHeaviside(x,eps)) + self.rho2*CHeaviside(x,eps)


    """Auxiliary function to compute viscosity"""
    def mu(self, x, eps):
        return self.mu1*(1.0 - CHeaviside(x,eps)) + self.mu2*CHeaviside(x,eps)


    """Interior penalty method"""
    def IP(self, phi, l):
        r = self.alpha*self.h_avg*self.h_avg* \
            inner(jump(grad(phi),self.n_mesh), jump(grad(l),self.n_mesh))*dS
        return r


    """SUPG method"""
    def SUPG(self, phi, l):
        r = ((phi - self.phi_old)/self.DT + inner(self.u_curr, grad(phi)))* \
            self.alpha*self.h/ufl.Max(2.0*sqrt(inner(self.u_curr,self.u_curr)), 4.0/(self.Re*self.h))*\
            inner(self.u_curr,self.u_curr)*inner(self.u_curr, grad(l))*dx
        return r


    """Weak formulation for Navier-Stokes"""
    def NS_weak_form(self):
        F1 = self.Re*self.At*self.Bo*self.rho_old* \
             (inner((self.u - self.u_old)/self.DT, self.v) + \
              inner(dot(self.u_old, nabla_grad(self.u)), self.v))*dx \
           + 2.0*self.At*self.Bo*self.mu_old*inner(D(self.u), grad(self.v))*dx \
           - self.At*self.Bo*self.p*div(self.v)*dx \
           + self.Re*self.At*self.Bo*div(self.u)*self.q*dx \
           + self.Re*self.Bo*self.rho_old*inner(self.e2, self.v)*dx \
           + self.Re*div(self.n)*inner(self.n, self.v)*CDelta(self.phi_old, self.eps)*dx

        #Save corresponding weak form and declare suitable matrix and vector
        self.a1 = lhs(F1)
        self.L1 = rhs(F1)

        self.A1 = Matrix()
        self.b1 = Vector()


    """Weak formulation step 1 ICT(Incremental Chorin-Temam)"""
    def Step1_ICT_weak_form(self):
        #Define intermediate function
        self.U_12 = 0.5*(self.u + self.u_old)

        F1 = self.Re*self.At*self.Bo*self.rho_old* \
             (inner((self.u - self.u_old)/self.DT, self.v) + \
              inner(dot(self.u_old, nabla_grad(self.u)), self.v))*dx \
            + 2.0*self.At*self.Bo*self.mu_old*inner(D(self.U_12), grad(self.v))*dx \
            + self.Re*self.Bo*self.rho_old*inner(self.e2, self.v)*dx \
            + self.Re*div(self.n)*inner(self.n, self.v)*CDelta(self.phi_old, self.eps)*dx

        #Save corresponding weak form and declare suitable matrix and vector
        self.a1 = lhs(F1)
        self.L1 = rhs(F1)

        self.A1 = Matrix()
        self.b1 = Vector()


    """Weak formulation step 2 ICT(Incremental Chorin-Temam)"""
    def Step2_ICT_weak_form(self):
        self.a1_bis = inner(grad(self.p), grad(self.q))*dx
        self.L1_bis = inner(grad(self.p_old), grad(self.q))*dx - \
                      (1.0/self.DT)*div(self.u_curr)*self.q*dx

        self.A1_bis = Matrix()
        self.b1_bis = Vector()


    """Weak formulation velocity projection ICT(Incremental Chorin-Temam)"""
    def Step3_ICT_weak_form(self):
        self.a1_tris = inner(self.u, self.v)*dx
        self.L1_tris = inner(self.u_curr, self.v)*dx - \
                       self.DT*inner(grad(self.p_curr - self.p_old), self.v)*dx

        self.A1_tris = Matrix()
        self.b1_tris = Vector()


    """Level-set weak formulation"""
    def LS_weak_form(self):
        F2 = (self.phi - self.phi_old)/self.DT*self.l*dx \
           + inner(self.u_curr, grad(self.phi))*self.l*dx

        F2 += self.switcher_stab[self.stab_method](self.phi, self.l)

        #Save corresponding weak form and declare suitable matrix and vector
        self.a2 = lhs(F2)
        self.L2 = rhs(F2)

        self.A2 = Matrix()
        self.b2 = Vector()


    """Weak form non conservative reinitialization"""
    def NCLSM_weak_form(self):
        self.approx_sign = signp(self.phi_curr, self.eps_reinit)

        self.a3 = self.phi/self.dt_reinit*self.l*dx
        self.L3 = self.phi0/self.dt_reinit*self.l*dx + \
                  self.approx_sign*(1.0 - sqrt(inner(grad(self.phi0), grad(self.phi0))))*self.l*dx -\
                  self.alpha_reinit*inner(grad(self.phi0), grad(self.l))* dx


    """Weak form conservative reinitialization"""
    def CLSM_weak_form(self):
        F3 = (self.phi - self.phi0)/self.dt_reinit*self.l*dx \
           - 0.5*(self.phi + self.phi0)*(1.0 - 0.5*(self.phi + self.phi0))* \
             inner(self.n, grad(self.l))*dx \
           + self.eps_reinit*inner(self.n, grad((0.5*(self.phi + self.phi0))))* \
             inner(self.n, grad(self.l))*dx
        self.a3 = lhs(F3)
        self.L3 = rhs(F3)


    """Set weak formulations"""
    def set_weak_forms(self):
        #Set variational problem for step 1 (Navier-Stokes)
        if(self.NS_sol_method == 'Standard'):
            self.NS_weak_form()
        elif(self.NS_sol_method == 'ICT'):
            self.Step1_ICT_weak_form()
            self.Step2_ICT_weak_form()
            self.Step3_ICT_weak_form()

        #Set variational problem for step 2 (Level-set)
        self.LS_weak_form()

        #Set weak form for level-set reinitialization
        if(self.reinit_method == 'Non_Conservative'):
            self.NCLSM_weak_form()
        elif(self.reinit_method == 'Conservative'):
            self.CLSM_weak_form()


    """Assemble boundary condition"""
    def assembleBC(self):
        self.bcs = DirichletBC(self.W.sub(0), Constant((0.0,0.0)), WallBoundary())


    """Build and solve the system for Navier-Stokes simulation"""
    def solve_Standard_NS_system(self):
        # Assemble matrices and right-hand sides
        assemble(self.a1, tensor = self.A1)
        assemble(self.L1, tensor = self.b1)

        # Apply boundary conditions
        self.bcs.apply(self.A1)
        self.bcs.apply(self.b1)

        #Solve the system
        solve(self.A1, self.w_curr.vector(), self.b1)
        (self.u_curr, self.p_curr) = self.w_curr.split()


    """Build the systems for Navier-Stokes solution through ICT"""
    def solve_ICT_NS_systems(self):
        #Assemble matrix and right-hand side for first step
        assemble(self.a1, tensor = self.A1)
        assemble(self.L1, tensor = self.b1)
        self.bcs.apply(self.A1)
        self.bcs.apply(self.b1)

        #Solve the first step
        solve(self.A1, self.u_curr.vector(), self.b1)

        #Assemble matrix and right-hand side for second step
        assemble(self.a1_bis, tensor = self.A1_bis)
        assemble(self.L1_bis, tensor = self.b1_bis)

        #Solve the second step
        solve(self.A1_bis, self.p_curr.vector(), self.b1_bis)

        #Assemble matrix and right-hand side for the velocity projection step
        assemble(self.a1_tris, tensor = self.A1_tris)
        assemble(self.L1_tris, tensor = self.b1_tris)

        #Solve the projection step
        solve(self.A1_tris, self.u_curr.vector(), self.b1_tris)

        #Assign to the vector function space the found solution
        self.assigner.assign(self.w_curr, [self.u_curr,self.p_curr])

    """Build the system for Level set simulation"""
    def solve_Levelset_system(self):
        # Assemble matrix and right-hand side
        assemble(self.a2, tensor = self.A2)
        assemble(self.L2, tensor = self.b2)

        #Solve the level-set system
        solve(self.A2, self.phi_curr.vector(), self.b2)


    """Build the system for Level set reinitialization"""
    def Levelset_reinit(self):
        #Assign current solution and current normal vector to the interface
        #in case of conservative simulation
        self.phi0.assign(self.phi_curr)
        if(self.reinit_method == 'Non_Conservative'):
            self.approx_sign = signp(self.phi_curr, self.eps_reinit)
        elif(self.reinit_method == 'Conservative'):
            self.grad_phi = project(grad(self.phi_curr), self.Q2)
            self.n = self.grad_phi/sqrt(inner(self.grad_phi, self.grad_phi))

        E_old = 1e10
        for n in range(10):
            #Solve the system
            solve(self.a3 == self.L3, self.phi_intermediate, [])

            #Compute the error and check no divergence
            error = (((self.phi_intermediate - self.phi0)/self.dt_reinit)**2)*dx
            E = sqrt(abs(assemble(error)))

            if(E_old < E):
                raise RuntimeError("Divergence at the reinitialization level (iteration " + str(n + 1) + ")")
            elif(E_old - E < 1e-3):
                break

            E_old = E

            #Set previous step solution
            self.phi0.assign(self.phi_intermediate)

        #Assign the reinitialized level-set to the current solution and
        #update normal vector to the interface (for Navier-Stokes)
        self.phi_curr.assign(self.phi_intermediate)
        self.grad_phi = project(grad(self.phi_curr), self.Q2)
        self.n = self.grad_phi/sqrt(inner(self.grad_phi, self.grad_phi))


    """Plot the level-set function and compute the volume"""
    def plot_and_volume(self):
        #Extract vector for FE function
        self.phi_curr_vec = self.phi_curr.vector().get_local()

        #Construct vector of ones inside the bubble
        for i in range(len(self.phi_curr_vec)):
            self.lev_set[i] = 1.0*(self.phi_curr_vec[i] < 0.0)

        #Assign vector to FE function
        self.tmp.vector().set_local(self.lev_set)

        #Plot the function just computed
        #fig = plot(self.tmp, interactive = True, scalarbar = True)
        #plt.colorbar(fig)
        #plt.show()

        #Check volume consistency
        Vol = assemble(self.tmp*dx)
        begin(int(LogLevel.INFO) + 1,"Volume = " + str(Vol))
        end()


    """Execute simulation"""
    def run(self):
        #Build the mesh
        self.build_mesh()

        #Set the initial condition
        self.set_initial_condition()

        #Set weak formulations
        self.set_weak_forms()

        #Assemble boundary conditions
        self.assembleBC()

        #Time-stepping loop
        t = self.dt
        while t <= self.t_end:
            begin(int(LogLevel.INFO) + 1,"t = " + str(t))

            #Solve Navier-Stokes
            begin(int(LogLevel.INFO) + 1,"Solving Navier-Stokes")
            self.switcher_NS[self.NS_sol_method]()
            #print(self.u_curr.vector().get_local)
            end()

            #Solve level-set
            begin(int(LogLevel.INFO) + 1,"Solving Level-set")
            self.solve_Levelset_system()
            #print(self.phi_curr.vector().get_local())
            end()

            #Apply reinitialization for level-set
            try:
                begin(int(LogLevel.INFO) + 1,"Solving reinitialization")
                self.Levelset_reinit()
                end()
            except RuntimeError as e:
                print(e)
                print("Aborting simulation...")
                exit(1)

            begin(int(LogLevel.INFO) + 1,"Plotting and computing volume")
            self.plot_and_volume()
            end()

            end()

            #Prepare to next step assign previous-step solution
            self.w_old.assign(self.w_curr)
            (self.u_old, self.p_old) = self.w_old.split()
            self.phi_old.assign(self.phi_curr)
            self.rho_old = self.rho(self.phi_old,self.eps)
            self.mu_old = self.mu(self.phi_old,self.eps)

            t = t + self.dt if t + self.dt <= self.t_end or abs(t - self.t_end) < DOLFIN_EPS else self.t_end